"""The learning corpus is the non-deleted archive, and nothing erases it.

Four hygiene rules behind the online-learning work, each of which was
observed broken:
- deleting a post must invalidate the corpus caches (it did not bump
  corpus_version, so voice/shape/lane kept serving the deleted post);
- a passive re-sighting sends metrics:{} and must not blank the stored
  metrics the learning loop's rewards read;
- shape silhouettes, lane tokens and duplication checks must not include
  deleted posts;
- an edited post with an unchanged row count must still refresh the
  shape/lane caches (they keyed on len(posts) alone).
"""
from __future__ import annotations

from app import shape as shape_mod
from app import subject as subject_mod
from app.account_model import infer_account_model
from app.storage import (
    corpus_version,
    get_post,
    mark_post_deleted,
    record_observed_post,
    upsert_post,
)


def _reset_local_caches() -> None:
    shape_mod.reset_shape_cache()
    subject_mod._LANE_CACHE = None


def test_mark_post_deleted_bumps_corpus_version():
    upsert_post({"post_id": "del-1", "text": "A post that will be deleted.", "post_type": "post"})
    before = corpus_version()
    assert mark_post_deleted("del-1") is True
    assert corpus_version() > before
    # Second delete is a no-op and must not pretend the corpus moved.
    settled = corpus_version()
    assert mark_post_deleted("del-1") is False
    assert corpus_version() == settled


def test_resighting_with_empty_metrics_keeps_stored_metrics():
    upsert_post({"post_id": "m-1", "text": "Post with measured reach.",
                 "post_type": "post", "metrics": {"views": 500, "likes": 12}})
    # Exactly what a passive profile re-sighting sends: identity, no numbers.
    upsert_post({"post_id": "m-1", "text": "Post with measured reach.",
                 "post_type": "post", "metrics": {}})
    assert (get_post("m-1") or {}).get("metrics") == {"views": 500, "likes": 12}


def test_observed_resighting_keeps_metrics_and_origin():
    upsert_post({"post_id": "m-2", "text": "Hand-written post seen with metrics.",
                 "post_type": "post", "metrics": {"views": 90}})
    record_observed_post({"post_id": "m-2", "text": "Hand-written post seen with metrics.",
                          "post_type": "post", "metrics": {},
                          "origin": "external", "author_handle": "example_builder"})
    stored = get_post("m-2") or {}
    assert stored.get("metrics") == {"views": 90}
    assert stored.get("origin") == "external"
    assert stored.get("first_observed_at")


def test_deleted_posts_leave_shape_lane_and_voice():
    _reset_local_caches()
    upsert_post({"post_id": "keep", "post_type": "post",
                 "text": "Benchmarks only matter when the harness is honest."})
    upsert_post({"post_id": "gone", "post_type": "post",
                 "text": "Spam Blast giveaway!!! \U0001F525\U0001F525 #win #free"})
    assert mark_post_deleted("gone") is True

    distribution = shape_mod.account_shape_distribution()
    assert distribution["sample"] == 1

    tokens = subject_mod.account_core_tokens()
    assert "blast" not in tokens

    model = infer_account_model()
    assert model["confidence"]["post_count"] == 1
    assert model["voice_profile"]["emoji_rate"] == 0.0
    assert model["voice_profile"]["style"]["hashtag_rate"] == 0.0


def test_recent_own_posts_excludes_deleted():
    from app.main import recent_own_posts

    upsert_post({"post_id": "r-1", "post_type": "post", "text": "Still standing post."})
    upsert_post({"post_id": "r-2", "post_type": "post", "text": "Deleted afterthought."})
    mark_post_deleted("r-2")
    texts = [row["text"] for row in recent_own_posts(10)]
    assert "Still standing post." in texts
    assert "Deleted afterthought." not in texts


def test_same_count_edit_refreshes_shape_cache():
    _reset_local_caches()
    upsert_post({"post_id": "edit-1", "post_type": "post", "text": "One tight line."})
    first = shape_mod.account_shape_distribution()
    assert first["sample"] == 1
    # Same post_id, new body: len(posts) is unchanged, corpus_version is not.
    upsert_post({"post_id": "edit-1", "post_type": "post",
                 "text": "First line of a bigger take.\n\nSecond block with the actual point spelled out."})
    second = shape_mod.account_shape_distribution()
    assert second["shapes"] != first["shapes"]
