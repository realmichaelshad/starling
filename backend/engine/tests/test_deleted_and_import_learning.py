"""Posted-then-deleted teaches, and an import lands visibly.

Two things his 2026-07-31 follow-up asked for:

- "let's say it was posted then deleted" — a post that was LIVE and then
  taken down is the strongest style-rejection signal the product can
  observe. It must stop counting as history everywhere, AND start counting
  as a negative. Only the archive export's own `deleted_tweet` records were
  reaching the scorer before this; a post he pulled from his own timeline
  taught nothing at all.
- "the import thing in settings — make sure that also helps the learning
  phase" — an import must report what the corpus now teaches, not just how
  many rows it wrote.
"""
from __future__ import annotations

import base64
import io
import zipfile

from app.storage import (
    deleted_post_texts,
    mark_post_deleted,
    upsert_archive_records,
    upsert_post,
)

REJECTED = "Hot take that did not survive ten minutes on the timeline!!!"
KEPT = "Benchmarks only matter when the harness is public and the runs repeat."


def zipped(files: dict[str, str | bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return output.getvalue()


def _posted_then_deleted() -> None:
    upsert_post({"post_id": "kept-1", "post_type": "post", "text": KEPT,
                 "created_at": "2026-07-20T10:00:00+00:00"})
    upsert_post({"post_id": "gone-1", "post_type": "post", "text": REJECTED,
                 "created_at": "2026-07-21T10:00:00+00:00"})
    assert mark_post_deleted("gone-1") is True


# --- a deletion is a rejection signal -------------------------------------- #

def test_a_live_deleted_post_becomes_a_rejection_example():
    _posted_then_deleted()
    texts = deleted_post_texts()
    assert REJECTED in texts
    assert KEPT not in texts


def test_the_scorer_sees_both_kinds_of_deletion_without_duplicates():
    _posted_then_deleted()
    upsert_archive_records("archive.zip", [
        {"key": "d1", "text": "An old tweet the archive says was deleted.",
         "created_at": "2026-01-01T00:00:00+00:00", "payload": {}},
    ], "deleted_tweet")

    from app.intelligence import PersonalModel
    deleted = PersonalModel().deleted_texts
    assert REJECTED in deleted, "a post he pulled from his own timeline is the strongest signal"
    assert any("archive says was deleted" in text for text in deleted)
    assert KEPT not in deleted
    assert len(deleted) == len(set(deleted))


# RETIRED (API-native pass, 2026-09-22): test_a_deleted_post_is_a_negative_not_a_positive_in_training_data: the local archive import and the training-dataset export left the hosted engine.


def test_a_deleted_post_never_sets_the_bar_for_the_next_one():
    """Rewards are percentiles against the account's own history, and pace is
    compared to its own median — a post he deleted is in neither."""
    upsert_post({"post_id": "live-1", "post_type": "post", "text": KEPT,
                 "created_at": "2026-07-20T10:00:00+00:00",
                 "metrics": {"views": 100}, "metrics_updated_at": "2026-07-21T10:00:00+00:00"})
    upsert_post({"post_id": "viral-gone", "post_type": "post", "text": REJECTED,
                 "created_at": "2026-07-20T10:00:00+00:00",
                 "metrics": {"views": 999999}, "metrics_updated_at": "2026-07-21T10:00:00+00:00"})
    from app.learning_loop import account_velocity_baseline
    with_deleted = account_velocity_baseline("post")
    assert mark_post_deleted("viral-gone") is True
    without = account_velocity_baseline("post")
    assert without["sample"] == with_deleted["sample"] - 1
    assert without["views_per_hour_median"] < with_deleted["views_per_hour_median"]


# --- the settings import feeds the learning phase --------------------------- #

def _pack_with(posts: list[tuple[str, str, str]]) -> str:
    entries = ",".join(
        '{"tweet":{"id_str":"%s","full_text":"%s","created_at":"%s","entities":{}}}' % row
        for row in posts)
    x_archive = zipped({"data/tweets.js": f"window.YTD.tweets.part0 = [{entries}];"})
    return base64.b64encode(zipped({"voice-training-pack/x-archive.zip": x_archive})).decode()


# RETIRED (API-native pass, 2026-09-22): test_import_reports_what_the_corpus_now_teaches: the local archive import and the training-dataset export left the hosted engine.


# RETIRED (API-native pass, 2026-09-22): test_import_counts_the_rejection_examples_it_can_see: the local archive import and the training-dataset export left the hosted engine.


# RETIRED (API-native pass, 2026-09-22): test_a_one_post_import_reports_its_learning_effect: the local archive import and the training-dataset export left the hosted engine.


# RETIRED (API-native pass, 2026-09-22): test_a_failed_summary_never_fails_the_import: the local archive import and the training-dataset export left the hosted engine.

