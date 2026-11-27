"""The customer's export carries their drafts, ratings and queue (CONTRACTS 3.3)."""
from __future__ import annotations

from app import storage

EXPECTED_KEYS = {
    "account", "posts", "profile_snapshots", "metric_snapshots", "goals", "campaigns", "experiments",
    "knowledge_sources", "strategic_state", "preferences", "opportunities",
    "generations", "generation_candidates", "scheduled_drafts", "user_sources", "feedback",
    "image_feedback", "topic_saves",
}


def test_export_bundle_covers_every_customer_owned_table():
    assert EXPECTED_KEYS <= set(storage.export_bundle())


def test_export_bundle_contains_the_queue_and_the_ratings():
    storage.save_scheduled_draft({"text": "a held draft", "target_at": "2026-09-19T10:00:00Z", "action": "post"})
    storage.save_feedback("cand-x", "kept", "the text the customer kept", None)

    bundle = storage.export_bundle()
    assert [row["text"] for row in bundle["scheduled_drafts"]] == ["a held draft"]
    assert [row["final_text"] for row in bundle["feedback"]] == ["the text the customer kept"]
    assert [row["action"] for row in bundle["feedback"]] == ["kept"]
