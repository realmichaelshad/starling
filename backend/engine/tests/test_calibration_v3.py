"""V3: a quality gate that fires on a writer's OWN posts is a note for that writer, not a verdict.

The gates were calibrated on one author's 35 posts. Run over other writers' own published
posts they flagged 17 of 25 (terse) and 12 of 20 (long-form). Per-account calibration runs
them over this account's own posts; one that fires on more than 15% becomes note-only. The
integrity gates are never touched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import calibration, intelligence
from app.storage import upsert_post

NOW = datetime.now(timezone.utc)
TERSE = [
    "shipping the boring version first is still undefeated",
    "every agent demo is a happy path",
    "benchmarks tell you what a model can do once",
    "latency is a feature people notice before quality",
    "most ai features should be a button, not a chat box",
    "typing speed was never the bottleneck. deciding was",
    "open weights are great until you need them fast",
    "rag is search with extra steps",
    "a model that says i don't know is worth two that guess",
    "the best developer tool this year was a faster test suite",
    "if your prompt needs a paragraph of rules, your task is two tasks",
]


@pytest.fixture(autouse=True)
def _fresh():
    calibration.reset()
    yield
    calibration.reset()


def _seed(texts, post_type="post"):
    for index, text in enumerate(texts):
        upsert_post({"post_id": f"{post_type}{index}", "text": text, "post_type": post_type,
                     "created_at": (NOW - timedelta(days=index + 1)).isoformat(), "visibility": "public"})


def test_a_gate_this_writer_trips_all_the_time_becomes_a_note():
    _seed(TERSE)
    rate = calibration.issue_rate("names_nothing", "post")
    assert rate is not None and rate > calibration.NOTE_ONLY_RATE
    assert calibration.note_only("names_nothing", "post")
    blocking, notes = calibration.split_issues(["names_nothing", "copied_voice_example"], "post")
    assert blocking == ["copied_voice_example"] and notes == ["names_nothing"]


def test_integrity_gates_never_become_notes():
    _seed(TERSE)
    for issue in ("engagement_bait", "copied_voice_example", "copies_the_post",
                  "leaked_into_reply", "misattributed_model"):
        assert not calibration.note_only(issue, "post"), issue
        assert not calibration.note_only(issue, "reply"), issue


def test_a_thin_archive_keeps_the_global_calibration():
    _seed(TERSE[:4])
    assert calibration.issue_rate("names_nothing", "post") is None
    assert not calibration.note_only("names_nothing", "post")


# RETIRED (API-native pass, 2026-09-22): test_the_retry_loop_stops_asking_for_what_this_writer_never_does: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


def test_a_writer_who_uses_dashes_keeps_them():
    dashed = [f"the fix wasn't a bigger model — it was moving step {i} earlier" for i in range(12)]
    _seed(dashed)
    intelligence.reset_personal_model_cache()
    from app.account_model import reset_topic_profile_cache
    reset_topic_profile_cache()
    calibration.reset()
    assert calibration.keeps_dashes()
    assert not intelligence._off_register("caching changed the bill — more than the model did")
