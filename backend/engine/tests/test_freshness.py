"""Age + velocity awareness (§3.1): fresh boosts, stale-and-slowing waits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.opportunities import assess_freshness, assess_opportunity
from app.schemas import PostContext
from app.storage import set_account_profile

NOW = datetime(2026, 7, 16, 20, 0, 0, tzinfo=timezone.utc)


def _context(age_minutes: float, views: float, replies: float, text: str | None = None) -> PostContext:
    created = (NOW - timedelta(minutes=age_minutes)).isoformat()
    return PostContext(
        author="TestAI", author_handle="@testai",
        text=text or "New benchmarks show 14B local AI agents closing the gap with cloud developer tools",
        visibility="public", post_created_at=created,
        visible_metrics={"views": views, "replies": replies, "likes": views / 100},
    )


def _profile():
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI agents", "developer tools"]})


def test_freshness_brackets_and_stats_line():
    fresh = assess_freshness(_context(10, 40_000, 60), NOW)
    assert fresh["bracket"] == "prime"
    assert "posted 10m ago" in fresh["stats_line"]
    assert fresh["views_per_hour"] == 240_000.0

    borderline = assess_freshness(_context(150, 3_000, 5), NOW)
    assert borderline["bracket"] == "needs_velocity"

    stale = assess_freshness(_context(8 * 60, 4_000, 6), NOW)
    assert stale["bracket"] == "stale"
    assert not stale["still_hot"]

    hot_but_old = assess_freshness(_context(6 * 60, 200_000, 400), NOW)
    assert hot_but_old["bracket"] == "stale"
    assert hot_but_old["still_hot"]
    assert "still accelerating" in hot_but_old["stats_line"]


def test_stale_and_slowing_post_is_an_age_based_wait():
    _profile()
    result = assess_opportunity(_context(8 * 60, 4_000, 6), NOW)
    assert result["action"] == "wait"
    assert "window is closed" in result["headline"].lower() or "window" in result["rationale"].lower()
    assert result["freshness"]["bracket"] == "stale"


def test_old_but_accelerating_post_can_still_act():
    _profile()
    result = assess_opportunity(_context(6 * 60, 400_000, 900), NOW)
    assert result["action"] in ("reply", "quote")
    assert any("velocity beats age" in reason for reason in result["reasons"])


def test_fresh_post_gets_the_boost_over_identical_older_post():
    _profile()
    fresh = assess_opportunity(_context(10, 20_000, 25), NOW)
    old = assess_opportunity(_context(200, 20_000, 25), NOW)
    assert fresh["action"] in ("reply", "quote")
    assert fresh["score"] > old.get("score", 0.0)


def test_missing_timestamp_changes_nothing():
    _profile()
    context = _context(10, 20_000, 25)
    context.post_created_at = None
    result = assess_opportunity(context, NOW)
    assert result.get("freshness") is None
    assert result["action"] in ("reply", "quote", "wait")
