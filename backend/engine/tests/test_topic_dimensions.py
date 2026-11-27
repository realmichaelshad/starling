"""Multi-dimensional topics: each beat is first-class, pillars merge.

His requirement: an account that talks about two or more topics must be
represented as separate dimensions — not blurred into one bag, not gated
down to the dominant beat — and the pillar list must FOLLOW the account
(configured pillars stay; learned beats append; abandoned beats fade).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import account_model
from app.account_model import (
    ACTIVE_DIMENSION_MIN_RECENCY_SHARE,
    infer_account_model,
    reset_topic_dimension_cache,
    topic_dimensions,
)
from app.storage import set_account_profile, upsert_post

# Relative to the real clock, because topic_dimensions() judges "active" against the real clock
# (account_model._utc_now). A fixed date here made the "posted today" beat age out of the 60-day
# window from about 29 September 2026 onwards.
NOW = datetime.now(timezone.utc).replace(microsecond=0)

AI_BEAT = [
    "New benchmark drop: the model clears every eval suite at half the price.",
    "Ran the benchmark myself, the model scores hold up on the harder eval set.",
    "Benchmark culture is broken but this model eval is honestly designed.",
    "The model wins the coding benchmark and loses the reasoning eval, interesting split.",
    "Every benchmark chart hides the eval harness details that matter for the model.",
    "A model release without a public eval benchmark is just a press release.",
]

COOKING_BEAT = [
    "Sourdough week: the bread finally has an open crumb after the cold proof.",
    "Baking sourdough taught me more patience than any bread book ever did.",
    "The sourdough starter survived vacation, the bread gods are merciful bakers.",
    "Best bread of the year: sourdough with roasted garlic folded in before baking.",
    "Baking tip: sourdough crumb opens up when the bread proofs overnight.",
    "My sourdough bread baking notes are now longer than my work notes.",
]


def _dated(text: str, days_old: float) -> dict:
    return {"text": text, "post_type": "post",
            "created_at": (NOW - timedelta(days=days_old)).isoformat()}


def _two_beat_rows() -> list[dict]:
    rows = [_dated(text, 40.0 - index) for index, text in enumerate(AI_BEAT)]
    rows += [_dated(text, 10.0 - index) for index, text in enumerate(COOKING_BEAT)]
    return rows


def test_two_beats_become_two_active_dimensions():
    dimensions = topic_dimensions(_two_beat_rows())
    for dimension in dimensions:
        assert 0.0 < dimension["share"] <= 1.0
        assert 0.0 <= dimension["recency_share"] <= 1.0
        assert dimension["last_active"] is not None
        assert dimension["tokens"]
        assert dimension["display_label"]
    assert abs(sum(d["recency_share"] for d in dimensions) - 1.0) < 0.05

    active = [dim for dim in dimensions if dim["active"]]
    assert len(active) >= 2
    labels = " ".join(dim["display_label"] for dim in active)
    assert "sourdough" in labels or "bread" in labels
    assert "benchmark" in labels or "model" in labels
    ai_tokens = next(set(d["tokens"]) for d in active if "benchmark" in " ".join(d["tokens"]))
    cook_tokens = next(set(d["tokens"]) for d in active if "sourdough" in " ".join(d["tokens"]))
    # The beats may share ambient vocabulary; their TOP token cores must not
    # collapse into one another.
    assert "sourdough" not in ai_tokens
    assert "benchmark" not in cook_tokens


def test_learned_dimension_appends_to_configured_pillars():
    set_account_profile({"content_pillars": ["food", "recipes"], "audience": "home cooks"})
    for index, text in enumerate(AI_BEAT):
        upsert_post({"post_id": f"ai-{index}", "post_type": "post", "text": text,
                     "created_at": (datetime.now(timezone.utc) - timedelta(days=5 + index)).isoformat()})
    reset_topic_dimension_cache()
    model = infer_account_model()
    pillars = model["content_pillars"]
    assert pillars[:2] == ["food", "recipes"]
    assert len(pillars) >= 3
    assert any("benchmark" in pillar or "model" in pillar or "eval" in pillar
               for pillar in pillars[2:])


def test_overlapping_dimension_appends_nothing():
    """The test_account_model invariant, asserted where it is designed:
    dimension-level dedupe keeps configured pillars exactly when the learned
    dimension is the same subject."""
    set_account_profile({"content_pillars": ["benchmarks", "evals"], "audience": "AI builders"})
    for index, text in enumerate(AI_BEAT):
        upsert_post({"post_id": f"ai-{index}", "post_type": "post", "text": text,
                     "created_at": (datetime.now(timezone.utc) - timedelta(days=5 + index)).isoformat()})
    reset_topic_dimension_cache()
    model = infer_account_model()
    assert model["content_pillars"] == ["benchmarks", "evals"]


def test_cold_start_keeps_the_frequent_token_fallback():
    """Posts too short to group (under the 25-char usable floor) leave no
    dimensions, so pillars fall back to the frequent-token path exactly."""
    set_account_profile({"audience": "anyone"})
    for index in range(3):
        upsert_post({"post_id": f"cold-{index}", "post_type": "post",
                     "text": f"quick tools note {index}"})
    reset_topic_dimension_cache()
    assert topic_dimensions() == []
    model = infer_account_model()
    assert model["content_pillars"]
    assert "tools" in model["content_pillars"]


def test_three_related_posts_already_make_a_dimension_pillar():
    set_account_profile({"audience": "anyone"})
    for index in range(3):
        upsert_post({"post_id": f"grow-{index}", "post_type": "post",
                     "text": f"Shipping small tools beats planning big tools, attempt {index}."})
    reset_topic_dimension_cache()
    model = infer_account_model()
    assert model["content_pillars"]
    assert any("tools" in pillar for pillar in model["content_pillars"])


def test_two_beat_account_without_config_shows_both_beats():
    set_account_profile({"audience": "anyone"})
    for index, text in enumerate(AI_BEAT):
        upsert_post({"post_id": f"mixa-{index}", "post_type": "post", "text": text,
                     "created_at": (datetime.now(timezone.utc) - timedelta(days=20 + index)).isoformat()})
    for index, text in enumerate(COOKING_BEAT):
        upsert_post({"post_id": f"mixc-{index}", "post_type": "post", "text": text,
                     "created_at": (datetime.now(timezone.utc) - timedelta(days=2 + index)).isoformat()})
    reset_topic_dimension_cache()
    pillars = infer_account_model()["content_pillars"]
    joined = " ".join(pillars)
    assert any(token in joined for token in ("benchmark", "model", "eval"))
    assert any(token in joined for token in ("sourdough", "bread", "baking"))


def test_a_beat_posted_on_today_is_active_however_small():
    """Measured on his live archive 2026-07-31: his one real beat carries 28
    of 498 recent posts (0.062 share) because the rest of his timeline is
    one-off personal tweets. A share-only rule marked it inactive, and every
    consumer fell back to pre-dimension behaviour — the feature was inert on
    the only account it had ever run against. An account with six beats has
    none above the share line either."""
    rows = [_dated(text, float(index)) for index, text in enumerate(COOKING_BEAT)]
    rows += [_dated(f"Unrelated one-off thought number {index}, nothing recurring here at all.",
                    float(index)) for index in range(60)]
    active = [dim for dim in topic_dimensions(rows) if dim["active"]]
    assert active, "a beat posted on today must stay active at any share"
    assert active[0]["recency_share"] < ACTIVE_DIMENSION_MIN_RECENCY_SHARE


def test_an_abandoned_small_beat_goes_inactive():
    """The other half of the rule: recency keeps a live beat, share is what
    retires one he has walked away from."""
    rows = [_dated(text, 400.0 + index) for index, text in enumerate(COOKING_BEAT)]
    rows += [_dated(f"Recent unrelated note {index} about entirely other matters today.",
                    float(index)) for index in range(60)]
    cooking = [dim for dim in topic_dimensions(rows)
               if "sourdough" in " ".join(dim["tokens"]) or "bread" in " ".join(dim["tokens"])]
    assert cooking, "the beat should still be listed as history"
    assert not cooking[0]["active"]


def test_confirmed_shift_fades_an_abandoned_beat(monkeypatch):
    """The mechanism his ask names: after a confirmed approach shift the
    30-day half-life drops an old beat below the pillar floor while the new
    beat clears it — under the default 90d the old beat would still hold."""
    rows = [_dated(text, 120.0 + index) for index, text in enumerate(AI_BEAT)]
    rows += [_dated(text, float(index)) for index, text in enumerate(COOKING_BEAT[:3])]
    monkeypatch.setattr(account_model, "topic_dimensions", lambda profile=None: [])

    import app.approach as approach_mod
    monkeypatch.setattr(approach_mod, "approach_state",
                        lambda *args, **kwargs: {"state": "shifted", "half_life_days": 30.0})
    shifted = infer_account_model(rows, weighted=True)
    assert "sourdough" in shifted["content_pillars"]
    assert "benchmark" not in shifted["content_pillars"]

    monkeypatch.setattr(approach_mod, "approach_state",
                        lambda *args, **kwargs: {"state": "stable", "half_life_days": 90.0})
    stable = infer_account_model(rows, weighted=True)
    assert "benchmark" in stable["content_pillars"]


def test_explicit_rows_stay_unweighted():
    rows = [_dated(text, 10.0 * index) for index, text in enumerate(AI_BEAT)]
    model = infer_account_model(rows)
    assert model["voice_profile"]["recency"]["weighted"] is False
    assert model["voice_profile"]["style"].get("weighted") is False
