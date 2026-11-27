"""Phase 1 foundation: evidence, momentum, relationships, live memory, profile.

The rule these all serve is spec §1: **unknown data must remain unknown**. A
missing reply count is not zero replies, one snapshot is not proof of movement,
and an unmeasured relationship term is not a middling score.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import evidence as ev
from app import momentum, profile, relationships, storage

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _snapshot(post_id: str, minutes_ago: float, **metrics):
    storage.save_metric_snapshot({
        "post_id": post_id, "captured_at": _iso(minutes_ago),
        "metrics": metrics, "source": "test"})


def _post(post_id: str, text: str, post_type: str = "post", **kw):
    storage.upsert_post({"post_id": post_id, "text": text, "post_type": post_type,
                         "created_at": _iso(600), "source_file": "test", **kw})


# --------------------------------------------------------------------------- #
# Migration safety
# --------------------------------------------------------------------------- #

def test_initialize_is_idempotent_on_a_populated_database():
    """Startup runs migrations every boot; a second pass must change nothing."""
    _post("keep1", "A post that must survive a second initialize() call.")
    storage.initialize()
    storage.initialize()
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM posts WHERE post_id='keep1'").fetchone()[0] == 1
        applied = [row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
    assert applied == sorted(set(applied)), "a migration must never be applied twice"
    assert applied == list(range(1, len(storage.MIGRATIONS) + 1))


def test_every_new_column_exists_after_migration():
    """Guards the hazard that bit this build: editing an ALREADY-APPLIED
    migration is silently skipped by the runner, so a live database never gets
    the new columns even though a fresh test database does. New columns belong
    in a NEW migration."""
    storage.initialize()
    with storage.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
    for column in ("origin", "first_observed_at", "author_handle",
                   "reply_target_handle", "reply_target_basis"):
        assert column in columns, f"posts.{column} missing after migration"


def test_new_columns_are_nullable_so_old_rows_survive():
    """§ storage rules: never rewrite an archive to add a column."""
    _post("old1", "A row written before the new columns existed.")
    storage.initialize()
    with storage.connect() as conn:
        row = conn.execute("SELECT origin, reply_target_handle FROM posts WHERE post_id='old1'").fetchone()
    assert row["origin"] is None and row["reply_target_handle"] is None


# --------------------------------------------------------------------------- #
# Evidence envelope (§1)
# --------------------------------------------------------------------------- #

def test_a_missing_value_is_unknown_not_zero():
    item = ev.observed(None, observed_at=_iso(1))
    assert item.known is False
    assert item.value is None, "a missing metric must never read as 0"
    assert item.confidence == ev.UNKNOWN_CONFIDENCE


def test_freshness_degrades_with_age():
    assert ev.freshness_of(_iso(5), now=NOW) == ev.FRESH
    assert ev.freshness_of(_iso(120), now=NOW) == ev.AGING
    assert ev.freshness_of(_iso(60 * 48), now=NOW) == ev.STALE


def test_a_missing_timestamp_is_unknown_not_stale():
    """Not knowing WHEN differs from knowing it was long ago — §3.1 needs both."""
    assert ev.freshness_of(None, now=NOW) == ev.UNKNOWN_FRESHNESS
    assert ev.freshness_of("not a date", now=NOW) == ev.UNKNOWN_FRESHNESS


def test_a_forecast_may_never_be_stated_as_fact():
    assert ev.forecast(0.7).may_state_as_fact() is False
    assert ev.derived(0.7, sample_size=99).may_state_as_fact() is False
    assert ev.observed(7, observed_at=_iso(1)).may_state_as_fact() is True


def test_confidence_tracks_sample_size():
    assert ev.confidence_for_samples(None) == ev.UNKNOWN_CONFIDENCE
    assert ev.confidence_for_samples(3) == ev.LIMITED
    assert ev.confidence_for_samples(10) == ev.DIRECTIONAL
    assert ev.confidence_for_samples(50) == ev.STRONG


def test_a_chain_is_only_as_strong_as_its_weakest_link():
    strong = ev.derived(1, sample_size=100)
    limited = ev.derived(1, sample_size=2)
    assert ev.weakest(strong, limited) == ev.LIMITED


def test_freshness_basis_says_so_when_there_is_no_observation():
    assert "do not have a current observation" in ev.freshness_basis(None)
    assert "minute" in ev.freshness_basis(_iso(4), now=NOW)


# --------------------------------------------------------------------------- #
# Momentum (§5.2) — the capability that did not exist
# --------------------------------------------------------------------------- #

def test_no_snapshots_is_unknown():
    result = momentum.measure("missing", "views")
    assert result.basis == "none"
    assert result.velocity is None
    assert result.may_claim_takeoff() is False


def test_one_snapshot_gives_an_average_and_forbids_takeoff():
    """§5.2: a single snapshot must never produce 'this is taking off'."""
    _snapshot("p1", 60, views=600)
    result = momentum.measure("p1", "views", posted_at=_iso(120), now=NOW)
    assert result.basis == "average"
    assert result.velocity is None, "one point cannot yield a rate"
    assert result.lifetime_average == pytest.approx(10.0, rel=0.01)
    assert result.may_claim_takeoff() is False


def test_two_snapshots_give_a_real_velocity():
    _snapshot("p2", 60, views=100)
    _snapshot("p2", 30, views=400)
    result = momentum.measure("p2", "views", posted_at=_iso(120), now=NOW)
    assert result.basis == "velocity"
    assert result.velocity == pytest.approx(10.0, rel=0.01)  # 300 views / 30 min
    assert result.acceleration is None, "acceleration needs a third point"


def test_three_snapshots_detect_acceleration():
    _snapshot("p3", 90, views=0)
    _snapshot("p3", 60, views=100)     # 3.33/min
    _snapshot("p3", 30, views=500)     # 13.3/min
    result = momentum.measure("p3", "views", posted_at=_iso(120), now=NOW)
    assert result.basis == "acceleration"
    assert result.acceleration > 0
    assert result.trend == "accelerating"
    assert result.may_claim_takeoff() is True


def test_a_slowing_post_is_not_takeoff():
    _snapshot("p4", 90, views=0)
    _snapshot("p4", 60, views=900)     # 30/min
    _snapshot("p4", 30, views=1000)    # 3.3/min
    result = momentum.measure("p4", "views", posted_at=_iso(120), now=NOW)
    assert result.trend == "slowing"
    assert result.may_claim_takeoff() is False


def test_near_identical_timestamps_are_collapsed():
    """X updates counters in bursts; differencing them measures rounding."""
    _snapshot("p5", 30.0, views=100)
    _snapshot("p5", 29.5, views=101)
    result = momentum.measure("p5", "views", posted_at=_iso(120), now=NOW)
    assert result.snapshots == 1


def test_early_response_rate_flags_its_own_limitation():
    _snapshot("p6", 20, views=1000, replies=5, reposts=5)
    item = momentum.early_qualified_response("p6")
    assert item.value == pytest.approx(0.01)
    assert "denominator" in (item.note or ""), "the impression caveat must travel"


def test_early_response_is_unknown_without_views():
    _snapshot("p7", 20, replies=5)
    assert momentum.early_qualified_response("p7").known is False


# --------------------------------------------------------------------------- #
# Relationships (§2.6 / §4.2)
# --------------------------------------------------------------------------- #

def test_reply_target_recovered_from_the_leading_mention():
    """The real archive has parent_author NULL on every reply; X puts the
    replied-to handles at the start of the text instead."""
    handle, basis = relationships.reply_target(
        {"text": "@sama @grok Why doesn't Sam like xAI?"})
    assert handle == "sama"
    assert basis == "mention"


def test_recorded_parent_author_wins_over_the_mention():
    handle, basis = relationships.reply_target(
        {"parent_author": "@karpathy", "text": "@sama something"})
    assert (handle, basis) == ("karpathy", "recorded")


def test_a_mid_text_mention_is_not_a_reply_target():
    """Someone talked ABOUT is not someone replied TO."""
    handle, basis = relationships.reply_target({"text": "I think @sama is right"})
    assert handle is None and basis == "unknown"


def test_rebuild_groups_replies_by_target():
    _post("r1", "@sama a distinct first point about model access limits", "reply")
    _post("r2", "@sama an entirely different second observation on pricing", "reply")
    _post("r3", "@karpathy something else again regarding tokenizer design", "reply")
    assert relationships.rebuild() == 2
    sama = storage.get_author_relationship("sama")
    assert sama["interactions"] == 2
    assert sama["replies"] == 2


def test_mention_recovered_relationships_never_claim_strong_confidence():
    for index in range(40):
        _post(f"m{index}", f"@sama point number {index} about a topic of substance", "reply")
    relationships.rebuild()
    assert storage.get_author_relationship("sama")["confidence"] == ev.DIRECTIONAL


def test_an_unmeasured_term_lowers_confidence_rather_than_scoring_average():
    _post("r1", "@sama a distinct point about model access limits today", "reply")
    relationships.rebuild()
    scored = relationships.opportunity("sama")
    assert "outcome" in scored["missing_terms"]
    assert scored["confidence"] in (ev.LIMITED, ev.UNKNOWN_CONFIDENCE)


def test_repeat_pressure_penalises_mechanical_targeting():
    """`now=NOW` is not decoration: the fixtures are dated relative to the
    pinned NOW, while `repeat_pressure` defaults to the real wall clock. Without
    it the seven-day window walks forward every day until the posts fall out of
    it, and the test starts failing on a date with no code change behind it —
    which is exactly what happened at 2026-07-26 02:00 UTC."""
    for index in range(6):
        _post(f"rep{index}", f"@sama yet another reply number {index} here", "reply")
    relationships.rebuild()
    pressure = relationships.repeat_pressure("sama", now=NOW)
    assert pressure["recent_replies"] >= 3
    assert pressure["penalty"] > 0


def test_an_unknown_account_scores_nothing_rather_than_zero():
    scored = relationships.opportunity("nobody")
    assert scored["score"] is None
    assert scored["confidence"] == ev.UNKNOWN_CONFIDENCE


# --------------------------------------------------------------------------- #
# Live Account Memory (§3.2)
# --------------------------------------------------------------------------- #

def test_an_externally_authored_post_is_recorded():
    """Previously impossible: only Starling-generated drafts could be linked."""
    storage.record_observed_post(
        {"post_id": "ext1", "text": "Posted from my phone, not through the app.",
         "post_type": "post", "created_at": _iso(30)}, observed_at=_iso(5))
    external = storage.externally_authored_posts()
    assert [row["post_id"] for row in external] == ["ext1"]


def test_re_observing_a_post_does_not_reset_when_we_first_saw_it():
    storage.record_observed_post({"post_id": "ext2", "text": "First sighting of this post."},
                                 observed_at=_iso(120))
    storage.record_observed_post({"post_id": "ext2", "text": "First sighting of this post."},
                                 observed_at=_iso(1))
    with storage.connect() as conn:
        first = conn.execute("SELECT first_observed_at FROM posts WHERE post_id='ext2'").fetchone()[0]
    assert first == _iso(120)


def test_latest_observation_is_none_before_anything_is_seen():
    assert storage.latest_observation_at() is None


# --------------------------------------------------------------------------- #
# Account Intelligence Profile (§2.7)
# --------------------------------------------------------------------------- #

def test_profile_has_all_eight_sections_and_a_gaps_list():
    built = profile.build(save=True)
    for section in ("identity", "voice", "performance_map", "audience_map",
                    "relationship_map", "cadence_map", "risk_rules"):
        assert section in built["profile"], f"missing §2.7 section {section}"
    assert built["gaps"], "a profile that cannot say what it lacks is not honest"


def test_an_empty_account_is_preliminary_and_says_why():
    built = profile.build(save=True)
    assert built["preliminary"] is True
    assert any("authored posts" in gap["missing"] for gap in built["gaps"])


def test_the_visibility_boundary_is_always_a_gap():
    """§5.3b: Starling can never see viewer histories or X's ranking."""
    built = profile.build(save=True)
    permanent = [gap for gap in built["gaps"] if gap.get("permanent")]
    assert permanent and "viewer-level" in permanent[0]["missing"]


def test_profile_is_versioned_and_reused_until_the_corpus_moves():
    first = profile.build(save=True)
    again = profile.ensure_profile()
    assert again["version"] == first["version"], "must not rebuild for free"
    _post("new1", "A brand new post that changes the corpus entirely.")
    storage.mark_corpus_dirty()
    assert profile.ensure_profile()["version"] != first["version"]


def test_freshness_line_states_the_version_and_the_observation():
    storage.record_observed_post({"post_id": "ext3", "text": "Seen just now by the extension."},
                                 observed_at=_iso(2))
    line = profile.freshness_line()
    assert "Account profile v" in line
    assert "Observed" in line
