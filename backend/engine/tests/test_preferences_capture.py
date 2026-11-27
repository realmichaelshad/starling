"""Standing instructions have to stick, and none of them ever did.

`preference_memories` has never held a row, and there were two reasons:

  1. `propose_preference` returned confidence of exactly 0.78 for every match,
     while the caller saved a proposal only when confidence was `< 0.78`. The
     single value the function could produce was precisely the value that
     excluded it — so nothing was persisted, at any confidence, ever.
  2. Only "don't / do not / never X" was recognised. "always keep replies
     under 20 words", "stop starting posts with…", "no, shorter" — the ways he
     actually talks — matched nothing.

Confirmation still gates persistence. Confidence now only says how sure the
proposal is, which is what it was always supposed to mean.
"""
from __future__ import annotations

import pytest

from app.preferences import propose_preference


@pytest.mark.parametrize("message,fragment", [
    ("never use hashtags", "hashtags"),
    ("don't use em dashes in my posts", "em dashes"),
    ("always keep replies under 20 words", "under 20 words"),
    ("stop starting posts with lets be real", "lets be real"),
    ("i hate the word delve", "delve"),
    ("keep it punchier", "punchier"),
])
def test_the_ways_he_actually_phrases_a_rule(message: str, fragment: str) -> None:
    inferred = propose_preference(message)
    assert inferred, f"not recognised: {message!r}"
    assert fragment in inferred["rule"].lower()


def test_a_correction_to_the_current_draft_is_a_weaker_proposal():
    """'no, shorter' is a real instruction but a weak guess at scope."""
    inferred = propose_preference("no, shorter")
    assert inferred and "shorter" in inferred["rule"].lower()
    assert inferred["confidence"] < 0.78


def test_a_flat_imperative_is_a_confident_proposal():
    assert propose_preference("never use hashtags")["confidence"] >= 0.78


def test_no_inferred_confidence_lands_on_the_old_dead_boundary():
    """The bug in one line: every value equalled the threshold that dropped it."""
    for message in ("never use hashtags", "i prefer short replies",
                    "always cite the source", "i hate hype"):
        inferred = propose_preference(message)
        assert inferred is None or inferred["confidence"] != 0.78, message


@pytest.mark.parametrize("message", [
    "what's trending today",
    "how many followers do i have",
    "write me a post about opus 5",
    "when should i post",
])
def test_an_ordinary_question_is_not_a_preference(message: str) -> None:
    assert propose_preference(message) is None, message


def test_the_thread_rule_still_wins():
    inferred = propose_preference("never post threads")
    assert inferred["scope"] == "format"
    assert inferred["confidence"] == 0.99
