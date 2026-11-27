"""Viral radar: rising-trend scoring, pillar fit, caching, honest fallback."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import radar
from app.radar import compute_radar, creation_grounding
from app.storage import save_trend_snapshots, set_account_profile, trend_frequencies


class OfflineModel:
    """No model available → radar must still return trend-based candidates."""
    router = None

    def status(self):
        return {"available": False}


def _seed_trends():
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI agents", "Claude", "Opus", "local models"]})
    now = datetime.now(timezone.utc)
    # "Opus 5" appears repeatedly and recently → strongly rising + pillar fit.
    for minutes in (5, 12, 20, 40):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "Opus 5", "category": "Technology · Trending", "volume_text": "82K posts", "rank": 1},
            {"name": "Some Sports Thing", "category": "Sports", "volume_text": "10K posts", "rank": 2},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())
    # A one-off unrelated trend → noise, should rank low.
    save_trend_snapshots("https://x.com/explore", [{"name": "Random Meme", "volume_text": "5K posts", "rank": 9}],
                         captured_at=(now - timedelta(minutes=8)).isoformat())


def test_trend_frequencies_scores_rising_over_oneoff():
    _seed_trends()
    freqs = {row["name"]: row for row in trend_frequencies(hours=8.0)}
    assert freqs["Opus 5"]["count"] == 4
    assert freqs["Opus 5"]["rising_score"] > freqs["Random Meme"]["rising_score"]


def test_radar_prefers_pillar_relevant_rising_trend(monkeypatch):
    _seed_trends()
    # Keep it fully offline: no web research, no model.
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    result = compute_radar(OfflineModel(), force=True)
    assert result["emerging"], "radar must always surface at least the rising trends"
    topics = [e["topic"] for e in result["emerging"]]
    assert "Opus 5" in topics
    # The account-relevant AI trend outranks the sports one.
    assert topics.index("Opus 5") < (topics.index("Some Sports Thing") if "Some Sports Thing" in topics else 99)
    assert result["limitation"]


def test_radar_caches_within_ttl(monkeypatch):
    _seed_trends()
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    first = compute_radar(OfflineModel(), force=True)
    second = compute_radar(OfflineModel())
    assert second.get("cached") is True
    assert second["generated_at"] == first["generated_at"]


def test_creation_grounding_returns_top_angle(monkeypatch):
    _seed_trends()
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    grounding = creation_grounding(OfflineModel())
    assert grounding is not None
    assert grounding["topic"]
    assert "rising" in grounding["based_on"].lower()


def _seed_fading_trend():
    """'Old News' appeared often EARLY in the window then vanished → fading."""
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI agents", "old", "news", "local models"]})
    now = datetime.now(timezone.utc)
    for minutes in (300, 330, 360, 390):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "Old News", "category": "Technology · Trending", "volume_text": "60K posts", "rank": 1},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())
    for minutes in (5, 15, 30):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "AI agents update", "category": "Technology · Trending", "volume_text": "40K posts", "rank": 2},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())


def test_trend_lifecycle_separates_rising_from_fading():
    _seed_fading_trend()
    freqs = {row["name"]: row for row in trend_frequencies(hours=8.0)}
    assert freqs["Old News"]["lifecycle"] == "fading"
    assert freqs["AI agents update"]["lifecycle"] == "rising"
    assert freqs["AI agents update"]["velocity"] > freqs["Old News"]["velocity"]


def test_fading_trend_is_discounted_in_account_ranking():
    _seed_fading_trend()
    trends = {t["name"]: t for t in radar._rising_for_account()}
    # Same pillar fit territory, but the fading trend must not outrank the rising one.
    assert trends["AI agents update"]["account_score"] > trends["Old News"]["account_score"]


def test_topic_staleness_flags_fading_topic(monkeypatch):
    _seed_fading_trend()
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    result = compute_radar(OfflineModel(), force=True)
    verdict = radar.topic_staleness("Old News", result)
    assert verdict["stale"] is True
    assert "passed" in verdict["reason"]
    fresh = radar.topic_staleness("AI agents update", result)
    assert fresh["stale"] is False
    unknown = radar.topic_staleness("something never captured", result)
    assert unknown["stale"] is False
    assert unknown["status"] in ("unknown", "fresh_web")


def test_creation_grounding_skips_stale_topic(monkeypatch):
    _seed_fading_trend()
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    grounding = creation_grounding(OfflineModel())
    assert grounding is not None
    assert grounding["topic"] != "Old News"


def test_a_single_sighting_is_not_a_rising_trend():
    # velocity compares the window's two halves. One capture put everything in
    # the late half and scored velocity 1.0 — the strongest signal the timing
    # engine has, from a single observation. It overrides the calendar and
    # suppresses scheduling entirely, so it has to be earned.
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"]})
    now = datetime.now(timezone.utc)
    save_trend_snapshots("https://x.com/explore", [
        {"name": "Seen Once", "category": "Technology", "volume_text": "10K posts", "rank": 1},
    ], captured_at=(now - timedelta(minutes=10)).isoformat())
    seen_once = {row["name"]: row for row in trend_frequencies(hours=8.0)}["Seen Once"]
    assert seen_once["lifecycle"] == "unknown"
    assert seen_once["velocity"] == 0.0


def test_two_sightings_on_one_side_are_still_not_enough():
    # Two captures minutes apart is one sync, not a trajectory.
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"]})
    now = datetime.now(timezone.utc)
    for minutes in (8, 12):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "Twice Only", "category": "Technology", "volume_text": "10K posts", "rank": 1},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())
    assert {r["name"]: r for r in trend_frequencies(hours=8.0)}["Twice Only"]["lifecycle"] == "unknown"


def test_evidence_on_both_sides_still_reads_as_a_trajectory():
    # The guard must not blind the engine to a trend it can actually see.
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"]})
    now = datetime.now(timezone.utc)
    for minutes in (300, 20, 10):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "Real Wave", "category": "Technology", "volume_text": "10K posts", "rank": 1},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())
    assert {r["name"]: r for r in trend_frequencies(hours=8.0)}["Real Wave"]["lifecycle"] == "rising"


def test_no_trends_no_signal_is_honest(monkeypatch):
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"]})
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    result = compute_radar(OfflineModel(), force=True)
    # No trends captured and no web → no fabricated topics.
    assert result["emerging"] == []
    assert creation_grounding(OfflineModel()) is None
