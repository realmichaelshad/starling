"""The plural of "reply" is "replies", in a sentence he reads.

FOUND BY DRIVING, 2026-08-07. Asking the timing capability about replies:

    "At least 30 dated replys with this metric are needed before recommending
     a window."

`f"{post_type}s"` is right for three of the four post types — posts, quotes,
threads — and wrong for the one the account actually lives on. That is why it
survived: nothing pointed at the single value that breaks, and every other
reading of the same line looked correct.
"""
from __future__ import annotations

import pytest

from app.performance import plural_post_type


@pytest.mark.parametrize("post_type, expected", [
    ("reply", "replies"),
    ("post", "posts"),
    ("quote", "quotes"),
    ("thread", "threads"),
    (None, "posts"),
    ("", "posts"),
])
def test_every_post_type_pluralises(post_type, expected):
    assert plural_post_type(post_type) == expected


def test_the_shipped_sentence_reads_correctly():
    """The exact string the user saw, rebuilt."""
    sentence = (f"At least 30 dated {plural_post_type('reply')} with this "
                "metric are needed before recommending a window.")
    assert "replys" not in sentence
    assert "dated replies with" in sentence


def test_an_unknown_type_still_gets_a_plural():
    """A new post_type must not crash the sentence; naive is fine as a default,
    it is only "reply" that the naive rule gets wrong."""
    assert plural_post_type("repost") == "reposts"


def test_the_timing_answer_uses_the_helper():
    """Pinned POSITIVELY — the caller must go through the helper.

    The first version of this asserted the naive `f"{post_type}s"` appeared
    nowhere in the module, and failed on the helper's OWN fallback, which is
    where that expression correctly lives. Fourth absence-check to misfire on
    the thing it was protecting; a positive structural check cannot.
    """
    import inspect

    from app.performance import timing_recommendations

    source = inspect.getsource(timing_recommendations)
    assert "plural_post_type(post_type)" in source, (
        "timing_recommendations must build its plural through the helper; "
        "an inline f-string renders 'replys'")


def test_the_forecast_note_uses_the_helper_too():
    """The card read "You don't have enough replys yet" (the golden run): the forecast note builds its
    plural through the same helper, so there is one rule."""
    import inspect

    from app.intelligence import PersonalModel

    assert "plural_post_type(post_type)" in inspect.getsource(PersonalModel.predict)
