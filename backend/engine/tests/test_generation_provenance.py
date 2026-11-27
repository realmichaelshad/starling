"""PROVENANCE-1 - a generation records the lane and register it was planned in.

MIGRATION 14 CREATED THREE COLUMNS AND NOTHING WROTE THEM. Measured before this file existed:
2,249 rows, zero non-null in `topic_lane`, `topic_lane_score`, `post_register`.

THE VACUOUS ZERO IS THE POINT. An all-NULL column does not error - it ANSWERS. "Do my
vibe-coding posts do better than my frontier posts?" returns a clean empty result with no
reason to doubt it: the exact question the column exists for, answered wrongly, silently.
A column nothing writes is a column nobody notices is empty.
"""
from __future__ import annotations

import pytest

from app import storage


def _row(generation_id: str) -> dict:
    with storage.connect() as conn:
        return dict(conn.execute("SELECT * FROM generations WHERE id=?",
                                 (generation_id,)).fetchone())


def test_the_register_is_recorded_in_its_column():
    """It was RECOVERABLE but not recorded: `post_register` rides inside `request_json` and
    six gates read it off the request object, so the feature worked and only the COLUMN was
    lost. That is why this one can be fixed today with no other lane's help."""
    gid = storage.create_generation({"action": "post", "post_register": "shitpost",
                                     "context": {}}, {})
    assert _row(gid)["post_register"] == "shitpost"


def test_the_lane_is_recorded_when_the_caller_supplies_it():
    """Proves the COLUMN IS WIRED, independently of whether any caller fills it yet."""
    gid = storage.create_generation(
        {"action": "post", "context": {}, "topic_lane": "vibe coding",
         "topic_lane_score": 0.7}, {})
    row = _row(gid)
    assert row["topic_lane"] == "vibe coding"
    assert row["topic_lane_score"] == pytest.approx(0.7)


def test_the_lane_is_also_taken_from_the_strategy_payload():
    """Either caller shape works, so the lane lands whichever side threads it."""
    gid = storage.create_generation({"action": "post", "context": {}},
                                    {"topic_lane": "frontier models", "topic_lane_score": 1.0})
    row = _row(gid)
    assert row["topic_lane"] == "frontier models"
    assert row["topic_lane_score"] == pytest.approx(1.0)


def test_absent_values_stay_null_rather_than_becoming_a_default():
    """NULL means "no lane was recorded". A default would mean "this post was frontier",
    which is a fabricated fact and worse than the empty column this replaces."""
    row = _row(storage.create_generation({"action": "post", "context": {}}, {}))
    assert row["topic_lane"] is None
    assert row["topic_lane_score"] is None
    assert row["post_register"] is None


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP, NOT MINE TO CLOSE. The column is wired but NO CALLER SUPPLIES A LANE: "
           "it appears in neither `request_json` nor `strategy_json`, and `context_json` holds "
           "the parent post rather than the topic, so every press still loses which lane it "
           "was about and no backfill can recover it. Threading it lives in `main.py`, which "
           "is Lane Q's file (R-SCOPE-1). strict=True on purpose: when they land their half "
           "this XPASSES and FAILS the run, forcing this marker to be deleted rather than "
           "forgotten.")
def test_a_real_draft_request_carries_a_lane():
    """The shape `main.py` actually passes - a request with no lane threaded through."""
    gid = storage.create_generation(
        {"action": "post", "context": {}, "post_register": "shitpost",
         "direction": "write about the OpenRouter token usage story"},
        {"objective": "growth"})
    assert _row(gid)["topic_lane"] is not None
