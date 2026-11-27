"""Post-feedback, diagnosis, and advice must be DETERMINISTIC and grounded.

Each of these questions previously went to the 9B model, which refused, wrote
the wrong thing, or invented facts. They now compute from real data.
"""
from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone

from app.storage import set_account_profile, upsert_post
from app.strategy import _facts_question_answer


def _seed():
    """A small, known history so the deterministic branches have real numbers."""
    set_account_profile({"content_pillars": ["Anthropic", "Claude", "Kimi", "OpenAI"],
                         "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    # Newest first: the last post is a weak one (78 views) so the critique has a
    # clear underperform verdict to produce.
    posts = [
        ("p1", "Kimi K3 is already changing the AI industry for good", 78, 0),
        ("p2", "Grok 4.5 pricing keeps coming up in my mentions honestly", 400, 1),
        ("p3", "Opus context window doubled and nobody is talking about it", 250, 2),
        ("p4", "Claude Fable is quietly the best writer right now", 150, 3),
        ("p5", "OpenAI shipped again, the pace is relentless these days", 300, 4),
        ("p6", "GPT and Gemini both moved this week, wild timing", 180, 5),
        ("p7", "Local models are catching up faster than priced in", 200, 6),
        ("p8", "The context window race is the real story now", 120, 7),
    ]
    for pid, text, views, days_ago in posts:
        created = now - timedelta(days=days_ago, hours=1)
        upsert_post({"post_id": pid, "text": text, "post_type": "post",
                     "created_at": created.isoformat(),
                     "metrics": {"views": views, "likes": 3, "replies": 1},
                     "metrics_updated_at": (created + timedelta(hours=6)).isoformat()})


@pytest.fixture(autouse=True)
def seeded():
    _seed()
    yield


def test_critique_compares_the_last_post_to_a_real_median():
    answer = _facts_question_answer("what do you think of my last post")
    assert answer is not None
    # It must cite the actual last post's views (78) and read like a judgement.
    assert "78" in answer
    assert any(word in answer.lower() for word in ("underperform", "median", "middle", "strong"))
    # never the model's refusal
    assert "i don't have access" not in answer.lower()
    assert "can't assist" not in answer.lower()


def test_roast_uses_the_same_data_bluntly():
    answer = _facts_question_answer("roast my last post")
    assert answer is not None and "78" in answer


def test_was_my_last_post_good_routes_to_critique():
    answer = _facts_question_answer("was my last post good")
    assert answer is not None and "78" in answer


def test_diagnosis_gives_real_numbers_not_a_refusal():
    answer = _facts_question_answer("why arent my posts doing well")
    assert answer is not None
    assert "median" in answer.lower()
    assert "i don't have access" not in answer.lower()


def test_whats_working_is_grounded():
    answer = _facts_question_answer("can you look at my recent posts and tell me whats working")
    assert answer is not None and "median" in answer.lower()


def test_viral_never_promises_and_never_refuses():
    answer = _facts_question_answer("how do i go viral")
    assert answer is not None
    assert "can't promise" in answer.lower() or "cannot promise" in answer.lower()
    assert "can't assist" not in answer.lower()


def test_tweet_advice_explains_not_writes_a_tweet():
    answer = _facts_question_answer("what makes a good tweet")
    assert answer is not None
    assert "one clear point" in answer.lower() or "one point" in answer.lower()
    # It should not itself BE a posted-style tweet with hashtags.
    assert "#" not in answer


def test_critique_and_diagnosis_use_the_same_median():
    """A sharp user must not see two different 'medians'."""
    import re
    crit = _facts_question_answer("what do you think of my last post")
    diag = _facts_question_answer("why arent my posts doing well")
    crit_med = re.search(r"median[^0-9]*(\d+)", crit.lower())
    diag_med = re.search(r"median of (\d+)", diag.lower())
    if crit_med and diag_med:
        assert crit_med.group(1) == diag_med.group(1)
