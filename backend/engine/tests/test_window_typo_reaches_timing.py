""""best widnwo" is a timing question, and it used to reach the model instead.

He typed *"when is my best widnwo for psotign give me the exact day and time"*.
`psotign` was corrected, `widnwo` was not, and the timing capability's matcher
needs `\\bbest window\\b` — none of its other alternatives ("when should i post",
"best time to post", "what time should i") appear in that sentence. So the typo
was the entire difference between his own measured posting windows and a model
guessing at them.

WHY THIS IS A CURATED KEY AND NOT A VOCABULARY ENTRY. Adding "window" to
`_CANONICAL` is the obvious fix and it fails on both sides at once:

    widnwo  -> window   difflib 0.667   UNDER the 0.88 cutoff — his sentence
                                        would still not have been fixed
    widow   -> window   difflib 0.909   over — a real, different word
    windows -> window   difflib 0.923   over — the operating system

The fuzzy stage cannot reach a two-swap typo without also reaching one-letter
neighbours. An exact key reaches his and nothing else, which is the same
argument the file already makes for `automaticly`.
"""
from __future__ import annotations

import pytest

from app.capabilities import find_capability
from app.text_normalizer import normalize_typos


HIS_MESSAGE = "when is my best widnwo for psotign give me the exact day and time"


def test_his_sentence_reaches_the_timing_answer():
    capability = find_capability(HIS_MESSAGE)
    assert capability is not None, "still falls through to the model"
    assert capability.key == "timing", capability.key


def test_the_correction_is_what_does_it():
    assert normalize_typos(HIS_MESSAGE) == (
        "when is my best window for posting give me the exact day and time")


@pytest.mark.parametrize("typo", [
    "widnwo", "widnow", "windwo", "wnidow", "winodw", "iwndow",
])
def test_the_transposition_family_is_covered(typo):
    """None of these are English words, and none are reachable by the fuzzy
    stage — one adjacent swap does not close a two-swap gap."""
    assert normalize_typos(f"my best {typo}") == "my best window"


# --------------------------------------------------------------------------- #
# what must NOT be rewritten — the reason this is not in `_CANONICAL`
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message", [
    "i am on windows 11",
    "does this run on windows",
    "the widow of the founder",
    "a wider window of time",
    "winnow it down",
    "its windy today",
])
def test_real_words_near_window_are_left_alone(message):
    assert normalize_typos(message) == message, message


def test_a_correctly_typed_window_survives():
    """`window` is not in `_CANONICAL`, so nothing protects it from being
    corrected TOWARD something else. Nothing in the vocabulary is within the
    cutoff today; this pins that, because a future addition could take it."""
    assert "window" in normalize_typos("whats my best window")


def test_the_archive_still_routes_the_same_way():
    """Re-measured over his 194 distinct messages after the change: 40 reach a
    capability, 3 of them timing — the two that always did plus his. Nothing
    moved to a different capability."""
    for message, expected in [
        ("when should i post", "timing"),
        ("when should i post today", "timing"),
        (HIS_MESSAGE, "timing"),
    ]:
        capability = find_capability(message)
        assert capability is not None and capability.key == expected, message
