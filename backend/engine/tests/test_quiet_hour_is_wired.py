"""The quiet-hour wait has to be REACHED, and reached before the work.

`timing_verdict` is the cautionary tale this file exists to prevent repeating:
it computes a real answer, it is exposed at `/timing/verdict`, the planner and
the scheduler both call it — and the create-post path never did. It was correct
and inert for as long as it existed.
"""
import pathlib
import re

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"


def _source() -> str:
    return _MAIN.read_text(encoding="utf-8")


def test_the_create_path_asks_about_the_hour():
    assert "posting_hours" in _source(), (
        "nothing on the composer path consults the account's posting hours")


# RETIRED (API-native pass, 2026-09-22): test_it_asks_before_it_spends_a_draft: the quiet hour is a prior on the card now; the post is drafted (test_api_native_paths.py).


def test_the_wait_is_posts_only():
    """A reply is answered on the other person's clock, not this account's."""
    source = _source()
    window = source[source.index("from .posting_hours import hour_verdict") - 1400:
                    source.index("from .posting_hours import hour_verdict")]
    assert 'creating and action == "post"' in window, (
        "the quiet-hour gate is not scoped to posts")


def test_saying_wait_never_means_no():
    """The card offers an override, so the override has to exist."""
    from app.posting_hours import wants_it_anyway
    source = _source()
    assert "wants_it_anyway" in source, (
        "the wait card promises 'say so and I'll write it' with nothing behind it")
    for asked in ("write it anyway", "post it now", "i don't care, do it",
                  "go ahead", "ignore the time"):
        assert wants_it_anyway(asked), asked
    for ordinary in ("", "write about the new Claude release",
                     "what should I post about today"):
        assert not wants_it_anyway(ordinary), ordinary


# RETIRED (API-native pass, 2026-09-22): test_the_wait_card_carries_its_evidence: same: the reason and resume time ride on the drafted post's card.


def test_a_timing_opinion_never_costs_a_draft():
    """Every failure mode here degrades to drafting, never to silence."""
    source = _source()
    block = source[source.index("from .posting_hours import hour_verdict"):][:1200]
    assert "except Exception" in block, (
        "an exception in the timing check would take the whole draft down")
