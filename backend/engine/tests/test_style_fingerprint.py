"""The writer must match the account's measured style (vocab, formatting, emoji,
hashtags, length) — the 'scary accurate' voice, not a generic register."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.account_model import _style_metrics, infer_account_model, style_directive
from app.intelligence import _match_account_style
from app.storage import set_account_profile, upsert_post


@pytest.fixture(autouse=True)
def seeded():
    set_account_profile({"content_pillars": ["Anthropic", "Claude"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    # Distinctive style: short, multi-line, no emoji, no hashtags, a CAPS word.
    samples = [
        "Kimi K3 tops the board.\n\nThat's the REAL story here.\n\nNot the benchmark.",
        "Opus context doubled.\n\nNobody's talking about it.",
        "@anthropic just shipped again.\n\nThe pace is wild honestly.",
    ]
    for i, text in enumerate(samples * 3):
        upsert_post({"post_id": f"s{i}", "text": text, "post_type": "post",
                     "created_at": (now - timedelta(days=i)).isoformat(),
                     "metrics": {"views": 100}})


def test_style_metrics_capture_the_signature():
    style = infer_account_model()["voice_profile"]["style"]
    assert style["hashtag_rate"] == 0.0          # never uses hashtags
    assert style["emoji_rate"] == 0.0            # never uses emoji
    assert style["multiline_rate"] > 0.5         # writes multi-line
    assert style["allcaps_emphasis_rate"] > 0.0  # CAPS for emphasis
    assert style["median_words"] > 0


def test_style_directive_is_imperative_and_concrete():
    directive = style_directive(infer_account_model())
    low = directive.lower()
    assert "length" in low and "hashtags: none" in low
    assert "emoji: none" in low
    assert "all caps" in low  # the emphasis signature is surfaced


def test_offstyle_hashtags_are_stripped():
    brief = {"account_model": infer_account_model()}
    out = _match_account_style("Kimi K3 is wild. #AI #MachineLearning", brief)
    assert "#" not in out
    assert "Kimi K3 is wild." in out


def test_offstyle_emoji_stripped_when_account_never_uses_them():
    brief = {"account_model": infer_account_model()}
    out = _match_account_style("Kimi K3 is wild \U0001F525\U0001F680", brief)
    assert "\U0001F525" not in out and "\U0001F680" not in out


def test_style_clean_up_preserves_content_while_reshaping():
    # A multi-line writer's block draft is laid out one thought per line, but no
    # words are dropped or altered — the content is exactly the same.
    brief = {"account_model": infer_account_model()}
    text = "Kimi K3 tops the board. That's the REAL story, not the benchmark."
    out = _match_account_style(text, brief)
    assert "Kimi K3 tops the board." in out
    assert "That's the REAL story, not the benchmark." in out
    assert out.replace("\n\n", " ").strip() == text


def test_reply_format_uses_reply_samples_and_preserves_their_compact_spacing():
    now = datetime.now(timezone.utc)
    # The account's standalone posts use roomy paragraphs, but its replies are
    # compact one-line-break takes. A reply must follow the latter, not inherit
    # the post format merely because there are more post samples.
    for index in range(4):
        upsert_post({"post_id": f"post-{index}", "post_type": "post",
                     "text": "A standalone point.\n\nA roomy second block.",
                     "created_at": (now - timedelta(days=index)).isoformat()})
        upsert_post({"post_id": f"reply-{index}", "post_type": "reply",
                     "text": "First quick point.\nSecond quick point.",
                     "created_at": (now - timedelta(days=index, minutes=1)).isoformat()})
    model = infer_account_model()
    reply_directive = style_directive(model, "reply").lower()
    assert "do not add blank spacer lines" in reply_directive
    brief = {"account_model": model}
    out = _match_account_style("First quick point. Second quick point.", brief, "reply")
    assert out == "First quick point.\nSecond quick point."


def test_reply_without_reply_samples_never_borrows_roomy_post_spacing():
    # This is the common cold-start case: a few original posts reveal language
    # habits, but none prove that replies use the same blank-line layout.
    model = infer_account_model()
    directive = style_directive(model, "reply").lower()
    assert "no confirmed reply samples" in directive
    assert "do not add blank spacer lines" in directive
    brief = {"account_model": model}
    text = "First quick point. Second quick point."
    assert _match_account_style(text, brief, "reply") == text


def _model_from(texts, has_img=None):
    import re
    from app.account_model import _style_metrics
    rows = [{"text": t, "has_image": (has_img[i] if has_img else False)} for i, t in enumerate(texts)]
    wc = [len(re.findall(r"\S+", t)) for t in texts]
    return {"voice_profile": {"style": _style_metrics(rows, texts, wc, len(texts))}}


def test_directive_generalizes_to_an_emoji_hashtag_account():
    m = _model_from(["wild 🔥🔥 #ai #tech", "best rn 😭 #ml", "no cap 🚀 #future"] * 4)
    d = style_directive(m).lower()
    assert "emoji: usually" in d or "emoji: often" in d   # NOT "none"
    assert "hashtags: none" not in d                       # it uses them
    assert "mostly lowercase" in d                          # casual casing


def test_directive_surfaces_image_habit():
    m = _model_from(["look at this", "wait what", "this is it"] * 4, has_img=[True] * 12)
    assert "image" in style_directive(m).lower()


def test_directive_hedges_on_a_tiny_archive():
    m = _model_from(["first post about ai", "second post here"])
    assert "rough read" in style_directive(m).lower()


def test_directive_respects_a_technical_account():
    m = _model_from(["The hierarchical attention architecture demonstrates substantial "
                     "computational efficiency improvements across distributed inference."] * 12)
    assert "technical" in style_directive(m).lower()
    assert "don't dumb it down" in style_directive(m).lower()


def test_nothing_is_hardcoded_to_one_account():
    """Two different archives must yield different directives."""
    a = style_directive(_model_from(["wild 🔥 #ai"] * 10))
    b = style_directive(_model_from(["A measured, formal observation about model architecture."] * 10))
    assert a != b
