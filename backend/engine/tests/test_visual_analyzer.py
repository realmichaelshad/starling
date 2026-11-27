"""The Analyzer is the semantic gate for Image Radar, not query-word overlap."""
from __future__ import annotations

from app.analyzer import analyze_visual_brief, score_visual_candidate
from app import images as im


def test_bare_ambiguous_name_does_not_start_an_image_search():
    brief = analyze_visual_brief("Claude has everyone talking today.", action="post")

    assert brief["state"] == "none"
    assert brief["queries"] == []


def test_claude_model_brief_requires_anthropic_context_in_the_candidate():
    brief = analyze_visual_brief(
        "Anthropic just released Claude Opus, and the model's coding work is a real step up.",
        action="post",
    )

    assert brief["state"] == "optional"
    assert brief["queries"] == ["Claude Anthropic AI model"]
    assert score_visual_candidate(brief, "Claude the Dragon bronze sculpture at Burning Man")["pass"] is False

    approved = score_visual_candidate(
        brief, "Anthropic Claude Opus AI model release announcement",
    )
    assert approved["pass"] is True
    assert approved["entity_match"] >= 0.8


def test_comparison_requires_an_image_that_actually_carries_the_comparison():
    brief = analyze_visual_brief(
        "Anthropic's Claude Opus leads this coding benchmark, but latency is still the tradeoff.",
        action="post",
    )

    assert brief["state"] == "recommended"
    assert score_visual_candidate(brief, "Anthropic Claude Opus product image")["pass"] is False
    assert score_visual_candidate(
        brief, "Anthropic Claude Opus coding benchmark comparison chart and latency data",
    )["pass"] is True


def test_image_scraper_refuses_thumbnail_only_results(monkeypatch):
    from app import research

    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(im, "_openverse_results", lambda _query, _limit: [{
        "thumbnail": "https://example.com/thumb.jpg", "title": "Claude Opus",
        "tags": [{"name": "Anthropic"}],
    }])
    downloaded: list[str] = []
    monkeypatch.setattr(im, "_download_licensed_image", lambda url, deadline: downloaded.append(url))

    assert im.search_web_image(["Claude Anthropic AI model"]) == []
    assert downloaded == []


def test_high_quality_web_bar_is_stricter_than_the_general_image_bar():
    inspection = {"ok": True, "quality_score": im.WEB_MIN_QUALITY - 0.01, "problems": []}
    licensed = im.Provenance(rights=im.RIGHTS_LICENSE_CLEARED,
                              source_tier=im.SOURCE_LICENSED)

    result = im.passes_visual_bar(licensed, inspection, claim_relevance=1.0)

    assert result["pass"] is False
    assert result["gate"] == "quality"
    assert "high-quality web threshold" in result["reason"]


def test_user_image_habit_makes_relevant_visuals_more_available_without_relaxing_matching():
    from app import storage

    for index in range(10):
        storage.upsert_post({
            "post_id": f"own-{index}", "post_type": "post",
            "text": f"Open source agent note {index}",
            "has_image": index < 6,
            "created_at": f"2026-07-{index + 1:02d}T12:00:00+00:00",
        })
    habit = im.observed_visual_habit(action="post")
    brief = analyze_visual_brief(
        "Open source agents are getting much better at browser automation.",
        action="post", visual_habit=habit,
    )

    assert habit["image_forward"] is True
    assert habit["overall_rate"] == 0.6
    assert brief["state"] == "optional"
    assert brief["queries"]
    # A generic visual still does not earn a place just because this account
    # uses images frequently.
    assert score_visual_candidate(brief, "abstract blue technology wallpaper")["pass"] is False


def test_direct_image_choices_can_establish_a_preference_before_history_exists():
    from app import storage

    for _ in range(5):
        storage.save_image_feedback({"choice": "used"})

    habit = im.observed_visual_habit(action="post")

    assert habit["image_forward"] is True
    assert habit["feedback"]["keep_rate"] == 1.0


def _clean_owned_image(path) -> None:
    """A clean, high-resolution local asset for the live handoff test."""
    from PIL import Image, ImageDraw

    gradient = Image.linear_gradient("L").resize((1200, 675))
    image = Image.merge("RGB", (gradient, gradient, gradient.point(lambda value: 200 - value // 2)))
    ImageDraw.Draw(image).rectangle((300, 170, 600, 337), fill=(20, 20, 20))
    image.save(path, format="PNG")


def test_live_visual_chain_rejects_the_wrong_claude_and_prepares_the_right_asset(tmp_path, monkeypatch):
    """Analyzer -> semantic gate -> inspection -> receipt -> composer payload."""
    from app import main, storage

    # Production accepts only images managed beneath Starling's media root.
    # Give this integration test the equivalent isolated root instead of
    # bypassing that guard with an arbitrary temporary path.
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "starling.db")
    storage.initialize()
    media_dir = tmp_path / "reference-images"
    media_dir.mkdir()
    asset = media_dir / "claude-opus.png"
    _clean_owned_image(asset)
    storage.replace_reference_images([{
        "filename": asset.name,
        "source_url": "https://example.com/claude-opus",
        "rights_status": im.RIGHTS_USER_OWNED,
        "notes": "Anthropic Claude Opus AI model release announcement",
        "local_path": str(asset),
    }])

    receipt = main._visual_offer(
        "Anthropic just released Claude Opus, and the model's coding work is a real step up.",
        action="post",
    )

    assert receipt is not None
    assert receipt["preview_data_url"].startswith("data:image/png;base64,")
    assert receipt["source"]["rights"] == im.RIGHTS_USER_OWNED
    assert receipt["post_fit"]["visual_job"] == "identify the exact announced product"
    assert receipt["post_fit"]["analyzer_fit"]["pass"] is True
    assert receipt["post_fit"]["decision"] in (im.ATTACH, im.OFFER)

    storage.replace_reference_images([{
        # Same library key replaces the approved asset, so this leg exercises
        # the wrong candidate alone rather than retaining the prior test row.
        "filename": asset.name,
        "source_url": "https://example.com/dragon",
        "rights_status": im.RIGHTS_USER_OWNED,
        "notes": "Claude the Dragon bronze sculpture",
        "local_path": str(asset),
    }])

    assert main._visual_offer(
        "Anthropic just released Claude Opus, and the model's coding work is a real step up.",
        action="post",
    ) is None


# --------------------------------------------------------------------------- #
# Why an image never once appeared
# --------------------------------------------------------------------------- #

def test_search_relaxes_multi_word_queries_narrowest_first():
    """Openverse ANDs the terms, so Analyzer's exact subject phrase matched
    nothing (measured: 0 results for "Claude Anthropic AI model", 20 for
    "artificial intelligence"). The full query must still be tried FIRST so an
    exact match always outranks a relaxed one."""
    from app.images import _relaxations

    out = _relaxations(["Claude Anthropic AI model data chart"])
    assert out[0] == "Claude Anthropic AI model data chart"
    assert "Claude Anthropic" in out
    assert len(out) <= 8
    assert len(out) == len(set(out)), "no duplicate searches"
    # It must never relax to a single word — one word is how an unrelated
    # sculpture named Claude gets through.
    assert all(len(variant.split()) >= 2 for variant in out)


def test_relaxation_keeps_single_word_queries_intact():
    from app.images import _relaxations

    assert _relaxations(["technology"]) == ["technology"]
