"""The reader-value bar, calibrated against writing he actually published.

Measured on 2026-07-26: the gate rejected 15 of his own 34 published posts as
having "no reason to read". A bar that throws out nearly half of what the
account owner chooses to publish is not measuring reader value — it is
measuring one shape of post, the long explained argument, and calling every
other shape empty.

The three shapes it was blind to, all of them his highest-value output:

  a dated call      "GPT-5.6 is expected on July 7"      — checkable, holdable
  a scoop           "Grok 4.5 is now live for Heavy users" — being first
  a first-hand find "OpenAI's own AI just told me ..."   — nobody else has it

Widening a quality bar is dangerous in one direction only, so the slop cases
below matter more than the archive cases: if any of them starts passing, the
bar has stopped meaning anything.
"""
from __future__ import annotations

import pytest

from app.reader_value import WEAK_THRESHOLD, reader_value


def score(text: str, action: str = "post") -> float:
    return reader_value(text, action)["score"]


@pytest.mark.parametrize("text", [
    "GPT-5.6 is expected on July 7, and GPT-5.5 shipped two weeks early.",
    "My Claude Opus 5 prediction: July 30.",
    "Tibo is going to release Codex tomorrow, then Claude drops on Thursday.",
])
def test_a_dated_call_is_a_carryable_specific(text: str) -> None:
    assert score(text) >= WEAK_THRESHOLD, text


@pytest.mark.parametrize("text", [
    "GPT-5.6 is already showing up for select users in the Codex app.",
    "Grok 4.5 is now live for Heavy users.",
    "Polymarket just nuked the odds of a July 23 Opus 5 release.",
])
def test_being_first_with_a_checkable_status_is_worth_reading(text: str) -> None:
    assert score(text) >= WEAK_THRESHOLD, text


@pytest.mark.parametrize("text", [
    "OpenAI's own AI just told me the official OpenAI account was fake.",
    "Turns out people would rather talk to their agents than babysit a terminal.",
])
def test_a_first_hand_finding_is_worth_reading(text: str) -> None:
    assert score(text) >= WEAK_THRESHOLD, text


def test_declaring_what_he_will_do_counts_as_a_stake():
    assert reader_value("If this is true, I'm switching to GPT-5.6.")["signals"]["has_stake"]


@pytest.mark.parametrize("text", [
    "This is a really interesting development and it will be fascinating to see "
    "how it all plays out over time.",
    "Big if true.",
    "This changes everything for the industry.",
    "Exciting times ahead in AI!",
    "I think this is huge for everyone.",
    "Really excited to see where this goes next.",
    "Great thread, thanks for sharing!",
    "This is the future.",
])
def test_slop_is_still_rejected(text: str) -> None:
    """The direction that matters. Widening the bar must not open it."""
    assert score(text) < WEAK_THRESHOLD, text


def test_a_status_claim_about_nothing_is_not_a_scoop():
    """'It's live!' about no named thing is not news."""
    assert score("it is now live and I am excited") < WEAK_THRESHOLD


def test_the_bar_accepts_most_of_what_he_publishes():
    """A regression guard on the calibration itself, not on any one post."""
    from app.storage import performance_records
    rows = [row for row in performance_records() if row.get("text")]
    if len(rows) < 20:
        pytest.skip("not enough archive to calibrate against")
    rejected = sum(1 for row in rows
                   if score(str(row["text"]), row.get("post_type") or "post")
                   < WEAK_THRESHOLD)
    assert rejected / len(rows) <= 0.30, (
        f"the bar rejects {rejected}/{len(rows)} of his own published posts; "
        f"it was 15/34 before calibration and must not drift back")
