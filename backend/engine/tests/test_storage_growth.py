from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app import storage


def test_migrations_preserve_legacy_posts(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE posts(post_id TEXT PRIMARY KEY,post_url TEXT,created_at TEXT,post_type TEXT,text TEXT NOT NULL,
        parent_post_id TEXT,parent_text TEXT,media_count INTEGER DEFAULT 0,has_image INTEGER DEFAULT 0,
        metrics_json TEXT DEFAULT '{}',source_file TEXT,imported_at TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE feedback(id INTEGER PRIMARY KEY AUTOINCREMENT,candidate_id TEXT NOT NULL,action TEXT NOT NULL,final_text TEXT,context_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE reference_images(filename TEXT PRIMARY KEY,source_url TEXT,rights_status TEXT,notes TEXT);
      CREATE TABLE topic_saves(id INTEGER PRIMARY KEY AUTOINCREMENT,context_json TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
      INSERT INTO posts(post_id,text) VALUES('legacy','still here');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.initialize()
    assert storage.get_post("legacy")["text"] == "still here"
    with storage.connect() as migrated:
        assert "features_json" in {row[1] for row in migrated.execute("PRAGMA table_info(posts)")}
        assert migrated.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == len(storage.MIGRATIONS)
        assert migrated.execute("SELECT name FROM sqlite_master WHERE name='generations'").fetchone()


def test_growth_entities_and_generation_lifecycle_are_durable():
    profile = storage.set_account_profile({"audience": "AI builders", "content_pillars": ["agents"], "timezone": "America/New_York"})
    assert profile["content_pillars"] == ["agents"]
    goal = storage.create_goal({"name": "Grow useful conversations", "objective": "conversation", "baseline": 10, "target": 25,
        "horizon_at": "2026-12-01T00:00:00+00:00", "weekly_cadence": {"posts": 4}, "guardrails": ["no bait"]})
    campaign = storage.create_campaign({"name": "Agent notes", "goal_id": goal["id"], "narrative": "Build in public", "content_pillars": ["agents"]})
    experiment = storage.create_experiment({"name": "Hook style", "campaign_id": campaign["id"], "hypothesis": "Questions invite useful replies",
        "variable": "hook", "variants": ["question", "statement"], "primary_metric": "conversation", "min_sample_size": 8})
    assert storage.update_experiment(experiment["id"], {"status": "running"})["started_at"]
    generation_id = storage.create_generation({"action": "post", "context": {"author": "me", "text": "agents"}}, {"objective": "conversation"})
    storage.save_generation_candidates(generation_id, [{"id": "candidate-1", "text": "A useful draft"}])
    storage.save_feedback("candidate-1", "edited", "A better useful draft", None, generation_id)
    outcome = storage.record_outcome({"generation_id": generation_id, "candidate_id": "candidate-1", "status": "posted",
        "final_text": "A better useful draft", "published_post_id": "900", "posted_at": datetime.now(timezone.utc), "metadata": {}})
    assert outcome["lifecycle_status"] == "posted"
    assert outcome["published_post_id"] == "900"
    assert storage.learning_examples()[0] == "A better useful draft"


def test_metric_snapshots_preserve_missing_fields():
    storage.upsert_post({"post_id": "1", "text": "hello", "created_at": "2026-01-01T00:00:00+00:00", "metrics": {}})
    stored = storage.save_metric_snapshot({"post_id": "1", "captured_at": "2026-01-02T00:00:00+00:00", "followers_count": 100,
        "metrics": {"impressions": 500}, "source": "manual", "metadata": {}})
    assert stored["metrics"] == {"impressions": 500}
    assert "likes" not in stored["metrics"]


def test_visible_reply_match_backfills_parent_and_sparse_recapture_cannot_erase_it():
    generation_id = storage.create_generation(
        {"action": "reply", "context": {"post_id": "source-1", "author": "A", "text": "A source post", "visibility": "public"}},
        {"objective": "conversation"},
    )
    storage.save_generation_candidates(generation_id, [{"id": "reply-candidate", "text": "A clear point about the tradeoff."}])
    storage.save_feedback("reply-candidate", "inserted", None, None, generation_id)
    # A profile capture can see the authored reply but not its parent status.
    storage.record_observed_post({
        "post_id": "visible-reply", "post_type": "reply", "text": "A clear point about the tradeoff.",
        "origin": "external",
    })

    assert storage.match_visible_post_to_generation(
        "visible-reply", "  a CLEAR point about the tradeoff.  ", "reply",
        "2026-07-15T12:00:00+00:00", "extension",
    ) == generation_id
    matched = storage.get_generation(generation_id)
    assert matched["lifecycle_status"] == "posted"
    assert matched["published_post_id"] == "visible-reply"
    assert matched["events"][-1]["event"] == "matched_visible_post"
    assert storage.get_post("visible-reply")["parent_post_id"] == "source-1"
    assert storage.find_recorded_reply_to_post("source-1")["post_id"] == "visible-reply"

    # A later low-detail profile capture must not clear the known relation or
    # downgrade a reply into a generic post.
    storage.record_observed_post({
        "post_id": "visible-reply", "post_type": "post", "text": "A clear point about the tradeoff.",
        "origin": "external",
    })
    stored = storage.get_post("visible-reply")
    assert stored["post_type"] == "reply"
    assert stored["parent_post_id"] == "source-1"

    wrong_format = storage.create_generation(
        {"action": "post", "context": {"author": "You", "text": "An original thought", "visibility": "public"}},
        {"objective": "reach"},
    )
    storage.save_generation_candidates(wrong_format, [{"id": "post-candidate", "text": "Same words, wrong format."}])
    storage.save_feedback("post-candidate", "inserted", None, None, wrong_format)
    assert storage.match_visible_post_to_generation("not-a-reply", "Same words, wrong format.", "reply") is None
    assert storage.get_generation(wrong_format)["lifecycle_status"] == "inserted"


def test_recorded_reply_lookup_uses_exact_parent_identity_and_survives_restart():
    storage.record_observed_post({
        "post_id": "my-reply", "post_type": "reply", "parent_post_id": "source-42",
        "parent_text": "The source post", "text": "My earlier reply.",
        "created_at": "2026-07-22T12:00:00+00:00", "origin": "external",
    })
    found = storage.find_recorded_reply_to_post("source-42")
    assert found and found["post_id"] == "my-reply"
    assert storage.find_recorded_reply_to_post("another-source") is None


def test_recorded_reply_lookup_also_covers_posted_starling_generations():
    generation_id = storage.create_generation(
        {"action": "reply", "context": {"post_id": "source-43", "author": "A", "text": "Source"}},
        {"objective": "conversation"},
    )
    storage.record_outcome({"generation_id": generation_id, "status": "posted",
                            "final_text": "The reply already sent.", "published_post_id": "reply-43"})
    found = storage.find_recorded_reply_to_post("source-43")
    assert found and found["generation_id"] == generation_id


def test_curated_primary_sources_and_strategy_versions_are_seeded():
    sources = storage.list_knowledge_sources(compatible_only=False)
    urls = {source["url"] for source in sources}
    assert "https://github.com/xai-org/x-algorithm" in urls
    assert "https://help.x.com/en/rules-and-policies/authenticity" in urls
    assert all(source["evidence_grade"] == "A" for source in sources if source["url"] in urls and source["publisher"] in ("X / xAI", "X Business", "X Help Center"))
    official = next(source for source in sources if source["url"] == "https://github.com/xai-org/x-algorithm")
    assert official["retrieved_at"] and official["review_after"] and official["applicability"]

    first = storage.save_strategic_state({"recommended_action": "post", "reason": "test"}, "test")
    second = storage.save_strategic_state({"recommended_action": "post", "reason": "test"}, "test")
    assert first["id"] == second["id"]
    assert storage.latest_strategic_state()["state"]["recommended_action"] == "post"
