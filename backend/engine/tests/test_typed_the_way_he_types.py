"""The deterministic answers have to survive HIS typing, not clean input.

Every account answer was written and tested against correctly spelled English,
and matched `message.lower()` raw — while the greeting, scope and capability
routers all matched a typo-normalised copy. He does not type correctly spelled
English. Measured against a live companion, typing the way he actually does:

    "waht was my mroe recnt psot"  -> "I'm with you. Give me the rough idea…"
    "whats my raech"               -> "Your reach is limited. You're posting in
                                       a niche, and the audience is small."
    "am i psoting to much"         -> "You're posting 3 times a day"

Not one reached its answer. All three fell through to the model, and the last
two are the exact fabrications those answers exist to prevent — the same
"median reach score" and "3 a day is your budget" failures reappearing through
a door nobody had checked. The correct answers already existed; the intents
were simply unreachable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.storage import set_account_profile, upsert_post
from app.strategy import _facts_question_answer
from app.text_normalizer import normalize_typos


def _seed() -> None:
    set_account_profile({"audience": "AI builders",
                         "content_pillars": ["AI models"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    for index, (kind, views) in enumerate(
            [("post", 4886), ("post", 300), ("post", 12),
             ("reply", 486), ("reply", 200)]):
        upsert_post({"post_id": f"t{index}", "post_type": kind,
                     "created_at": (now - timedelta(days=index + 0.5)).isoformat(),
                     "text": f"seeded post {index}, long enough to read back",
                     "metrics": {"views": views, "likes": 1, "replies": 0},
                     "metrics_updated_at": now.isoformat()})


# (typed the way he types, a phrase that must appear in the answer)
HIS_TYPING = [
    ("waht was my mroe recnt psot", "seeded post"),
    ("hwo mnay folwers do i have", "follower"),
    ("whats my raech", "views"),
    ("am i psoting to much", "averaging"),
    ("waht is my wrost psot", "12 views"),
    ("hwo mnay veiws did i get this wek", "views across the last 7 days"),
    ("wehn was my lsat psot", "went out"),
    ("hwo mnay repleis have i snet", "2 replies"),
    ("whats my engagment", "median views"),
    ("waht time do i usaully psot", "mostly post around"),
]


@pytest.mark.parametrize("message,expected", HIS_TYPING)
def test_his_typing_reaches_the_real_answer(message: str, expected: str) -> None:
    _seed()
    reply = _facts_question_answer(message)
    assert reply is not None, f"no deterministic answer for {message!r}"
    assert expected.lower() in reply.lower(), f"{message!r} -> {reply!r}"


@pytest.mark.parametrize("typo,fixed", [
    ("hwo", "how"), ("mnay", "many"), ("folwers", "followers"),
    ("raech", "reach"), ("mroe", "more"), ("recnt", "recent"),
    ("wrost", "worst"), ("lsat", "last"), ("wek", "week"),
    ("psoting", "posting"), ("veiws", "views"), ("nmae", "name"),
    ("engagment", "engagement"),
])
def test_the_routing_vocabulary_covers_his_transpositions(typo: str, fixed: str) -> None:
    assert normalize_typos(typo) == fixed


def test_the_same_question_gets_the_same_answer_however_it_is_spelled() -> None:
    _seed()
    clean = _facts_question_answer("how many followers do i have")
    typed = _facts_question_answer("hwo mnay folwers do i have")
    assert clean == typed


@pytest.mark.parametrize("message", [
    "when should i post",
    "is now a good time to post",
])
def test_clean_input_is_unchanged(message: str) -> None:
    """Widening the match must not re-route questions that already worked."""
    _seed()
    reply = _facts_question_answer(message)
    assert reply is not None
    assert "mostly post around" not in reply.lower()
    assert "went out" not in reply
