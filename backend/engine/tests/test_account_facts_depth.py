"""What Starling may state about his own account — the parts that were wrong.

He asked what his most recent post was and got a post he had already deleted;
that is filtered at the source now. What was still wrong underneath it: the
answer clipped his post at 90 characters mid-word, never said WHEN, reported
his best post's clock in UTC while labelling it his own zone, and disclaimed
per-post likes and replies that were sitting on the same rows as the views.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import account_facts as af  # noqa: E402


LONG = ("this is the first time “AI agent” actually feels like delegating "
        "work instead of babysitting a chatbot, and the difference shows up in "
        "how little I re-read the output before shipping it somewhere real")


def _row(created="2026-07-22T23:09:34.000Z", text=LONG, **metrics):
    return {"created_at": created, "post_type": "post", "text": text,
            "metrics": metrics or {}}


def test_a_post_is_not_clipped_mid_word() -> None:
    line = af._post_line(_row())
    assert "babysitti\"" not in line["text"]
    # Either the whole post, or a clean break that reads as truncated.
    assert line["text"] == LONG or (
        line["text"].endswith("…") and not line["text"][-2].isspace())


def test_a_short_post_is_returned_whole() -> None:
    short = "this is the first time the “Jarvis” comparison feels legit"
    assert af._post_line(_row(text=short))["text"] == short


def test_every_post_line_says_how_long_ago() -> None:
    line = af._post_line(_row())
    assert line.get("posted_ago"), "a date with no gap makes him do the arithmetic"


def test_local_clock_is_the_users_zone_not_utc() -> None:
    """23:09 UTC on a Wednesday is 19:09 Eastern — same day, different clock.

    The bug was not the conversion, it was the LABEL: the UTC clock was printed
    with the user's zone name appended, which asserts something false.
    """
    line = af._post_line(_row(created="2026-07-22T23:09:34.000Z"),
                         "America/New_York")
    assert "(America/New_York)" in line["posted_local"]
    assert "19:09" in line["posted_local"]
    assert "23:09" not in line["posted_local"]


def test_interactions_are_summed_not_disclaimed() -> None:
    line = af._post_line(_row(views=1000, likes=3, replies=2, reposts=1))
    assert line["interactions"] == 6
    assert line["engagement_rate_pct"] == 0.6


def test_interaction_totals_span_the_account() -> None:
    rows = [_row(views=100, likes=2, replies=1, reposts=0),
            _row(views=200, likes=4, replies=0, reposts=2)]
    totals = af._interaction_totals(rows)
    assert totals["total_likes"] == 6
    assert totals["total_replies"] == 1
    assert totals["total_reposts"] == 2
    assert totals["posts_with_interactions"] == 2


def test_no_interactions_recorded_reports_nothing_rather_than_zero() -> None:
    assert af._interaction_totals([_row(views=100)]) == {"posts_with_interactions": 0}


@pytest.mark.parametrize("minutes,expected", [
    (0.5, "just now"), (45, "45 min ago"), (90, "1h ago"),
    (60 * 24, "24h ago"), (60 * 24 * 3, "3 days ago"),
])
def test_gap_is_stated_in_units_a_person_uses(minutes: float, expected: str) -> None:
    assert af._humanize_gap(minutes) == expected
