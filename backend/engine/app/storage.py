from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .security import APP_DIR, ensure_app_dir

DB_PATH = APP_DIR / "starling.db"

# In-process corpus version: bumped whenever a write could change what the
# PersonalModel would learn from, so its cache invalidates without polling.
_corpus_lock = threading.Lock()
_corpus_version = 0


def mark_corpus_dirty() -> None:
    global _corpus_version
    with _corpus_lock:
        _corpus_version += 1


def corpus_version() -> int:
    with _corpus_lock:
        return _corpus_version


CURATED_KNOWLEDGE_SOURCES = (
    {
        "id": "official-x-algorithm",
        "url": "https://github.com/xai-org/x-algorithm",
        "title": "X Recommendation Algorithm",
        "publisher": "X / xAI",
        "published_at": "2026-05-15",
        "claim": "The current For You system combines in-network and out-of-network candidates and predicts several engagement outcomes. Treat those signals as context, never as a recipe or a promise of reach.",
        "evidence_grade": "A",
        "tags": ["reach", "conversation", "resonance", "recommendations", "official"],
        "source_version": "repository snapshot reviewed 2026-07-14",
        "applicability": "Use to understand broad recommendation inputs and constraints; personal chronological evidence still decides draft ranking.",
        "review_after": "2026-08-14T00:00:00+00:00",
    },
    {
        "id": "official-x-organic-best-practices",
        "url": "https://business.x.com/en/basics/organic-best-practices",
        "title": "Organic best practices",
        "publisher": "X Business",
        "published_at": None,
        "claim": "Official organic guidance favors concise, conversational posts with a clear point and selective use of media or a call to action when it genuinely fits.",
        "evidence_grade": "A",
        "tags": ["reach", "resonance", "writing", "media", "official"],
        "source_version": "web page reviewed 2026-07-14",
        "applicability": "Use as a baseline writing heuristic, then test it against the account's own audience and formats.",
        "review_after": "2026-10-14T00:00:00+00:00",
    },
    {
        "id": "official-x-community-management",
        "url": "https://business.x.com/en/basics/community-management",
        "title": "Community management",
        "publisher": "X Business",
        "published_at": None,
        "claim": "Timely, human, useful replies and participation in relevant conversations can create stronger discussion and follower opportunities.",
        "evidence_grade": "A",
        "tags": ["conversation", "followers", "replies", "community", "official"],
        "source_version": "web page reviewed 2026-07-14",
        "applicability": "Use for relevant conversation opportunities, never bulk or repetitive replies.",
        "review_after": "2026-10-14T00:00:00+00:00",
    },
    {
        "id": "official-x-authenticity",
        "url": "https://help.x.com/en/rules-and-policies/authenticity",
        "title": "Authenticity policy",
        "publisher": "X Help Center",
        "published_at": None,
        "claim": "Bulk or duplicative replies, irrelevant trend use, engagement exchanges, follow churn, and aggressive automation are prohibited or restricted behaviors.",
        "evidence_grade": "A",
        "tags": ["policy", "safety", "followers", "conversation", "official"],
        "source_version": "policy page reviewed 2026-07-14",
        "applicability": "Hard guardrail for every recommendation, experiment, and bridge action.",
        "review_after": "2026-08-14T00:00:00+00:00",
    },
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _from_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


@contextmanager
def connect():
    ensure_app_dir()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS posts (
      post_id TEXT PRIMARY KEY,
      post_url TEXT,
      created_at TEXT,
      post_type TEXT,
      text TEXT NOT NULL,
      parent_post_id TEXT,
      parent_text TEXT,
      media_count INTEGER DEFAULT 0,
      has_image INTEGER DEFAULT 0,
      metrics_json TEXT DEFAULT '{}',
      source_file TEXT,
      imported_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS reference_images (
      filename TEXT PRIMARY KEY,
      source_url TEXT,
      rights_status TEXT,
      notes TEXT
    );
    CREATE TABLE IF NOT EXISTS feedback (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      candidate_id TEXT NOT NULL,
      action TEXT NOT NULL,
      final_text TEXT,
      context_id TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS topic_saves (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      context_json TEXT NOT NULL,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)


def _migration_2(conn: sqlite3.Connection) -> None:
    for name, declaration in (
        ("parent_author", "TEXT"), ("parent_post_url", "TEXT"), ("quoted_post_id", "TEXT"),
        ("quote_text", "TEXT"), ("thread_json", "TEXT DEFAULT '[]'"), ("media_json", "TEXT DEFAULT '[]'"),
        ("language", "TEXT"), ("visibility", "TEXT DEFAULT 'public'"), ("features_json", "TEXT DEFAULT '{}'"),
        ("metrics_updated_at", "TEXT"),
    ):
        _ensure_column(conn, "posts", name, declaration)
    for name, declaration in (("generation_id", "TEXT"), ("metadata_json", "TEXT DEFAULT '{}'")):
        _ensure_column(conn, "feedback", name, declaration)
    for name, declaration in (("title", "TEXT"), ("status", "TEXT DEFAULT 'saved'"), ("campaign_id", "TEXT")):
        _ensure_column(conn, "topic_saves", name, declaration)

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS account_profile (
      singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
      handle TEXT, display_name TEXT, bio TEXT DEFAULT '', audience TEXT DEFAULT '',
      content_pillars_json TEXT DEFAULT '[]', preferences_json TEXT DEFAULT '{}',
      constraints_json TEXT DEFAULT '[]', timezone TEXT DEFAULT 'UTC', updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS profile_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL, handle TEXT,
      followers_count INTEGER, following_count INTEGER, posts_count INTEGER, profile_views INTEGER,
      source TEXT NOT NULL, metadata_json TEXT DEFAULT '{}', UNIQUE(handle,captured_at,source)
    );
    CREATE TABLE IF NOT EXISTS metric_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL, captured_at TEXT NOT NULL,
      followers_count INTEGER, metrics_json TEXT NOT NULL, source TEXT NOT NULL,
      metadata_json TEXT DEFAULT '{}', UNIQUE(post_id,captured_at,source)
    );
    CREATE TABLE IF NOT EXISTS growth_goals (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, objective TEXT NOT NULL, baseline REAL NOT NULL,
      target REAL NOT NULL, horizon_at TEXT NOT NULL, weekly_cadence_json TEXT DEFAULT '{}',
      guardrails_json TEXT DEFAULT '[]', status TEXT DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS campaigns (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, goal_id TEXT, narrative TEXT DEFAULT '',
      content_pillars_json TEXT DEFAULT '[]', status TEXT DEFAULT 'active', memory_json TEXT DEFAULT '{}',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(goal_id) REFERENCES growth_goals(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS experiments (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, campaign_id TEXT, hypothesis TEXT NOT NULL,
      variable TEXT NOT NULL, variants_json TEXT NOT NULL, primary_metric TEXT NOT NULL,
      min_sample_size INTEGER NOT NULL, status TEXT DEFAULT 'draft', result_json TEXT DEFAULT '{}',
      started_at TEXT, ended_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS knowledge_sources (
      id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, publisher TEXT,
      published_at TEXT, claim TEXT NOT NULL, evidence_grade TEXT DEFAULT 'unrated',
      policy_compatible INTEGER DEFAULT 1, tags_json TEXT DEFAULT '[]', notes TEXT DEFAULT '',
      verified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS generations (
      id TEXT PRIMARY KEY, action TEXT NOT NULL, context_id TEXT, context_json TEXT NOT NULL,
      request_json TEXT NOT NULL, strategy_json TEXT DEFAULT '{}', selected_candidate_id TEXT,
      lifecycle_status TEXT DEFAULT 'generated', inserted_at TEXT, posted_at TEXT, published_post_id TEXT,
      final_text TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS generation_candidates (
      candidate_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, rank_index INTEGER NOT NULL,
      text TEXT NOT NULL, candidate_json TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(generation_id) REFERENCES generations(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS generation_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, generation_id TEXT NOT NULL, candidate_id TEXT,
      event TEXT NOT NULL, text TEXT, metadata_json TEXT DEFAULT '{}', created_at TEXT NOT NULL,
      FOREIGN KEY(generation_id) REFERENCES generations(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS bridge_contexts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
      captured_at TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bridge_sessions (
      session_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, app_version TEXT, platform TEXT,
      created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bridge_commands (
      id INTEGER PRIMARY KEY AUTOINCREMENT, command_type TEXT NOT NULL, payload_json TEXT NOT NULL,
      status TEXT DEFAULT 'pending', detail TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS strategy_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
      content TEXT NOT NULL, context_json TEXT DEFAULT '{}', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS safety_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, severity TEXT NOT NULL,
      blocked INTEGER NOT NULL, reasons_json TEXT NOT NULL, context_json TEXT DEFAULT '{}', created_at TEXT NOT NULL
    );
    """)


def _migration_3(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at);
    CREATE INDEX IF NOT EXISTS idx_metric_post_time ON metric_snapshots(post_id,captured_at);
    CREATE INDEX IF NOT EXISTS idx_profile_time ON profile_snapshots(captured_at);
    CREATE INDEX IF NOT EXISTS idx_generation_created ON generations(created_at);
    CREATE INDEX IF NOT EXISTS idx_generation_events ON generation_events(generation_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_bridge_context_kind ON bridge_contexts(kind,captured_at);
    CREATE INDEX IF NOT EXISTS idx_bridge_commands_id ON bridge_commands(id);
    CREATE INDEX IF NOT EXISTS idx_strategy_conversation ON strategy_messages(conversation_id,id);
    """)


def _migration_4(conn: sqlite3.Connection) -> None:
    for name, declaration in (
        ("retrieved_at", "TEXT"),
        ("review_after", "TEXT"),
        ("source_version", "TEXT"),
        ("applicability", "TEXT"),
    ):
        _ensure_column(conn, "knowledge_sources", name, declaration)
    for name, declaration in (
        ("display_name", "TEXT"),
        ("bio", "TEXT"),
        ("profile_url", "TEXT"),
        ("pinned_post_id", "TEXT"),
    ):
        _ensure_column(conn, "profile_snapshots", name, declaration)
    _ensure_column(conn, "reference_images", "local_path", "TEXT")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS strategic_state_versions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      state_hash TEXT NOT NULL UNIQUE,
      state_json TEXT NOT NULL,
      reason TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_strategic_state_created ON strategic_state_versions(created_at);
    """)


def _migration_5(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS preference_memories (
      id TEXT PRIMARY KEY,
      source_text TEXT NOT NULL,
      rule TEXT NOT NULL,
      scope TEXT NOT NULL,
      confidence REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'proposed',
      supersedes_id TEXT,
      created_at TEXT NOT NULL,
      confirmed_at TEXT,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS opportunity_assessments (
      id TEXT PRIMARY KEY,
      context_hash TEXT NOT NULL,
      action TEXT NOT NULL,
      score REAL NOT NULL,
      assessment_json TEXT NOT NULL,
      outcome_json TEXT DEFAULT '{}',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_preferences_status ON preference_memories(status,confirmed_at);
    CREATE INDEX IF NOT EXISTS idx_opportunities_context ON opportunity_assessments(context_hash,created_at);
    """)


def _migration_6(conn: sqlite3.Connection) -> None:
    # This is a private archive ledger. It keeps the user's supplied X export
    # intact as local records, while strategy retrieval deliberately reads only
    # the relevant writing and account signals rather than sensitive device data.
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS archive_records (
      source_file TEXT NOT NULL,
      record_key TEXT NOT NULL,
      record_type TEXT NOT NULL,
      source_created_at TEXT,
      text_excerpt TEXT,
      payload_json TEXT NOT NULL,
      imported_at TEXT NOT NULL,
      PRIMARY KEY(source_file, record_key)
    );
    CREATE INDEX IF NOT EXISTS idx_archive_records_type ON archive_records(record_type,source_created_at);
    """)


def _migration_7(conn: sqlite3.Connection) -> None:
    # Model-intelligence storage: persisted embeddings, curated training data,
    # scorer calibration, bandit arms, live trend + research caches. All local.
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS embedding_cache (
      text_hash TEXT NOT NULL,
      model TEXT NOT NULL,
      vector BLOB NOT NULL,
      dim INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY(text_hash, model)
    );
    CREATE TABLE IF NOT EXISTS curated_examples (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      text TEXT NOT NULL,
      format TEXT,
      angle_id TEXT,
      label_json TEXT NOT NULL DEFAULT '{}',
      weight REAL NOT NULL DEFAULT 1.0,
      source_ref TEXT,
      dataset_version TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_curated_examples_kind ON curated_examples(kind, format);
    CREATE TABLE IF NOT EXISTS preference_pairs (
      id TEXT PRIMARY KEY,
      generation_id TEXT,
      context_json TEXT NOT NULL DEFAULT '{}',
      rejected_text TEXT NOT NULL,
      chosen_text TEXT NOT NULL,
      edit_kind TEXT NOT NULL,
      edit_distance REAL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS scorer_calibration (
      candidate_id TEXT PRIMARY KEY,
      generation_id TEXT NOT NULL,
      score REAL NOT NULL,
      terms_json TEXT NOT NULL DEFAULT '{}',
      objective TEXT NOT NULL,
      posted INTEGER NOT NULL DEFAULT 0,
      outcome_pct REAL,
      outcome_at TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_scorer_calibration_posted ON scorer_calibration(posted, outcome_at);
    CREATE TABLE IF NOT EXISTS bandit_arms (
      arm_id TEXT PRIMARY KEY,
      format TEXT NOT NULL,
      angle_id TEXT NOT NULL,
      -- Still '*' for every row, and that is a DECISION, not an oversight (cut 2026-08-21).
      -- Writing the lane here was scoped and cut on ONE ground that holds: `generations` has
      -- 2,249 rows of which FOUR are posted, so there is no reward signal to attribute and a
      -- bandit arm keyed by lane would be a promise rather than a feature. That ground alone
      -- is enough and the deferral stands on it.
      --
      -- CORRECTION 2026-08-23: this comment also claimed "the lane is recorded on
      -- `generations` (`topic_lane`, migration 14) where it costs nothing and accumulates".
      -- THAT WAS FALSE WHEN WRITTEN. Migration 14 created the column and nothing wrote it —
      -- zero non-null in 2,249 rows — so nothing was accumulating and
      -- `idx_generations_topic_lane` indexed an empty column. It was false in the direction
      -- that made deferring look free, which is the worst direction for a justification.
      -- `create_generation` now writes it; whether it FILLS depends on the caller supplying
      -- a lane, which is tracked separately.
      topic_cluster TEXT NOT NULL DEFAULT '*',
      n REAL NOT NULL DEFAULT 0,
      reward_sum REAL NOT NULL DEFAULT 0,
      reward_sq REAL NOT NULL DEFAULT 0,
      last_observation_at TEXT,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bandit_observations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      arm_id TEXT NOT NULL,
      post_id TEXT,
      generation_id TEXT,
      reward REAL NOT NULL,
      reward_parts_json TEXT NOT NULL DEFAULT '{}',
      observed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_bandit_observations_arm ON bandit_observations(arm_id, observed_at);
    CREATE TABLE IF NOT EXISTS trend_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      captured_at TEXT NOT NULL,
      source_page TEXT NOT NULL,
      name TEXT NOT NULL,
      category TEXT,
      volume_text TEXT,
      rank INTEGER,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_trend_snapshots_time ON trend_snapshots(captured_at);
    CREATE TABLE IF NOT EXISTS research_cache (
      id TEXT PRIMARY KEY,
      query_hash TEXT NOT NULL,
      query TEXT NOT NULL,
      answer TEXT NOT NULL,
      sources_json TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_research_cache_query ON research_cache(query_hash, expires_at);
    CREATE TABLE IF NOT EXISTS job_state (
      key TEXT PRIMARY KEY,
      value_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_sources (
      id TEXT PRIMARY KEY,
      url TEXT,
      title TEXT,
      text TEXT NOT NULL,
      summary TEXT,
      created_at TEXT NOT NULL
    );
    """)


def _migration_8(conn: sqlite3.Connection) -> None:
    """Foundation for the intelligence spec: relationships, live memory, profile.

    Additive only — new nullable columns and new tables. An older build opens a
    migrated database unharmed because nothing it reads changed shape, and a
    populated archive is never rewritten to add a column.
    """
    # Live Account Memory (§3.2): a post Starling did not generate is still
    # the user's writing. `origin` distinguishes imported archive rows from
    # posts observed live and from drafts this app produced; `first_observed_at`
    # records when we saw it, which is NOT the same as when it was posted.
    _ensure_column(conn, "posts", "origin", "TEXT")
    _ensure_column(conn, "posts", "first_observed_at", "TEXT")
    _ensure_column(conn, "posts", "author_handle", "TEXT")
    conn.executescript("""
    -- §2.6 / §4.2: an author relationship is not a follower count. One row per
    -- account the user meaningfully interacts with, rebuilt from the archive
    -- and kept current by live observation.
    CREATE TABLE IF NOT EXISTS author_relationships (
      handle TEXT PRIMARY KEY,
      display_name TEXT,
      interactions INTEGER NOT NULL DEFAULT 0,
      replies INTEGER NOT NULL DEFAULT 0,
      quotes INTEGER NOT NULL DEFAULT 0,
      topic_overlap REAL,
      outcome_percentile REAL,
      distinctness REAL,
      last_interaction_at TEXT,
      first_interaction_at TEXT,
      safety_fit REAL,
      sample_size INTEGER NOT NULL DEFAULT 0,
      confidence TEXT NOT NULL DEFAULT 'unknown',
      topics_json TEXT NOT NULL DEFAULT '[]',
      notes TEXT,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_author_rel_recent
      ON author_relationships(last_interaction_at DESC);

    -- §2.7: the Account Intelligence Profile is versioned and timestamped so a
    -- downstream recommendation can say it is running on a preliminary or
    -- stale understanding instead of pretending the account is fully known.
    CREATE TABLE IF NOT EXISTS account_profiles (
      version INTEGER PRIMARY KEY AUTOINCREMENT,
      built_at TEXT NOT NULL,
      evidence_through TEXT,
      preliminary INTEGER NOT NULL DEFAULT 1,
      profile_json TEXT NOT NULL,
      gaps_json TEXT NOT NULL DEFAULT '[]',
      corpus_version INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_account_profiles_built ON account_profiles(built_at DESC);
    """)


def _migration_9(conn: sqlite3.Connection) -> None:
    """The RESOLVED reply target, kept separate from `parent_author`.

    `parent_author` keeps meaning "what the archive actually said" — a plain X
    export leaves it empty on every reply, because it is only populated from the
    optional reply-context.csv. Without a resolved target the repeated-target
    and conversation-uniqueness penalties (§4.1c, §6.3) can never fire on real
    data. `reply_target_basis` records how we know: recorded | mention.

    Separate from migration 8 on purpose: 8 had already been applied to live
    databases, and an applied migration must never be edited — the runner would
    skip it and the columns would silently never appear.
    """
    _ensure_column(conn, "posts", "reply_target_handle", "TEXT")
    _ensure_column(conn, "posts", "reply_target_basis", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_reply_target "
                 "ON posts(reply_target_handle, created_at DESC)")


def _migration_10(conn: sqlite3.Connection) -> None:
    """Scheduled drafts — HOLD AND REMIND, never auto-post.

    A finished draft the user chose to hold for a better moment. Starling has
    no capability to post to X; `target_at` is when we surface the draft back to
    the user as "ready to post now" — the human always publishes. `status`
    moves scheduled → posted (user confirmed they posted it) or dismissed.
    """
    conn.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_drafts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT NOT NULL,
      target_at TEXT NOT NULL,
      target_label TEXT,
      action TEXT NOT NULL DEFAULT 'post',
      text TEXT NOT NULL,
      topic TEXT,
      freshness TEXT,
      reason TEXT,
      image_json TEXT,
      status TEXT NOT NULL DEFAULT 'scheduled',
      posted_at TEXT,
      meta_json TEXT NOT NULL DEFAULT '{}'
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_drafts_due "
                 "ON scheduled_drafts(status, target_at)")


def _migration_11(conn: sqlite3.Connection) -> None:
    """Local, auditable memory for explicit topic choices and verified posts.

    This is not a generic chat transcript index.  One table holds the current
    topic focuses; the event ledger explains exactly why each focus gained
    weight and makes repeat browser observations idempotent.
    """
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS topic_memories (
      topic_key TEXT PRIMARY KEY,
      topic TEXT NOT NULL,
      explicit_signals INTEGER NOT NULL DEFAULT 0,
      verified_posts INTEGER NOT NULL DEFAULT 0,
      active INTEGER NOT NULL DEFAULT 1,
      last_explicit_at TEXT,
      last_verified_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS topic_memory_events (
      id TEXT PRIMARY KEY,
      topic_key TEXT NOT NULL,
      event_type TEXT NOT NULL,
      source_ref TEXT NOT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,
      UNIQUE(topic_key, event_type, source_ref),
      FOREIGN KEY(topic_key) REFERENCES topic_memories(topic_key) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_topic_memories_active
      ON topic_memories(active, last_explicit_at DESC, last_verified_at DESC);
    CREATE INDEX IF NOT EXISTS idx_topic_memory_events_topic
      ON topic_memory_events(topic_key, created_at DESC);
    """)


def _migration_12(conn: sqlite3.Connection) -> None:
    """Explicit image choices are preference signals, not hidden inference."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS image_feedback (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      generation_id TEXT,
      candidate_id TEXT,
      choice TEXT NOT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_image_feedback_created
      ON image_feedback(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_image_feedback_generation
      ON image_feedback(generation_id, created_at DESC);
    """)


def _migration_13(conn: sqlite3.Connection) -> None:
    """A post he deleted must stop being described as his current one.

    He asked what his most recent post was, was told one he had already
    deleted, and said so. The archive import already knows about deletions
    (`deleted_tweet` records), but a deletion made in X's own UI never reaches
    this database at all until a re-sync — so there has to be a way to record
    one from the conversation, which is where he actually told us.
    """
    _ensure_column(conn, "posts", "deleted_at", "TEXT")


def _migration_14(conn: sqlite3.Connection) -> None:
    """What lane and what register a draft belonged to - recorded at the time or never.

    ONE MIGRATION FOR TWO SUBSYSTEMS, ON PURPOSE (R-MIG-1). Two lanes each appending their
    own `_migration_14` to `MIGRATIONS` is a collision that only shows up on someone else's
    machine, so both columns land here together. `_ensure_column` is idempotent, so whichever
    subsystem's code ships first the other is simply a no-op.

    `post_register`, NOT `register`: Pydantic shadows `ABCMeta.register` on a model field, and
    the column is named to match the field rather than to be renamed later under load.

    NO BACKFILL. `context_json` holds the parent post for replies, not the topic, so inventing
    lanes for 2,242 historical rows would poison the only clean signal this column will ever
    have. It starts empty and fills from today - which is also why it is worth landing now
    rather than at the end: the column's value is entirely in how long it has been collecting.
    """
    _ensure_column(conn, "generations", "topic_lane", "TEXT")
    _ensure_column(conn, "generations", "topic_lane_score", "REAL")
    _ensure_column(conn, "generations", "post_register", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_generations_topic_lane "
                 "ON generations(topic_lane, created_at DESC)")


MIGRATIONS = (_migration_1, _migration_2, _migration_3, _migration_4, _migration_5,
              _migration_6, _migration_7, _migration_8, _migration_9, _migration_10,
              _migration_11, _migration_12, _migration_13, _migration_14)


def _seed_curated_knowledge(conn: sqlite3.Connection) -> None:
    retrieved_at = "2026-07-14T00:00:00+00:00"
    now = _utc()
    for source in CURATED_KNOWLEDGE_SOURCES:
        conn.execute("""
        INSERT INTO knowledge_sources(
          id,url,title,publisher,published_at,claim,evidence_grade,policy_compatible,tags_json,notes,
          verified_at,created_at,updated_at,retrieved_at,review_after,source_version,applicability
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(url) DO NOTHING
        """, (
            source["id"], source["url"], source["title"], source["publisher"], source["published_at"],
            source["claim"], source["evidence_grade"], 1, _json(source["tags"]),
            "Bundled, versioned primary-source seed. Review on or before review_after.", retrieved_at, now, now,
            retrieved_at, source["review_after"], source["source_version"], source["applicability"],
        ))


def initialize() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, migration in enumerate(MIGRATIONS, start=1):
            if version in applied:
                continue
            migration(conn)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, _utc()))
        _seed_curated_knowledge(conn)
    # File permissions come from the process: a hosted engine runs as its own OS
    # user inside a 0700 data folder, so no per-file hardening is needed here.


def checkpoint() -> None:
    """Fold the write-ahead log back into the database (graceful shutdown)."""
    try:
        with connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:                                        # noqa: BLE001
        pass


def upsert_post(record: dict) -> None:
    fields = {
        "post_id": str(record["post_id"]), "post_url": record.get("post_url"), "created_at": _iso(record.get("created_at")),
        "post_type": record.get("post_type", "post"), "text": record.get("text", ""),
        "parent_post_id": record.get("parent_post_id"), "parent_text": record.get("parent_text"),
        "parent_author": record.get("parent_author"), "parent_post_url": record.get("parent_post_url"),
        "quoted_post_id": record.get("quoted_post_id"), "quote_text": record.get("quote_text"),
        "thread_json": _json(record.get("thread", [])), "media_json": _json(record.get("media", [])),
        "media_count": int(record.get("media_count", 0)), "has_image": int(record.get("has_image", 0)),
        "language": record.get("language"), "visibility": record.get("visibility", "public"),
        "features_json": _json(record.get("features", {})), "metrics_json": _json(record.get("metrics", {})),
        "metrics_updated_at": _iso(record.get("metrics_updated_at")), "source_file": record.get("source_file"),
    }
    with connect() as conn:
        conn.execute("""
        INSERT INTO posts(post_id,post_url,created_at,post_type,text,parent_post_id,parent_text,parent_author,parent_post_url,
          quoted_post_id,quote_text,thread_json,media_json,media_count,has_image,language,visibility,features_json,
          metrics_json,metrics_updated_at,source_file)
        VALUES(:post_id,:post_url,:created_at,:post_type,:text,:parent_post_id,:parent_text,:parent_author,:parent_post_url,
          :quoted_post_id,:quote_text,:thread_json,:media_json,:media_count,:has_image,:language,:visibility,:features_json,
          :metrics_json,:metrics_updated_at,:source_file)
        ON CONFLICT(post_id) DO UPDATE SET
          post_url=COALESCE(excluded.post_url, posts.post_url),
          created_at=COALESCE(excluded.created_at, posts.created_at),
          post_type=CASE
            WHEN excluded.post_type IN ('post', 'original') AND posts.post_type IN ('reply', 'quote', 'thread', 'poll')
              THEN posts.post_type
            ELSE excluded.post_type
          END,
          text=CASE WHEN excluded.text='' THEN posts.text ELSE excluded.text END,
          -- Profile captures often identify our reply but cannot expose its
          -- parent permalink. Never let that lower-detail sighting erase an
          -- exact parent relation captured on the source post detail page.
          parent_post_id=COALESCE(excluded.parent_post_id, posts.parent_post_id),
          parent_text=COALESCE(excluded.parent_text, posts.parent_text),
          parent_author=COALESCE(excluded.parent_author, posts.parent_author),
          parent_post_url=COALESCE(excluded.parent_post_url, posts.parent_post_url),
          quoted_post_id=COALESCE(excluded.quoted_post_id, posts.quoted_post_id),
          quote_text=COALESCE(excluded.quote_text, posts.quote_text),
          thread_json=excluded.thread_json, media_json=excluded.media_json, media_count=excluded.media_count,
          has_image=excluded.has_image, language=excluded.language, visibility=excluded.visibility,
          -- A passive re-sighting sends metrics:{} (main sends identity, not
          -- numbers). Blanking the stored metrics here silently un-earned every
          -- learning-loop reward that reads the raw column via get_post().
          features_json=excluded.features_json,
          metrics_json=CASE WHEN excluded.metrics_json IN ('','{}') THEN posts.metrics_json ELSE excluded.metrics_json END,
          metrics_updated_at=COALESCE(excluded.metrics_updated_at,posts.metrics_updated_at), source_file=excluded.source_file
        """, fields)
        if record.get("metrics"):
            captured = fields["metrics_updated_at"] or _utc()
            conn.execute("""INSERT OR IGNORE INTO metric_snapshots(post_id,captured_at,followers_count,metrics_json,source,metadata_json)
              VALUES(?,?,?,?,?,?)""", (fields["post_id"], captured, record.get("followers_count"), fields["metrics_json"],
              record.get("metrics_source", "analytics"), _json({"source_file": fields["source_file"]})))
            conn.execute("UPDATE posts SET metrics_updated_at=COALESCE(metrics_updated_at,?) WHERE post_id=?", (captured, fields["post_id"]))
    mark_corpus_dirty()


def upsert_archive_records(source_file: str, items: list[dict], record_type: str) -> int:
    """Store every parsed archive row locally, deduplicated by content hash."""
    now, count = _utc(), 0
    with connect() as conn:
        for index, item in enumerate(items):
            payload = _json(item)
            key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            raw = item if isinstance(item, dict) else {"value": item}
            created = raw.get("created_at") or raw.get("createdAt") or raw.get("createdAtDate")
            text = raw.get("full_text") or raw.get("text") or raw.get("message") or raw.get("title") or ""
            conn.execute("""INSERT INTO archive_records(source_file,record_key,record_type,source_created_at,text_excerpt,payload_json,imported_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_file,record_key) DO UPDATE SET
                record_type=excluded.record_type,source_created_at=excluded.source_created_at,text_excerpt=excluded.text_excerpt,
                payload_json=excluded.payload_json,imported_at=excluded.imported_at""",
                (source_file, key, record_type, _iso(created), str(text)[:4000], payload, now))
            count += 1
    mark_corpus_dirty()
    return count


def remove_posts_from_archive_sources(source_tokens: Iterable[str]) -> int:
    tokens = [token.lower() for token in source_tokens if token]
    if not tokens:
        return 0
    clauses = " OR ".join("LOWER(source_file) LIKE ?" for _ in tokens)
    with connect() as conn:
        cursor = conn.execute(f"DELETE FROM posts WHERE {clauses}", [f"%{token}%" for token in tokens])
    removed = int(cursor.rowcount)
    # This deletes from `posts` and every other writer to that table bumps the
    # corpus version — this one did not. Caches keyed on `corpus_version()`
    # alone (`intelligence.py` voice examples, `profile.py` snapshots) therefore
    # kept serving rows that no longer exist after an archive source was
    # removed. Callers that key on `(corpus_version(), len(rows))` happened to
    # notice via the length, which is what hid it.
    if removed:
        mark_corpus_dirty()
    return removed


def archive_texts(record_types: Iterable[str], limit: int = 500) -> list[str]:
    wanted = tuple(record_types)
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT text_excerpt FROM archive_records WHERE record_type IN ({placeholders}) AND text_excerpt IS NOT NULL ORDER BY imported_at DESC LIMIT ?",
            (*wanted, limit),
        ).fetchall()
    return [str(row[0]) for row in rows if str(row[0]).strip()]


def _decode_post(row: sqlite3.Row | dict) -> dict:
    value = dict(row)
    value["metrics"] = _from_json(value.pop("metrics_json", None), {})
    value["thread"] = _from_json(value.pop("thread_json", None), [])
    value["media"] = _from_json(value.pop("media_json", None), [])
    value["features"] = _from_json(value.pop("features_json", None), {})
    return value


def records() -> list[dict]:
    with connect() as conn:
        return [_decode_post(row) for row in conn.execute("SELECT * FROM posts ORDER BY created_at")]


def get_post(post_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM posts WHERE post_id=?", (post_id,)).fetchone()
        return _decode_post(row) if row else None


def save_feedback(candidate_id: str, action: str, final_text: str | None, context_id: str | None,
                  generation_id: str | None = None, metadata: dict | None = None) -> None:
    with connect() as conn:
        if not generation_id:
            row = conn.execute("SELECT generation_id FROM generation_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            generation_id = str(row[0]) if row else None
        conn.execute("INSERT INTO feedback(candidate_id,action,final_text,context_id,generation_id,metadata_json) VALUES(?,?,?,?,?,?)",
                     (candidate_id, action, final_text, context_id, generation_id, _json(metadata)))
        if generation_id:
            conn.execute("INSERT INTO generation_events(generation_id,candidate_id,event,text,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
                         (generation_id, candidate_id, action, final_text, _json(metadata), _utc()))
            status = {"accepted": "selected", "selected": "selected", "edited": "edited", "rejected": "rejected",
                      "regenerated": "regenerated", "copied": "copied", "inserted": "inserted",
                      "abandoned": "abandoned", "posted": "posted"}.get(action, action)
            conn.execute("UPDATE generations SET selected_candidate_id=COALESCE(?,selected_candidate_id), lifecycle_status=?, final_text=COALESCE(?,final_text), updated_at=? WHERE id=?",
                         (candidate_id if action in ("selected", "accepted", "edited", "copied", "inserted", "posted") else None, status, final_text, _utc(), generation_id))
            if action == "inserted":
                conn.execute("UPDATE generations SET inserted_at=COALESCE(inserted_at,?) WHERE id=?", (_utc(), generation_id))
            elif action == "posted":
                conn.execute("UPDATE generations SET posted_at=COALESCE(posted_at,?) WHERE id=?", (_utc(), generation_id))
    mark_corpus_dirty()


def save_image_feedback(data: dict) -> dict:
    """Store a direct keep/skip/replace choice for future visual ranking.

    This is intentionally separate from draft acceptance. A user may like the
    words but decline the image, and the system must preserve that distinction.
    """
    choice = str(data.get("choice") or "").lower()
    if choice not in {"used", "skipped", "replaced"}:
        raise ValueError("image feedback choice must be used, skipped, or replaced")
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO image_feedback(generation_id,candidate_id,choice,metadata_json,created_at)
               VALUES(?,?,?,?,?)""",
            (data.get("generation_id"), data.get("candidate_id"), choice,
             _json(data.get("metadata") or {}), _utc()),
        )
    return {"id": int(cursor.lastrowid), "choice": choice}


def image_feedback_summary(limit: int = 80) -> dict:
    """Return only explicit image choices, with a visible sample size."""
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT choice FROM image_feedback ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))]
    counts = {choice: sum(row["choice"] == choice for row in rows)
              for choice in ("used", "skipped", "replaced")}
    decided = counts["used"] + counts["skipped"] + counts["replaced"]
    positive = counts["used"]
    preference = positive / decided if decided else None
    return {
        "sample": decided, "used": counts["used"], "skipped": counts["skipped"],
        "replaced": counts["replaced"], "keep_rate": round(preference, 3) if preference is not None else None,
    }


def learning_examples(limit: int = 200, remote_safe: bool = False) -> list[str]:
    with connect() as conn:
        # WHAT HE REWROTE IS THE BEST VOICE DATA THERE IS, and it was being
        # thrown away. Measured on this machine: 190 generations, 0 marked
        # 'posted', 0 rows in `feedback` — so both branches below returned
        # nothing and the voice corpus was the frozen archive forever. Meanwhile
        # 5 generations sat at 'edited' holding his own final wording.
        #
        # 'inserted' is deliberately NOT included. Those rows are byte-identical
        # to the draft — it is Starling's text sitting in the composer, and
        # feeding it back as "his voice" is exactly how a model ends up
        # imitating itself. Only text he actually changed counts.
        rows = conn.execute("""
          SELECT final_text,context_json FROM generations
          WHERE lifecycle_status IN ('posted','edited') AND final_text IS NOT NULL
          UNION ALL
          SELECT feedback.final_text,generations.context_json FROM feedback
          LEFT JOIN generations ON generations.id=feedback.generation_id
          WHERE feedback.action IN ('accepted','edited') AND feedback.final_text IS NOT NULL
          LIMIT ?
        """, (limit,)).fetchall()
    values = []
    for text, context_json in rows:
        context = _from_json(context_json, {})
        if remote_safe and context.get("visibility") == "protected":
            continue
        if text:
            values.append(str(text))
    return values


def previously_drafted_texts(limit: int = 400) -> list[str]:
    """Every draft Starling has already written for this account.

    Not the same thing as learning_examples: those are drafts he ACCEPTED.
    These are all of them, accepted or not. The repeat that reached him
    ("Why is Anthropic so quiet about Opus 5...") had been drafted before and
    never posted, so it appeared in no archive and no example list, and the
    only repeat-detector on the account read `recent_angles` — a list written
    solely when a generation is marked posted against a campaign. There are
    zero campaigns on this machine, so that list has always been empty and the
    fatigue score has always been 0.0. A draft has to be checked against what
    the machine actually produced, which is this.
    """
    with connect() as conn:
        rows = conn.execute("""
          SELECT text FROM generation_candidates WHERE text IS NOT NULL
          UNION
          SELECT final_text FROM generations WHERE final_text IS NOT NULL
          ORDER BY 1 LIMIT ?
        """, (limit,)).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def previously_drafted_replies(limit: int = 120) -> list[str]:
    """Just the REPLIES, newest first — what the reply repeat-check compares to.

    `previously_drafted_texts` is the whole pile, alphabetically, capped at 400.
    Neither property survives contact with a reply. Replies are short, so a
    reply-vs-post comparison is noise, and the alphabetical cap means the most
    recent drafts — the only ones that can plausibly be repeated — are the
    least likely to be in the window at all.

    The repeat he would actually see is the one he was shown ten minutes ago:
    "So Opus 5's half price? What's the catch?" was drafted four separate times
    on 2026-07-26 and nothing objected.

    PROVENANCE, checked 2026-08-09: all 52 drafts of that shape carry FIXTURE
    ids. The gate's calibration is sound and its motivating example is arena
    output — a docstring's motivating example is also a count over fixtures,
    and it is more dangerous than a stray number because it reads as the reason
    the code exists.
    """
    with connect() as conn:
        rows = conn.execute("""
          SELECT c.text FROM generation_candidates c
          JOIN generations g ON g.id = c.generation_id
          WHERE g.action = 'reply' AND c.text IS NOT NULL
          ORDER BY c.created_at DESC LIMIT ?
        """, (limit,)).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def save_topic(context: dict, title: str | None = None, campaign_id: str | None = None) -> int:
    with connect() as conn:
        cursor = conn.execute("INSERT INTO topic_saves(context_json,title,campaign_id) VALUES(?,?,?)", (_json(context), title, campaign_id))
        return int(cursor.lastrowid)


def list_topics(status: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM topic_saves", []
    if status:
        sql, args = f"{sql} WHERE status=?", [status]
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(f"{sql} ORDER BY id DESC", args)]
    for row in rows:
        row["context"] = _from_json(row.pop("context_json"), {})
    return rows


def _scheduled_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["image"] = _from_json(value.pop("image_json", None), None)
    value["meta"] = _from_json(value.pop("meta_json", None), {})
    return value


def save_scheduled_draft(draft: dict) -> int:
    """Persist a held draft. HOLD AND REMIND only — never posts to X."""
    now = _utc()
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO scheduled_drafts(created_at,target_at,target_label,action,text,topic,
               freshness,reason,image_json,status,meta_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (now, _iso(draft.get("target_at")), draft.get("target_label"), draft.get("action", "post"),
             draft.get("text", ""), draft.get("topic"), draft.get("freshness"), draft.get("reason"),
             _json(draft.get("image")) if draft.get("image") is not None else None,
             draft.get("status", "scheduled"), _json(draft.get("meta", {}))))
        return int(cursor.lastrowid)


def reschedule_scheduled_draft(draft_id: int, target_at: str,
                               target_label: str | None = None) -> bool:
    """Move an existing hold to a new time. Still never posts anything.

    Without this, "actually, make it tomorrow" saved a SECOND draft and the
    original fired anyway — two reminders for one post.
    """
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE scheduled_drafts SET target_at=?, target_label=?
               WHERE id=? AND status='scheduled'""",
            (_iso(target_at), target_label, int(draft_id)))
        return cursor.rowcount > 0


def find_scheduled_duplicate(text: str) -> dict | None:
    """An already-held draft with the same text, if any.

    Pressing Schedule twice (or scheduling a draft that is already held) used
    to create a second identical reminder, which then fired twice.
    """
    needle = " ".join(str(text or "").split()).lower()
    if not needle:
        return None
    for row in list_scheduled_drafts("scheduled"):
        if " ".join(str(row.get("text") or "").split()).lower() == needle:
            return row
    return None


def list_scheduled_drafts(status: str | None = "scheduled") -> list[dict]:
    """Held drafts, soonest target first."""
    sql, args = "SELECT * FROM scheduled_drafts", []
    if status:
        sql, args = f"{sql} WHERE status=?", [status]
    with connect() as conn:
        rows = [_scheduled_row(row) for row in conn.execute(f"{sql} ORDER BY target_at ASC", args)]
    return rows


def due_scheduled_drafts(now: Any = None) -> list[dict]:
    """Scheduled drafts whose moment has arrived — surface as ready to post."""
    cutoff = _iso(now) or _utc()
    with connect() as conn:
        rows = [_scheduled_row(row) for row in conn.execute(
            "SELECT * FROM scheduled_drafts WHERE status='scheduled' AND target_at<=? ORDER BY target_at ASC",
            (cutoff,))]
    return rows


def mark_scheduled_draft(draft_id: int, status: str) -> bool:
    """scheduled → posted (user confirmed) or dismissed. Never posts anything."""
    if status not in {"posted", "dismissed"}:
        raise ValueError(f"Unsupported scheduled-draft status: {status}")
    posted_at = _utc() if status == "posted" else None
    with connect() as conn:
        # A stale click from another tab cannot overwrite a terminal outcome or
        # report success for a draft that no longer exists.
        cursor = conn.execute(
            "UPDATE scheduled_drafts SET status=?, posted_at=? WHERE id=? AND status='scheduled'",
            (status, posted_at, int(draft_id)),
        )
        return cursor.rowcount == 1


def delete_scheduled_draft(draft_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM scheduled_drafts WHERE id=?", (int(draft_id),))


def profile_counts() -> tuple[int, int]:
    with connect() as conn:
        posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        analytics = conn.execute("SELECT COUNT(*) FROM posts WHERE metrics_json != '{}' ").fetchone()[0]
    return int(posts), int(analytics)


def replace_reference_images(rows: list[dict]) -> None:
    with connect() as conn:
        for row in rows:
            if not row.get("filename"):
                continue
            conn.execute("INSERT OR REPLACE INTO reference_images(filename,source_url,rights_status,notes,local_path) VALUES(?,?,?,?,?)",
                         (row.get("filename"), row.get("source_url"), row.get("rights_status"), row.get("why_i_like_it") or row.get("notes"), row.get("local_path")))


def set_account_profile(profile: dict) -> dict:
    now = _utc()
    with connect() as conn:
        conn.execute("""INSERT INTO account_profile(singleton_id,handle,display_name,bio,audience,content_pillars_json,preferences_json,constraints_json,timezone,updated_at)
          VALUES(1,?,?,?,?,?,?,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET handle=excluded.handle,display_name=excluded.display_name,
          bio=excluded.bio,audience=excluded.audience,content_pillars_json=excluded.content_pillars_json,
          preferences_json=excluded.preferences_json,constraints_json=excluded.constraints_json,timezone=excluded.timezone,updated_at=excluded.updated_at""",
          (profile.get("handle"), profile.get("display_name"), profile.get("bio", ""), profile.get("audience", ""),
           _json(profile.get("content_pillars", [])), _json(profile.get("preferences", {})), _json(profile.get("constraints", [])),
           profile.get("timezone", "UTC"), now))
    return get_account_profile()


def get_account_profile() -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM account_profile WHERE singleton_id=1").fetchone()
    if not row:
        return {"handle": None, "display_name": None, "bio": "", "audience": "", "content_pillars": [], "preferences": {}, "constraints": [], "timezone": "UTC"}
    value = dict(row)
    value.pop("singleton_id", None)
    value["content_pillars"] = _from_json(value.pop("content_pillars_json"), [])
    value["preferences"] = _from_json(value.pop("preferences_json"), {})
    value["constraints"] = _from_json(value.pop("constraints_json"), [])
    return value


def save_profile_snapshot(snapshot: dict) -> dict:
    captured = _iso(snapshot.get("captured_at")) or _utc()
    with connect() as conn:
        conn.execute("""INSERT OR REPLACE INTO profile_snapshots(captured_at,handle,followers_count,following_count,posts_count,profile_views,source,metadata_json,
          display_name,bio,profile_url,pinned_post_id)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (captured, snapshot.get("handle"), snapshot.get("followers_count"), snapshot.get("following_count"),
          snapshot.get("posts_count"), snapshot.get("profile_views"), snapshot.get("source", "manual"), _json(snapshot.get("metadata", {})),
          snapshot.get("display_name"), snapshot.get("bio"), snapshot.get("profile_url"), snapshot.get("pinned_post_id")))
        row = conn.execute("SELECT * FROM profile_snapshots WHERE handle IS ? AND captured_at=? AND source=?", (snapshot.get("handle"), captured, snapshot.get("source", "manual"))).fetchone()
    mark_corpus_dirty()
    return _decode_json_columns(dict(row), ("metadata_json",))


def list_profile_snapshots(limit: int = 500) -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM profile_snapshots ORDER BY captured_at DESC LIMIT ?", (limit,))]
    return [_decode_json_columns(row, ("metadata_json",)) for row in rows]


def save_metric_snapshot(snapshot: dict) -> dict:
    captured = _iso(snapshot.get("captured_at")) or _utc()
    metrics = snapshot.get("metrics", {})
    with connect() as conn:
        conn.execute("""INSERT OR REPLACE INTO metric_snapshots(post_id,captured_at,followers_count,metrics_json,source,metadata_json)
          VALUES(?,?,?,?,?,?)""", (str(snapshot["post_id"]), captured, snapshot.get("followers_count"), _json(metrics), snapshot.get("source", "manual"), _json(snapshot.get("metadata", {}))))
        row = conn.execute("SELECT * FROM metric_snapshots WHERE post_id=? AND captured_at=? AND source=?", (str(snapshot["post_id"]), captured, snapshot.get("source", "manual"))).fetchone()
        latest = conn.execute("SELECT id FROM metric_snapshots WHERE post_id=? ORDER BY captured_at DESC,id DESC LIMIT 1", (str(snapshot["post_id"]),)).fetchone()
        if latest and int(latest[0]) == int(row["id"]):
            conn.execute("UPDATE posts SET metrics_json=?,metrics_updated_at=? WHERE post_id=?", (_json(metrics), captured, str(snapshot["post_id"])))
    value = dict(row)
    value["metrics"] = _from_json(value.pop("metrics_json"), {})
    value["metadata"] = _from_json(value.pop("metadata_json"), {})
    mark_corpus_dirty()
    return value


def list_metric_snapshots(post_id: str | None = None, limit: int = 2000) -> list[dict]:
    sql, args = "SELECT * FROM metric_snapshots", []
    if post_id:
        sql, args = f"{sql} WHERE post_id=?", [post_id]
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(f"{sql} ORDER BY captured_at DESC,id DESC LIMIT ?", [*args, limit])]
    for row in rows:
        row["metrics"] = _from_json(row.pop("metrics_json"), {})
        row["metadata"] = _from_json(row.pop("metadata_json"), {})
    return rows


def load_cached_embeddings(hashes: list[str], model: str) -> dict[str, tuple[bytes, int]]:
    """Return {text_hash: (vector_blob, dim)} for cached rows of this model."""
    if not hashes:
        return {}
    result: dict[str, tuple[bytes, int]] = {}
    with connect() as conn:
        for start in range(0, len(hashes), 400):
            chunk = hashes[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT text_hash,vector,dim FROM embedding_cache WHERE model=? AND text_hash IN ({placeholders})",
                (model, *chunk)).fetchall()
            for text_hash, vector, dim in rows:
                result[str(text_hash)] = (bytes(vector), int(dim))
    return result


def store_cached_embeddings(rows: list[tuple[str, bytes, int]], model: str) -> None:
    """Persist (text_hash, vector_blob, dim) rows for this embedding model."""
    if not rows:
        return
    now = _utc()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO embedding_cache(text_hash,model,vector,dim,created_at) VALUES(?,?,?,?,?)",
            [(text_hash, model, vector, dim, now) for text_hash, vector, dim in rows])


def save_trend_snapshots(source_page: str, trends: list[dict], captured_at: Any = None) -> int:
    """Store visible X trends captured by the extension's quiet sync."""
    captured = _iso(captured_at) or _utc()
    count = 0
    with connect() as conn:
        for rank, trend in enumerate(trends[:30], start=1):
            name = str(trend.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT INTO trend_snapshots(captured_at,source_page,name,category,volume_text,rank,metadata_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (captured, source_page[:300], name[:200], trend.get("category"), trend.get("volume_text"),
                 trend.get("rank") or rank, _json({})))
            count += 1
        # Bounded history: trends are hot-cache data, not an archive.
        conn.execute("DELETE FROM trend_snapshots WHERE id NOT IN (SELECT id FROM trend_snapshots ORDER BY id DESC LIMIT 600)")
    return count


# ONE WINDOW, READ BY THE GATE AND BY EVERY CONSUMER.
#
# `_has_trends()` asked for 2 hours while all three consumers read 48 — one of
# them three lines below the gate in the same `if`. Measured over 385 trend
# captures spanning 17.5 days, sampled every 10 minutes: consumers had data
# 94.4% of the time, the gate said no for 88.5%, and they DISAGREED for 82.9%.
# Discarding usable data was the gate's normal state.
#
# A constant rather than two aligned literals: `main.py:1258` records this exact
# pair being found and half-fixed once already.
TREND_FRESH_HOURS = 48.0


def list_trend_snapshots(hours: float = TREND_FRESH_HOURS, limit: int = 8) -> list[dict]:
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600.0
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT captured_at,source_page,name,category,volume_text,rank FROM trend_snapshots ORDER BY id DESC LIMIT 120")]
    fresh: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        try:
            captured = datetime.fromisoformat(str(row["captured_at"]).replace("Z", "+00:00"))
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            if captured.timestamp() < cutoff:
                continue
        except ValueError:
            continue
        key = row["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        fresh.append(row)
        if len(fresh) >= limit:
            break
    return fresh


def trend_frequencies(hours: float = 8.0) -> list[dict]:
    """Per-trend appearance counts + recency in the window — the 'rising' signal.

    A trend seen in many recent snapshots is accelerating; a one-off is noise.
    Each trend also carries a lifecycle read: comparing the early half of the
    window against the late half separates a trend that is still climbing from
    one that already peaked — the radar must never recommend a moment that has
    passed as if it were arriving.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600.0
    now_ts = datetime.now(timezone.utc).timestamp()
    midpoint = cutoff + (now_ts - cutoff) / 2.0
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT captured_at,name,category,volume_text,rank FROM trend_snapshots ORDER BY id DESC LIMIT 800")]
    agg: dict[str, dict] = {}
    for row in rows:
        try:
            captured = datetime.fromisoformat(str(row["captured_at"]).replace("Z", "+00:00"))
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            ts = captured.timestamp()
            if ts < cutoff:
                continue
        except ValueError:
            continue
        key = str(row["name"]).lower().strip()
        if not key:
            continue
        entry = agg.setdefault(key, {"name": row["name"], "count": 0, "latest_ts": 0.0,
                                     "category": row.get("category"), "volume_text": row.get("volume_text"),
                                     "best_rank": row.get("rank"), "_early": 0, "_late": 0})
        entry["count"] += 1
        entry["_late" if ts >= midpoint else "_early"] += 1
        if ts > entry["latest_ts"]:
            entry["latest_ts"] = ts
            entry["volume_text"] = row.get("volume_text") or entry["volume_text"]
            entry["category"] = row.get("category") or entry["category"]
    result = []
    for entry in agg.values():
        recency = max(0.0, 1.0 - (now_ts - entry["latest_ts"]) / (hours * 3600.0))
        entry["rising_score"] = round(entry["count"] * (0.5 + 0.5 * recency), 2)
        early, late = entry.pop("_early"), entry.pop("_late")
        # velocity > 0: appearing more often now than earlier in the window.
        entry["velocity"] = round((late - early) / max(1, early + late), 2)
        # Velocity compares two halves of the window, so it needs something in
        # both — or enough sightings on one side that the imbalance cannot just
        # be when the user happened to start syncing. A single capture scored
        # velocity 1.0 and came back "rising", which is the strongest signal in
        # the whole timing engine: it overrides the calendar, and downstream it
        # suppresses scheduling outright. One observation cannot buy that.
        if early + late < 2 or (min(early, late) == 0 and early + late < 3):
            entry["lifecycle"] = "unknown"
            entry["velocity"] = 0.0
        elif late and entry["velocity"] >= 0.2:
            entry["lifecycle"] = "rising"
        elif late and entry["velocity"] > -0.34:
            entry["lifecycle"] = "peaking"
        else:
            # Seen only (or mostly) in the early half: the moment is passing.
            entry["lifecycle"] = "fading"
        entry.pop("latest_ts", None)
        result.append(entry)
    result.sort(key=lambda item: item["rising_score"], reverse=True)
    return result


def get_job_state(key: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT value_json FROM job_state WHERE key=?", (key,)).fetchone()
    return _from_json(row[0], {}) if row else {}


def set_job_state(key: str, value: dict) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO job_state(key,value_json,updated_at) VALUES(?,?,?)",
                     (key, _json(value), _utc()))


def mark_post_deleted(post_id: str) -> bool:
    """Record that a post no longer exists on X. Returns whether a row changed."""
    if not str(post_id or "").strip():
        return False
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE posts SET deleted_at=? WHERE post_id=? AND deleted_at IS NULL",
            (_utc(), str(post_id)))
        changed = cursor.rowcount > 0
    if changed:
        # Voice, shape and lane caches key on corpus_version; without this bump
        # a deleted post keeps teaching them until an unrelated write lands.
        mark_corpus_dirty()
    return changed


def performance_records() -> list[dict]:
    # A DELETED POST IS NOT PERFORMANCE. This feeds "what was my most recent
    # post", the critique answer, topic memory and every learning path that
    # reads views — and it was handing back a post he had already deleted,
    # which is how he found the bug. Filtered at the source rather than at each
    # caller, because there were eleven callers and one of them would be missed.
    posts = [post for post in records() if not post.get("deleted_at")]
    snapshots = list_metric_snapshots(limit=100000)
    by_post: dict[str, dict] = {}
    for snapshot in snapshots:
        by_post.setdefault(snapshot["post_id"], snapshot)
    for post in posts:
        if post["post_id"] in by_post:
            snap = by_post[post["post_id"]]
            post["metrics"] = snap["metrics"]
            post["metrics_updated_at"] = snap["captured_at"]
            post["followers_count"] = snap["followers_count"]
        else:
            post["followers_count"] = _nearest_followers(post.get("created_at"))
    return posts


def _nearest_followers(created_at: str | None) -> int | None:
    if not created_at:
        return None
    with connect() as conn:
        row = conn.execute("SELECT followers_count FROM profile_snapshots WHERE captured_at<=? AND followers_count IS NOT NULL ORDER BY captured_at DESC LIMIT 1", (created_at,)).fetchone()
        if not row:
            row = conn.execute("SELECT followers_count FROM profile_snapshots WHERE followers_count IS NOT NULL ORDER BY ABS(julianday(captured_at)-julianday(?)) LIMIT 1", (created_at,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _decode_json_columns(row: dict, names: Iterable[str]) -> dict:
    for name in names:
        key = name.removesuffix("_json")
        row[key] = _from_json(row.pop(name, None), [] if key in {"content_pillars", "guardrails", "variants", "tags"} else {})
    return row


def create_goal(data: dict) -> dict:
    identifier, now = str(uuid4()), _utc()
    with connect() as conn:
        conn.execute("INSERT INTO growth_goals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identifier, data["name"], data["objective"], data["baseline"], data["target"],
          _iso(data["horizon_at"]), _json(data.get("weekly_cadence", {})), _json(data.get("guardrails", [])), "active", now, now))
    return get_goal(identifier) or {}


def get_goal(identifier: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM growth_goals WHERE id=?", (identifier,)).fetchone()
    return _decode_json_columns(dict(row), ("weekly_cadence_json", "guardrails_json")) if row else None


def list_goals() -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM growth_goals ORDER BY created_at DESC")]
    return [_decode_json_columns(row, ("weekly_cadence_json", "guardrails_json")) for row in rows]


def update_goal(identifier: str, changes: dict) -> dict | None:
    mapping = {"name": "name", "baseline": "baseline", "target": "target", "horizon_at": "horizon_at", "weekly_cadence": "weekly_cadence_json", "guardrails": "guardrails_json", "status": "status"}
    values, args = [], []
    for key, column in mapping.items():
        if key not in changes or changes[key] is None:
            continue
        values.append(f"{column}=?")
        args.append(_json(changes[key]) if key in ("weekly_cadence", "guardrails") else _iso(changes[key]) if key == "horizon_at" else changes[key])
    if values:
        with connect() as conn:
            conn.execute(f"UPDATE growth_goals SET {','.join(values)},updated_at=? WHERE id=?", (*args, _utc(), identifier))
    return get_goal(identifier)


def create_campaign(data: dict) -> dict:
    identifier, now = str(uuid4()), _utc()
    with connect() as conn:
        conn.execute("INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?)", (identifier, data["name"], data.get("goal_id"), data.get("narrative", ""),
          _json(data.get("content_pillars", [])), "active", _json({"recent_angles": [], "claims": [], "commitments": [], "audience_feedback": [], "fatigue_score": 0.0}), now, now,))
    return get_campaign(identifier) or {}


def get_campaign(identifier: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (identifier,)).fetchone()
    return _decode_json_columns(dict(row), ("content_pillars_json", "memory_json")) if row else None


def list_campaigns() -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM campaigns ORDER BY updated_at DESC")]
    return [_decode_json_columns(row, ("content_pillars_json", "memory_json")) for row in rows]


def update_campaign(identifier: str, changes: dict) -> dict | None:
    mapping = {"name": "name", "goal_id": "goal_id", "narrative": "narrative", "content_pillars": "content_pillars_json", "status": "status", "memory": "memory_json"}
    values, args = [], []
    for key, column in mapping.items():
        if key not in changes or changes[key] is None:
            continue
        values.append(f"{column}=?")
        args.append(_json(changes[key]) if key in ("content_pillars", "memory") else changes[key])
    if values:
        with connect() as conn:
            conn.execute(f"UPDATE campaigns SET {','.join(values)},updated_at=? WHERE id=?", (*args, _utc(), identifier))
    return get_campaign(identifier)


def create_experiment(data: dict) -> dict:
    identifier, now = str(uuid4()), _utc()
    with connect() as conn:
        conn.execute("INSERT INTO experiments(id,name,campaign_id,hypothesis,variable,variants_json,primary_metric,min_sample_size,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
          (identifier, data["name"], data.get("campaign_id"), data["hypothesis"], data["variable"], _json(data["variants"]), data["primary_metric"], data["min_sample_size"], "draft", "{}", now, now))
    return get_experiment(identifier) or {}


def get_experiment(identifier: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM experiments WHERE id=?", (identifier,)).fetchone()
    return _decode_json_columns(dict(row), ("variants_json", "result_json")) if row else None


def list_experiments() -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM experiments ORDER BY created_at DESC")]
    return [_decode_json_columns(row, ("variants_json", "result_json")) for row in rows]


def update_experiment(identifier: str, changes: dict) -> dict | None:
    mapping = {"status": "status", "result": "result_json", "ended_at": "ended_at"}
    values, args = [], []
    for key, column in mapping.items():
        if key not in changes or changes[key] is None:
            continue
        values.append(f"{column}=?")
        args.append(_json(changes[key]) if key == "result" else _iso(changes[key]) if key == "ended_at" else changes[key])
    if changes.get("status") == "running":
        values.append("started_at=COALESCE(started_at,?)")
        args.append(_utc())
    if values:
        with connect() as conn:
            if changes.get("status") == "running":
                # Starling intentionally runs one explicit account-level experiment at a time.
                conn.execute("UPDATE experiments SET status='paused',updated_at=? WHERE status='running' AND id!=?", (_utc(), identifier))
            conn.execute(f"UPDATE experiments SET {','.join(values)},updated_at=? WHERE id=?", (*args, _utc(), identifier))
    return get_experiment(identifier)


def upsert_knowledge_source(data: dict) -> dict:
    identifier, now = str(uuid4()), _utc()
    with connect() as conn:
        conn.execute("""INSERT INTO knowledge_sources(id,url,title,publisher,published_at,claim,evidence_grade,policy_compatible,tags_json,notes,verified_at,created_at,updated_at,
          retrieved_at,review_after,source_version,applicability)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,publisher=excluded.publisher,published_at=excluded.published_at,
          claim=excluded.claim,evidence_grade=excluded.evidence_grade,policy_compatible=excluded.policy_compatible,tags_json=excluded.tags_json,
          notes=excluded.notes,verified_at=excluded.verified_at,updated_at=excluded.updated_at,retrieved_at=excluded.retrieved_at,
          review_after=excluded.review_after,source_version=excluded.source_version,applicability=excluded.applicability""",
          (identifier, data["url"], data["title"], data.get("publisher"), data.get("published_at"), data["claim"], data.get("evidence_grade", "unrated"),
           int(data.get("policy_compatible", True)), _json(data.get("tags", [])), data.get("notes", ""), _iso(data.get("verified_at")), now, now,
           data.get("retrieved_at") or now, data.get("review_after"), data.get("source_version"), data.get("applicability")))
    with connect() as conn:
        row = conn.execute("SELECT * FROM knowledge_sources WHERE url=?", (data["url"],)).fetchone()
    value = _decode_json_columns(dict(row), ("tags_json",))
    value["policy_compatible"] = bool(value["policy_compatible"])
    return value


def list_knowledge_sources(tags: list[str] | None = None, compatible_only: bool = True) -> list[dict]:
    clauses, args = [], []
    if compatible_only:
        clauses.append("policy_compatible=1")
    sql = "SELECT * FROM knowledge_sources" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY updated_at DESC"
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, args)]
    values = [_decode_json_columns(row, ("tags_json",)) for row in rows]
    if tags:
        wanted = {tag.lower() for tag in tags}
        values = [value for value in values if wanted & {tag.lower() for tag in value["tags"]}]
    for value in values:
        value["policy_compatible"] = bool(value["policy_compatible"])
        review_after = value.get("review_after")
        try:
            due = datetime.fromisoformat(str(review_after).replace("Z", "+00:00")) if review_after else None
            if due and due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            value["review_status"] = "stale" if due and due <= datetime.now(timezone.utc) else "current" if due else "unreviewed"
        except ValueError:
            value["review_status"] = "unreviewed"
    return values


def delete_knowledge_source(identifier: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM knowledge_sources WHERE id=?", (identifier,))
        return cursor.rowcount > 0


def save_strategic_state(state: dict, reason: str = "recomputed") -> dict:
    serialized = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO strategic_state_versions(state_hash,state_json,reason,created_at) VALUES(?,?,?,?)",
            (digest, serialized, reason, _utc()),
        )
        # Strategy history is inspectable, but bounded so ordinary use cannot grow the database forever.
        conn.execute("DELETE FROM strategic_state_versions WHERE id NOT IN (SELECT id FROM strategic_state_versions ORDER BY id DESC LIMIT 250)")
        row = conn.execute("SELECT * FROM strategic_state_versions WHERE state_hash=?", (digest,)).fetchone()
    value = dict(row)
    value["state"] = _from_json(value.pop("state_json"), {})
    return value


def latest_strategic_state() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM strategic_state_versions ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    value = dict(row)
    value["state"] = _from_json(value.pop("state_json"), {})
    return value


def create_generation(request: dict, strategy: dict | None = None) -> str:
    """Record a generation, INCLUDING the lane and register it was planned in.

    MIGRATION 14 CREATED THREE COLUMNS AND NOTHING WROTE THEM. Measured: 2,249 rows, zero
    non-null in `topic_lane`, `topic_lane_score` and `post_register`. This INSERT named eight
    columns and none of the three was among them.

    WHY THAT IS WORSE THAN AN EMPTY COLUMN. An all-NULL column does not error - it answers.
    "Do my vibe-coding posts do better than my frontier posts?" returns a clean empty result
    with no reason to doubt it, which is the exact question the column was added to answer and
    the exact way it lies. `idx_generations_topic_lane` is an index over an empty column.

    THE TWO HALVES ARE NOT THE SAME. `post_register` survives inside `request_json` and six
    gates read it off the request object, so that feature works and the history is a JSON parse
    away - it is the COLUMN that was lost, not the value. **The lane appears in no stored
    payload at all**: not `request_json`, not `strategy_json`, and `context_json` holds the
    parent post rather than the topic. Every press until the caller supplies it loses which
    lane it was about, and no backfill can recover it.

    Read from the request first and the strategy second so either caller shape works; the lane
    lands here the moment `main.py` threads it through.
    """
    identifier, now = str(uuid4()), _utc()
    context = request.get("context", {})
    plan = strategy or {}
    register = request.get("post_register") or request.get("register") or plan.get("post_register")
    topic_lane = request.get("topic_lane") or plan.get("topic_lane")
    lane_score = request.get("topic_lane_score")
    if lane_score is None:
        lane_score = plan.get("topic_lane_score")
    with connect() as conn:
        conn.execute("INSERT INTO generations(id,action,context_id,context_json,request_json,strategy_json,created_at,updated_at,post_register,topic_lane,topic_lane_score) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (identifier, request.get("action", "post"), context.get("context_id") or context.get("post_id"), _json(context), _json(request), _json(strategy), now, now,
           str(register) if register else None,
           str(topic_lane) if topic_lane else None,
           float(lane_score) if isinstance(lane_score, (int, float)) else None))
        conn.execute("INSERT INTO generation_events(generation_id,event,metadata_json,created_at) VALUES(?,?,?,?)", (identifier, "generated", _json({}), now))
    return identifier


def save_generation_candidates(generation_id: str, candidates: list[dict]) -> None:
    with connect() as conn:
        for index, candidate in enumerate(candidates):
            conn.execute("INSERT OR REPLACE INTO generation_candidates(candidate_id,generation_id,rank_index,text,candidate_json,created_at) VALUES(?,?,?,?,?,?)",
              (candidate["id"], generation_id, index, candidate.get("text", ""), _json(candidate), _utc()))


def get_generation(identifier: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM generations WHERE id=?", (identifier,)).fetchone()
        if not row:
            return None
        candidates = [dict(item) for item in conn.execute("SELECT * FROM generation_candidates WHERE generation_id=? ORDER BY rank_index", (identifier,))]
        events = [dict(item) for item in conn.execute("SELECT * FROM generation_events WHERE generation_id=? ORDER BY id", (identifier,))]
    value = _decode_json_columns(dict(row), ("context_json", "request_json", "strategy_json"))
    value["candidates"] = [_from_json(item["candidate_json"], {}) for item in candidates]
    for event in events:
        event["metadata"] = _from_json(event.pop("metadata_json"), {})
    value["events"] = events
    return value


def list_generations(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM generations ORDER BY created_at DESC LIMIT ?", (limit,))]
    return [_decode_json_columns(row, ("context_json", "request_json", "strategy_json")) for row in rows]


def record_outcome(data: dict) -> dict:
    identifier, status, now = data["generation_id"], data["status"], _utc()
    with connect() as conn:
        generation = conn.execute("SELECT * FROM generations WHERE id=?", (identifier,)).fetchone()
        if not generation:
            raise KeyError(identifier)
        updates = {"lifecycle_status": status, "updated_at": now}
        if data.get("candidate_id"):
            updates["selected_candidate_id"] = data["candidate_id"]
        if data.get("final_text") is not None:
            updates["final_text"] = data["final_text"]
        if status == "inserted":
            updates["inserted_at"] = now
        if status == "posted":
            updates["posted_at"] = _iso(data.get("posted_at")) or now
            updates["published_post_id"] = data.get("published_post_id")
        conn.execute(f"UPDATE generations SET {','.join(f'{key}=?' for key in updates)} WHERE id=?", (*updates.values(), identifier))
        conn.execute("INSERT INTO generation_events(generation_id,candidate_id,event,text,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
          (identifier, data.get("candidate_id"), status, data.get("final_text"), _json(data.get("metadata", {})), now))
        if status == "posted":
            strategy = _from_json(generation["strategy_json"], {})
            campaign_id = (strategy.get("campaign") or {}).get("id")
            if campaign_id:
                campaign = conn.execute("SELECT memory_json FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
                if campaign:
                    memory = _from_json(campaign["memory_json"], {})
                    final_text = str(data.get("final_text") or generation["final_text"] or "").strip()
                    if final_text:
                        recent_angles = list(memory.get("recent_angles", []))
                        recent_angles.append(final_text[:240])
                        memory["recent_angles"] = recent_angles[-8:]
                    memory["last_posted_at"] = updates.get("posted_at", now)
                    memory["posted_count"] = int(memory.get("posted_count", 0) or 0) + 1
                    conn.execute("UPDATE campaigns SET memory_json=?,updated_at=? WHERE id=?", (_json(memory), now, campaign_id))
    mark_corpus_dirty()
    return get_generation(identifier) or {}


def _canonical_draft_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def match_visible_post_to_generation(post_id: str, text: str | None, post_type: str | None, captured_at: Any = None, source: str = "extension") -> str | None:
    """Mark one recent inserted generation as posted on an exact visible match.

    Exact normalization is intentional: visible post capture must never guess
    that a merely similar post came from Starling.
    """
    candidate_text = _canonical_draft_text(text)
    if not candidate_text:
        return None
    expected_action = {"reply": "reply", "quote": "quote"}.get(str(post_type or ""), "post")
    now = _iso(captured_at) or _utc()
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("""
            SELECT id, action, final_text, context_json FROM generations
            WHERE lifecycle_status IN ('inserted','edited') AND published_post_id IS NULL
            ORDER BY updated_at DESC LIMIT 80
        """)]
        for row in rows:
            if row["action"] != expected_action:
                continue
            values = [row.get("final_text")]
            values.extend(item[0] for item in conn.execute("SELECT text FROM generation_candidates WHERE generation_id=?", (row["id"],)))
            if not any(_canonical_draft_text(value) == candidate_text for value in values):
                continue
            conn.execute("""UPDATE generations SET lifecycle_status='posted', posted_at=?, published_post_id=?,
                final_text=COALESCE(final_text,?), updated_at=? WHERE id=?""", (now, post_id, text, now, row["id"]))
            # Profile timelines can omit a reply's parent permalink. An exact
            # visible match to a Starling generation still proves its target:
            # the composer context was captured before the user posted.
            if expected_action == "reply":
                context = _from_json(row.get("context_json"), {})
                parent_id = str(context.get("post_id") or "").strip()
                if parent_id:
                    conn.execute("""UPDATE posts SET
                        parent_post_id=COALESCE(parent_post_id, ?),
                        parent_text=COALESCE(parent_text, ?),
                        parent_author=COALESCE(parent_author, ?),
                        parent_post_url=COALESCE(parent_post_url, ?)
                        WHERE post_id=?""", (
                        parent_id, context.get("text"), context.get("author"),
                        context.get("post_url"), post_id,
                    ))
            # The visible post is the proof that an inserted Starling draft was
            # actually published. A draft left in a composer never earns this
            # origin and therefore cannot masquerade as writing evidence.
            conn.execute("UPDATE posts SET origin='starling_verified' WHERE post_id=?", (post_id,))
            conn.execute("INSERT INTO generation_events(generation_id,event,text,metadata_json,created_at) VALUES(?,?,?,?,?)",
                         (row["id"], "matched_visible_post", text, _json({"post_id": post_id, "source": source}), now))
            mark_corpus_dirty()
            return str(row["id"])
    return None


def find_recorded_reply_to_post(parent_post_id: str | None) -> dict | None:
    """Return one known authored reply to this exact parent post.

    This is intentionally an identity check, not semantic similarity. A reply
    drafted by Starling is linked through its persisted context; a reply written
    directly in X is linked through the visible-post capture's parent id. Both
    routes survive a browser refresh, unlike the old in-memory composer state.
    """
    parent_id = str(parent_post_id or "").strip()
    if not parent_id:
        return None
    with connect() as conn:
        row = conn.execute("""
            SELECT post_id, text, created_at FROM posts
            WHERE post_type='reply' AND parent_post_id=?
            ORDER BY COALESCE(created_at, first_observed_at) DESC LIMIT 1
        """, (parent_id,)).fetchone()
        if row:
            return {"source": "visible_own_reply", "post_id": row["post_id"],
                    "text": row["text"], "created_at": row["created_at"]}
        rows = conn.execute("""
            SELECT id, context_json, final_text, posted_at, published_post_id
            FROM generations WHERE action='reply' AND lifecycle_status='posted'
            ORDER BY posted_at DESC, updated_at DESC LIMIT 200
        """).fetchall()
    for row in rows:
        context = _from_json(row["context_json"], {})
        if str(context.get("post_id") or "").strip() != parent_id:
            continue
        return {"source": "starling_posted_reply", "generation_id": row["id"],
                "post_id": row["published_post_id"], "text": row["final_text"],
                "created_at": row["posted_at"]}
    return None


def latest_bridge_context(kind: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM bridge_contexts WHERE kind=? ORDER BY captured_at DESC,id DESC LIMIT 1", (kind,)).fetchone()
    if not row:
        return None
    return {"id": int(row["id"]), "kind": row["kind"], "data": _from_json(row["payload_json"], {}), "captured_at": row["captured_at"]}


def save_strategy_message(conversation_id: str, role: str, content: str, context: dict | None = None) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO strategy_messages(conversation_id,role,content,context_json,created_at) VALUES(?,?,?,?,?)", (conversation_id, role, content, _json(context), _utc()))


def strategy_messages(conversation_id: str, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM strategy_messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (conversation_id, limit))]
    return [{"role": row["role"], "content": row["content"], "context": _from_json(row["context_json"], {}), "created_at": row["created_at"]} for row in reversed(rows)]


def recent_strategy_instructions(limit: int = 12) -> list[str]:
    """Small local retrieval of recent user direction across chats."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT content FROM strategy_messages WHERE role='user' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    values = [str(row[0]).strip() for row in rows if str(row[0]).strip()]
    return list(reversed(values))


def save_preference_proposal(source_text: str, rule: str, scope: str, confidence: float) -> dict:
    identifier, now = str(uuid4()), _utc()
    with connect() as conn:
        conn.execute("""INSERT INTO preference_memories(id,source_text,rule,scope,confidence,status,created_at,updated_at)
          VALUES(?,?,?,?,?,'proposed',?,?)""", (identifier, source_text, rule, scope, float(confidence), now, now))
    return get_preference(identifier) or {}


def get_preference(identifier: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM preference_memories WHERE id=?", (identifier,)).fetchone()
    return dict(row) if row else None


def confirm_preference(identifier: str | None, source_text: str, rule: str, scope: str, confirmed: bool) -> dict:
    now = _utc()
    with connect() as conn:
        row = conn.execute("SELECT * FROM preference_memories WHERE id=?", (identifier,)).fetchone() if identifier else None
        if row:
            previous = dict(row)
            status = "confirmed" if confirmed else "dismissed"
            conn.execute("UPDATE preference_memories SET status=?,confirmed_at=?,updated_at=? WHERE id=?",
                         (status, now if confirmed else None, now, previous["id"]))
            result_id = previous["id"]
        else:
            result_id = str(uuid4())
            status = "confirmed" if confirmed else "dismissed"
            conn.execute("""INSERT INTO preference_memories(id,source_text,rule,scope,confidence,status,created_at,confirmed_at,updated_at)
              VALUES(?,?,?,?,?,?,?, ?,?)""", (result_id, source_text, rule, scope, 0.7, status, now, now if confirmed else None, now))
        if confirmed:
            # Keep a clear audit trail while ensuring one current rule wins inside each scope.
            conn.execute("UPDATE preference_memories SET status='superseded',updated_at=? WHERE scope=? AND status='confirmed' AND id<>? AND rule=?",
                         (now, scope, result_id, rule))
    return get_preference(result_id) or {}


def confirmed_preferences(limit: int = 80) -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("""SELECT * FROM preference_memories
          WHERE status='confirmed' ORDER BY confirmed_at DESC LIMIT ?""", (limit,))]
    return rows


_TOPIC_MEMORY_WORD = re.compile(r"[a-z0-9][a-z0-9+.#'/-]*", re.IGNORECASE)
_TOPIC_MEMORY_STOP = {
    "about", "around", "focus", "write", "post", "tweet", "topic", "more",
    "want", "would", "rather", "prefer", "cover", "talk", "this", "that",
}


def _topic_memory_key(topic: str) -> str:
    normalized = str(topic).casefold().replace("-", " ").replace("/", " ")
    return " ".join(_TOPIC_MEMORY_WORD.findall(normalized))[:180]


def _topic_memory_tokens(value: str) -> set[str]:
    normalized = str(value).replace("-", " ").replace("/", " ")
    return {token.casefold() for token in _TOPIC_MEMORY_WORD.findall(normalized)
            if token.casefold() not in _TOPIC_MEMORY_STOP}


def _decode_topic_memory(row: sqlite3.Row | dict) -> dict:
    value = dict(row)
    explicit = int(value.get("explicit_signals") or 0)
    verified = int(value.get("verified_posts") or 0)
    # A direct request is stronger than a derived pattern. Verification raises
    # confidence but can never fabricate intent the user did not state.
    value["priority"] = round(min(1.0, 0.55 + max(0, explicit - 1) * 0.12 + verified * 0.10), 2)
    value["confidence"] = "reinforced" if verified else "direct"
    value["active"] = bool(value.get("active"))
    return value


def get_topic_memory(topic_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM topic_memories WHERE topic_key=?", (topic_key,)).fetchone()
    return _decode_topic_memory(row) if row else None


def active_topic_memories(limit: int = 12) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("""SELECT * FROM topic_memories WHERE active=1
            ORDER BY COALESCE(last_explicit_at, created_at) DESC,
                     verified_posts DESC, updated_at DESC LIMIT ?""", (limit,)).fetchall()
    return [_decode_topic_memory(row) for row in rows]


def remember_topic_focus(topic: str, *, source_text: str, conversation_id: str | None = None) -> dict:
    """Record one explicit topic choice, safely handling retried chat messages."""
    key = _topic_memory_key(topic)
    if not key:
        raise ValueError("A topic must contain readable text")
    now = _utc()
    source_ref = hashlib.sha256(
        f"chat:{conversation_id or 'none'}:{source_text.strip()}".encode("utf-8")
    ).hexdigest()
    event_id = str(uuid4())
    with connect() as conn:
        conn.execute("""INSERT INTO topic_memories(topic_key,topic,explicit_signals,verified_posts,active,
              last_explicit_at,created_at,updated_at)
            VALUES(?,?,0,0,1,?,?,?)
            ON CONFLICT(topic_key) DO UPDATE SET topic=excluded.topic,active=1,updated_at=excluded.updated_at""",
                     (key, topic.strip()[:160], now, now, now))
        cursor = conn.execute("""INSERT OR IGNORE INTO topic_memory_events(
              id,topic_key,event_type,source_ref,metadata_json,created_at)
            VALUES(?,?, 'explicit_chat_topic', ?, ?, ?)""",
                              (event_id, key, source_ref,
                               _json({"message": source_text[:800], "conversation_id": conversation_id}), now))
        if cursor.rowcount:
            conn.execute("""UPDATE topic_memories SET explicit_signals=explicit_signals+1,
                last_explicit_at=?,active=1,updated_at=? WHERE topic_key=?""", (now, now, key))
        row = conn.execute("SELECT * FROM topic_memories WHERE topic_key=?", (key,)).fetchone()
    mark_corpus_dirty()
    return _decode_topic_memory(row) if row else {}


def reinforce_topic_memories_from_verified_post(post_id: str, text: str, *, verification_method: str) -> list[dict]:
    """Strengthen only explicit focuses that the verified post actually covers.

    The unique event key means a post revisited ten times by passive sync counts
    once, while a distinct future post can add fresh evidence.
    """
    post_tokens = _topic_memory_tokens(text)
    if not post_tokens:
        return []
    now, updated = _utc(), []
    with connect() as conn:
        rows = conn.execute("SELECT * FROM topic_memories WHERE active=1").fetchall()
        for row in rows:
            memory = _decode_topic_memory(row)
            topic_tokens = _topic_memory_tokens(memory["topic"])
            if not topic_tokens:
                continue
            shared = len(topic_tokens & post_tokens)
            required = 1 if len(topic_tokens) <= 2 else max(2, (len(topic_tokens) * 3 + 4) // 5)
            if shared < required:
                continue
            cursor = conn.execute("""INSERT OR IGNORE INTO topic_memory_events(
                id,topic_key,event_type,source_ref,metadata_json,created_at)
                VALUES(?,?, 'verified_post', ?, ?, ?)""",
                                  (str(uuid4()), memory["topic_key"], str(post_id),
                                   _json({"method": verification_method, "shared_tokens": shared}), now))
            if not cursor.rowcount:
                continue
            conn.execute("""UPDATE topic_memories SET verified_posts=verified_posts+1,
                last_verified_at=?,updated_at=? WHERE topic_key=?""",
                         (now, now, memory["topic_key"]))
            refreshed = conn.execute("SELECT * FROM topic_memories WHERE topic_key=?",
                                     (memory["topic_key"],)).fetchone()
            if refreshed:
                updated.append(_decode_topic_memory(refreshed))
    if updated:
        mark_corpus_dirty()
    return updated


def dismiss_topic_memory(topic_key: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("UPDATE topic_memories SET active=0,updated_at=? WHERE topic_key=?",
                              (_utc(), topic_key))
    if cursor.rowcount:
        mark_corpus_dirty()
    return bool(cursor.rowcount)


def save_opportunity_assessment(context: dict, assessment: dict) -> dict:
    normalized = _json({"post_id": context.get("post_id"), "post_url": context.get("post_url"), "author": context.get("author"), "text": context.get("text")})
    context_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    identifier, now = str(uuid4()), _utc()
    with connect() as conn:
        conn.execute("""INSERT INTO opportunity_assessments(id,context_hash,action,score,assessment_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?)""", (identifier, context_hash, str(assessment.get("action", "wait")), float(assessment.get("score", 0.0)), _json(assessment), now, now))
    return {"id": identifier, "context_hash": context_hash, **assessment, "created_at": now}


def recent_opportunity_assessments(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM opportunity_assessments ORDER BY created_at DESC LIMIT ?", (limit,))]
    return [{"id": row["id"], **_from_json(row["assessment_json"], {}), "created_at": row["created_at"]} for row in rows]


def save_safety_event(category: str, severity: str, blocked: bool, reasons: list[str], context: dict | None = None) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO safety_events(category,severity,blocked,reasons_json,context_json,created_at) VALUES(?,?,?,?,?,?)",
          (category, severity, int(blocked), _json(reasons), _json(context), _utc()))


def replies_already_drafted_for(post_id: str, limit: int = 4) -> list[str]:
    """Replies already written for THIS post, newest first.

    Pressing the button a second time on the same post is a RETRY, and a retry
    that returns the same thought is not one. Measured across a full arena run:
    seven of nine drafts that failed the clean bar were a second round on a
    parent already answered — near-identical, because the same context produces
    the same room, the same move and the same plan.

    `previously_drafted_replies` cannot answer this: it is the whole recent
    pile with no idea which post each belongs to. The link is in
    `generations.context_json`, which is JSON, so the id is matched as a
    substring against the exact quoted key — narrow enough not to collide with
    `parent_post_id` or a longer id that merely starts the same way.
    """
    needle = f'"post_id": "{post_id}"'
    alt = f'"post_id":"{post_id}"'                    # whichever way it was dumped
    with connect() as conn:
        rows = conn.execute("""
          SELECT c.text FROM generation_candidates c
          JOIN generations g ON g.id = c.generation_id
          WHERE g.action = 'reply' AND c.text IS NOT NULL
            AND (g.context_json LIKE ? OR g.context_json LIKE ?)
          ORDER BY c.created_at DESC LIMIT ?
        """, (f"%{needle}%", f"%{alt}%", limit)).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def recent_draft_rejections(since_iso: str) -> tuple[int, list[str]]:
    """Why drafts were thrown away since `since_iso`, newest first.

    The card that appears when nothing survives the gates has to say WHICH
    thing happened, because the two cases need opposite reactions from him:

      the model returned nothing        -> check that it is running
      the model returned three drafts   -> retry, or give me the angle
      and every one was rejected

    `main` tried to tell them apart with "were any candidates saved", and that
    can never work — a rejected draft is never saved as a candidate, so both
    cases look identical from there. Live, an all-rejected reply told him
    "Nothing usable came back from the model this time. Try again — and if
    Starling's status dot is red, the model isn't running", which sent him
    hunting a stopped model that was answering fine. The rejections are already
    written down here; this reads them back.
    """
    with connect() as conn:
        rows = conn.execute("""
          SELECT reasons_json FROM safety_events
          WHERE category = 'draft' AND created_at >= ?
          ORDER BY created_at DESC LIMIT 40
        """, (since_iso,)).fetchall()
    # TWO ANSWERS, because they are different questions. Every draft that is
    # ASSESSED writes a row here, clean or not — a clean one carries an empty
    # reason list. So the row COUNT says whether the model produced anything at
    # all, and only the non-empty lists say why anything was thrown away.
    # Reading just the reasons made an all-rejected reply indistinguishable
    # from a dead model, which is the bug this function exists to fix.
    reasons: list[str] = []
    for row in rows:
        try:
            reasons.extend(str(item) for item in json.loads(row[0] or "[]"))
        except Exception:                                   # noqa: BLE001
            continue
    return len(rows), reasons


def save_user_source(url: str | None, title: str | None, text: str, summary: str | None) -> dict:
    identifier, now = str(uuid4()), _utc()
    with connect() as conn:
        conn.execute("INSERT INTO user_sources(id,url,title,text,summary,created_at) VALUES(?,?,?,?,?,?)",
                     (identifier, url, title, text, summary, now))
    return {"id": identifier, "url": url, "title": title, "summary": summary, "created_at": now,
            "text_length": len(text)}


def list_user_sources(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT id,url,title,summary,created_at,length(text) AS text_length FROM user_sources ORDER BY created_at DESC LIMIT ?",
            (limit,))]
    return rows


def get_user_sources(ids: Iterable[str]) -> list[dict]:
    identifiers = [str(item) for item in ids]
    if not identifiers:
        return []
    placeholders = ",".join("?" for _ in identifiers)
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            f"SELECT id,url,title,text,summary,created_at FROM user_sources WHERE id IN ({placeholders})",
            identifiers)]
    order = {identifier: index for index, identifier in enumerate(identifiers)}
    rows.sort(key=lambda row: order.get(row["id"], 99))
    return rows


def delete_user_source(identifier: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM user_sources WHERE id=?", (identifier,))
    return cursor.rowcount > 0


# The only tables that survive "delete everything". `schema_migrations` is
# bookkeeping about the SHAPE of the database, not content — clearing it would
# make the next start re-run every migration against an already-migrated file.
_CLEAR_ALL_KEEP = frozenset({"schema_migrations"})


def export_profile() -> list[dict]:
    return records()


EXPORT_ROW_LIMIT = 50_000


def _export_rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, (EXPORT_ROW_LIMIT,))]


def export_bundle() -> dict:
    """Everything this customer's engine holds about them, for `GET /profile/export`.

    It must include what they wrote and what Starling made for them: their own material, the drafts and
    their candidates, the ratings they gave, their queue and the pages they saved.
    """
    with connect() as conn:
        drafting = {
            "generation_candidates": _export_rows(conn, "SELECT * FROM generation_candidates ORDER BY rowid LIMIT ?"),
            "feedback": _export_rows(conn, "SELECT * FROM feedback ORDER BY id LIMIT ?"),
            "image_feedback": _export_rows(conn, "SELECT * FROM image_feedback ORDER BY id LIMIT ?"),
            "topic_saves": _export_rows(conn, "SELECT * FROM topic_saves ORDER BY id LIMIT ?"),
        }
    return {
        "account": get_account_profile(), "posts": records(), "profile_snapshots": list_profile_snapshots(),
        "metric_snapshots": list_metric_snapshots(), "goals": list_goals(), "campaigns": list_campaigns(),
        "experiments": list_experiments(), "knowledge_sources": list_knowledge_sources(compatible_only=False),
        "strategic_state": latest_strategic_state(), "preferences": confirmed_preferences(),
        "opportunities": recent_opportunity_assessments(),
        "generations": list_generations(limit=EXPORT_ROW_LIMIT),
        "scheduled_drafts": list_scheduled_drafts(status=None),
        "user_sources": list_user_sources(limit=EXPORT_ROW_LIMIT),
        **drafting,
    }


# --------------------------------------------------------------------------- #
# Live Account Memory (spec §3.2)
# --------------------------------------------------------------------------- #

def record_observed_post(record: dict, *, observed_at: str | None = None) -> dict:
    """Record an authored post Starling did NOT generate.

    Previously impossible: `match_visible_post_to_generation` only handles the
    inverse (linking a visible post back to a draft this app produced), so a
    post written from the user's phone, or published directly on X, stayed
    invisible to cadence, duplication and voice.

    `origin` marks how the row arrived so nothing later mistakes a live
    observation for an archive import, and `first_observed_at` survives a
    conflict — re-seeing a post does not reset when we first saw it. Why the
    user wrote it is never inferred (§3.2).
    """
    seen = _iso(observed_at) or _utc()
    payload = dict(record)
    payload.setdefault("source_file", "live-observation")
    upsert_post(payload)
    with connect() as conn:
        conn.execute(
            "UPDATE posts SET origin = COALESCE(origin, ?), "
            "author_handle = COALESCE(?, author_handle), "
            "first_observed_at = COALESCE(first_observed_at, ?) WHERE post_id = ?",
            (str(record.get("origin") or "external"), record.get("author_handle"),
             seen, str(record["post_id"])),
        )
        row = conn.execute("SELECT * FROM posts WHERE post_id=?",
                           (str(record["post_id"]),)).fetchone()
    mark_corpus_dirty()
    return _decode_post(row) if row else {}


def latest_observation_at() -> str | None:
    """When Starling last saw ANY of the user's own activity.

    Feeds the freshness sentence in §3.1 — "I last synced this profile 4 minutes
    ago" — and returns None rather than a guess when nothing was ever observed.
    """
    with connect() as conn:
        candidates = [
            conn.execute("SELECT MAX(first_observed_at) FROM posts").fetchone()[0],
            conn.execute("SELECT MAX(captured_at) FROM profile_snapshots").fetchone()[0],
            conn.execute("SELECT MAX(captured_at) FROM metric_snapshots").fetchone()[0],
        ]
    stamps = [str(value) for value in candidates if value]
    return max(stamps) if stamps else None


def deleted_post_texts(limit: int = 120) -> list[str]:
    """Text of posts that were LIVE and then taken down, newest first.

    The counterpart to `archive_texts(["deleted_tweet"])`, which only knows
    about deletions recorded in the once-ever archive export. A post the user
    published and then deleted is the strongest style-rejection signal the
    product can observe — he saw it up and pulled it — and it was reaching
    nothing.

    Style-only, exactly like its archive twin: never an engagement label,
    never a few-shot example.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT text FROM posts WHERE deleted_at IS NOT NULL AND text IS NOT NULL "
            "AND TRIM(text) <> '' ORDER BY deleted_at DESC LIMIT ?",
            (int(limit),)).fetchall()
    return [str(row[0]) for row in rows]


def observed_post_counts() -> dict[str, int]:
    """How the learning corpus was gathered, by origin.

    `external` = the watcher saw a post this app did not draft (his own
    hand). `starling_verified` = a Starling draft matched to a live post.
    `archive` = imported history (origin NULL). Deleted rows are counted
    separately and excluded from the rest — they no longer teach anything.
    """
    counts = {"external": 0, "starling_verified": 0, "archive": 0,
              "deleted": 0, "total": 0}
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(origin,'archive') AS origin, "
            "SUM(deleted_at IS NULL) AS live, SUM(deleted_at IS NOT NULL) AS gone "
            "FROM posts GROUP BY 1").fetchall()
    for row in rows:
        origin = str(row["origin"] or "archive")
        live = int(row["live"] or 0)
        counts[origin] = counts.get(origin, 0) + live
        counts["deleted"] += int(row["gone"] or 0)
        counts["total"] += live
    return counts


def externally_authored_posts(limit: int = 50) -> list[dict]:
    """Posts observed live that this app did not draft, newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE origin='external' "
            "ORDER BY COALESCE(created_at, first_observed_at) DESC LIMIT ?",
            (limit,)).fetchall()
    return [_decode_post(row) for row in rows]


# --------------------------------------------------------------------------- #
# Author relationships (spec §2.6 / §4.2)
# --------------------------------------------------------------------------- #

def upsert_author_relationship(record: dict) -> None:
    handle = str(record.get("handle") or "").strip().lstrip("@").lower()
    if not handle:
        return
    with connect() as conn:
        conn.execute("""
        INSERT INTO author_relationships(
          handle, display_name, interactions, replies, quotes, topic_overlap,
          outcome_percentile, distinctness, last_interaction_at, first_interaction_at,
          safety_fit, sample_size, confidence, topics_json, notes, updated_at)
        VALUES(:handle,:display_name,:interactions,:replies,:quotes,:topic_overlap,
          :outcome_percentile,:distinctness,:last_interaction_at,:first_interaction_at,
          :safety_fit,:sample_size,:confidence,:topics_json,:notes,:updated_at)
        ON CONFLICT(handle) DO UPDATE SET
          display_name=COALESCE(excluded.display_name, author_relationships.display_name),
          interactions=excluded.interactions, replies=excluded.replies, quotes=excluded.quotes,
          topic_overlap=excluded.topic_overlap, outcome_percentile=excluded.outcome_percentile,
          distinctness=excluded.distinctness, last_interaction_at=excluded.last_interaction_at,
          first_interaction_at=COALESCE(author_relationships.first_interaction_at, excluded.first_interaction_at),
          safety_fit=excluded.safety_fit, sample_size=excluded.sample_size,
          confidence=excluded.confidence, topics_json=excluded.topics_json,
          notes=excluded.notes, updated_at=excluded.updated_at
        """, {
            "handle": handle, "display_name": record.get("display_name"),
            "interactions": int(record.get("interactions", 0)),
            "replies": int(record.get("replies", 0)), "quotes": int(record.get("quotes", 0)),
            "topic_overlap": record.get("topic_overlap"),
            "outcome_percentile": record.get("outcome_percentile"),
            "distinctness": record.get("distinctness"),
            "last_interaction_at": _iso(record.get("last_interaction_at")),
            "first_interaction_at": _iso(record.get("first_interaction_at")),
            "safety_fit": record.get("safety_fit"),
            "sample_size": int(record.get("sample_size", 0)),
            "confidence": str(record.get("confidence", "unknown")),
            "topics_json": _json(record.get("topics", [])),
            "notes": record.get("notes"), "updated_at": _utc(),
        })


def list_author_relationships(limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM author_relationships "
            "ORDER BY interactions DESC, last_interaction_at DESC LIMIT ?", (limit,))]
    for row in rows:
        row["topics"] = _from_json(row.pop("topics_json"), [])
    return rows


def get_author_relationship(handle: str) -> dict | None:
    key = str(handle or "").strip().lstrip("@").lower()
    if not key:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM author_relationships WHERE handle=?", (key,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["topics"] = _from_json(value.pop("topics_json"), [])
    return value


def replies_to_author(handle: str, *, since: str | None = None) -> list[dict]:
    """The user's own replies/quotes aimed at one account.

    Powers the repeated-target and conversation-uniqueness penalties (§4.1c,
    §6.3), which otherwise have no data to work from.
    """
    key = str(handle or "").strip().lstrip("@").lower()
    if not key:
        return []
    # Prefer the resolved target; fall back to the archive's own parent_author
    # so this still works before the first relationship rebuild.
    sql = ["SELECT * FROM posts WHERE post_type IN ('reply','quote')",
           "AND (reply_target_handle = ?",
           "     OR LOWER(REPLACE(COALESCE(parent_author,''),'@','')) = ?)"]
    args: list[Any] = [key, key]
    if since:
        sql.append("AND COALESCE(created_at, first_observed_at) >= ?")
        args.append(since)
    sql.append("ORDER BY COALESCE(created_at, first_observed_at) DESC LIMIT 200")
    with connect() as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()
    return [_decode_post(row) for row in rows]


# --------------------------------------------------------------------------- #
# Account Intelligence Profile (spec §2.7)
# --------------------------------------------------------------------------- #

def save_account_profile(profile: dict, *, gaps: list | None = None,
                         preliminary: bool = True,
                         evidence_through: str | None = None) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO account_profiles(built_at, evidence_through, preliminary, "
            "profile_json, gaps_json, corpus_version) VALUES(?,?,?,?,?,?)",
            (_utc(), _iso(evidence_through), 1 if preliminary else 0,
             _json(profile), _json(gaps or []), corpus_version()))
        return int(cursor.lastrowid)


def latest_account_profile() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM account_profiles ORDER BY version DESC LIMIT 1").fetchone()
    if not row:
        return None
    value = dict(row)
    value["profile"] = _from_json(value.pop("profile_json"), {})
    value["gaps"] = _from_json(value.pop("gaps_json"), [])
    value["preliminary"] = bool(value.get("preliminary"))
    return value
