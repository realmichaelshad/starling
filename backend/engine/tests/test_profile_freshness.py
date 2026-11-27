"""The profile must know whether it is current, and say when it was built.

Two defects, both found by reading his live rows rather than the code:

  * `corpus_version()` is an IN-PROCESS counter starting at 0
    (`storage._corpus_version`), and `ensure_profile` compared it to a value
    persisted by a different process. 18 of the 42 stored rows on his machine
    hold 0 - including three written at 21:02, 21:19 and 21:20 whose evidence
    stops at 03:33. A fresh process matched 0 == 0 and served a profile built
    from an older corpus; and once the running process wrote anything the
    counter could never equal a stored 0, so the cache was lost entirely and the
    profile was rebuilt on every call. Wrong in both directions.

  * `built_at` was hardcoded None on the path that actually builds, while the
    cached path returns the stored timestamp - so a freshly built profile
    reported no build time and nothing reading it could judge freshness.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from app.profile import _corpus_has_moved, build
from app.storage import mark_corpus_dirty, upsert_post


def _stored_like(posts: int, evidence: str | None) -> dict:
    return {"corpus_version": 0, "evidence_through": evidence,
            "profile": {"counts": {"authored_posts": posts}}, "gaps": []}


def _seed_one_post() -> None:
    now = datetime.now(timezone.utc)
    upsert_post({"post_id": "fresh-1", "post_type": "post",
                 "created_at": (now - timedelta(hours=1)).isoformat(),
                 "first_observed_at": (now - timedelta(hours=1)).isoformat(),
                 "text": "a seeded post, long enough to read back properly",
                 "metrics": {"views": 100, "likes": 1},
                 "metrics_updated_at": now.isoformat()})


def test_a_freshly_built_profile_records_when_it_was_built() -> None:
    _seed_one_post()
    built = build(save=False)
    assert built["built_at"], "built_at is still None on the build path"
    # Parseable, and not in the future.
    stamp = datetime.fromisoformat(built["built_at"])
    assert stamp <= datetime.now(timezone.utc) + timedelta(seconds=5)


def test_an_in_process_write_does_not_invalidate_a_current_profile() -> None:
    """The lost-cache direction: bumping the counter must not force a rebuild
    when the corpus itself is unchanged."""
    _seed_one_post()
    built = build(save=False)
    stored = _stored_like(
        built["profile"]["counts"]["authored_posts"], built["evidence_through"])
    assert _corpus_has_moved(stored) is False
    mark_corpus_dirty()
    mark_corpus_dirty()
    assert _corpus_has_moved(stored) is False, (
        "an in-process counter bump still decides cache validity")


def test_a_corpus_that_really_moved_is_detected() -> None:
    """The stale-serve direction, which a matching 0 == 0 used to wave through."""
    _seed_one_post()
    built = build(save=False)
    posts = built["profile"]["counts"]["authored_posts"]

    fewer = _stored_like(posts - 1, built["evidence_through"])
    assert _corpus_has_moved(fewer) is True, "a changed post count went unnoticed"

    older = _stored_like(posts, "2000-01-01T00:00:00Z")
    assert _corpus_has_moved(older) is True, "an older observation went unnoticed"


def test_a_stored_row_that_cannot_prove_itself_is_rebuilt() -> None:
    _seed_one_post()
    built = build(save=False)
    posts = built["profile"]["counts"]["authored_posts"]
    assert _corpus_has_moved(_stored_like(posts, None)) is True
    blank = copy.deepcopy(_stored_like(posts, built["evidence_through"]))
    blank["profile"]["counts"] = {}
    assert _corpus_has_moved(blank) is True
