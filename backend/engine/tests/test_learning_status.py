"""The learning surface is inspectable: counts, pace, beats — no black box.

Every claim the online-learning work makes about itself has to be visible
somewhere the user can read it, or it is a silent behavior change.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.encoders import jsonable_encoder

from app.account_model import reset_topic_dimension_cache
from app.storage import (
    mark_post_deleted,
    observed_post_counts,
    record_observed_post,
    upsert_post,
)
from app.user_learning import learning_status


def _seed_corpus() -> None:
    upsert_post({"post_id": "arch-1", "post_type": "post", "text": "An imported archive post."})
    record_observed_post({"post_id": "ext-1", "post_type": "post",
                          "text": "A post he wrote on his phone.", "origin": "external"})
    record_observed_post({"post_id": "sv-1", "post_type": "post",
                          "text": "A Starling draft he published.",
                          "origin": "starling_verified"})
    upsert_post({"post_id": "gone-1", "post_type": "post", "text": "A post he deleted."})
    mark_post_deleted("gone-1")


def test_observed_post_counts_split_by_origin():
    _seed_corpus()
    counts = observed_post_counts()
    assert counts["archive"] == 1
    assert counts["external"] == 1
    assert counts["starling_verified"] == 1
    assert counts["deleted"] == 1
    assert counts["total"] == 3


def test_learning_status_carries_the_learning_surface():
    _seed_corpus()
    reset_topic_dimension_cache()
    status = learning_status()
    # The pre-existing keys are untouched.
    assert "topic_memory" in status
    assert "note" in status
    assert status["observed_posts"]["external"] == 1
    assert status["approach"]["state"] in ("stable", "watching", "shifted")
    assert status["approach"]["half_life_days"] in (30.0, 90.0)
    assert "count" in status["dimensions"]
    assert status["voice_weighting"]["starling_verified_factor"] == 0.5


def test_learning_status_survives_json_encoding():
    """The `/learning/status` route that served this is gone. The function
    remains, and what it returns must still encode the way the route encoded
    it, with the totals intact."""
    _seed_corpus()
    reset_topic_dimension_cache()
    payload = json.loads(json.dumps(jsonable_encoder(learning_status())))
    assert payload["observed_posts"]["total"] == 3
    assert payload["approach"]["state"]
    assert "topic_memory" in payload


def test_profile_carries_approach_and_real_pillars():
    for index in range(8):
        upsert_post({"post_id": f"p-{index}", "post_type": "post",
                     "text": f"Benchmark notes {index}: the model eval harness matters more than the score.",
                     "created_at": (datetime.now(timezone.utc) - timedelta(days=index)).isoformat()})
    reset_topic_dimension_cache()
    from app import profile as profile_mod
    built = profile_mod.build(save=True)
    assert built["profile"]["approach"]["state"] in ("stable", "watching", "shifted")
    assert built["profile"]["audience_map"]["pillars"]
    for dimension in built["profile"]["audience_map"]["dimensions"]:
        assert "centroid" not in dimension
    # STRICTLY serializable — no default=str fallback quietly storing a
    # stringified numpy array nothing can read back (4.8KB per version).
    json.dumps(built)
    # ...while the in-memory profile keeps the centroids matching needs.
    from app.account_model import personal_topic_profile
    live = personal_topic_profile()
    if live["clusters"]:
        assert any("_centroid" in cluster for cluster in live["clusters"])
