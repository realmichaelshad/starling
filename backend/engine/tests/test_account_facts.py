"""Account facts: the deterministic block that stops the chat model from
inventing dates, counts, recent posts, and per-post metrics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.account_facts import account_facts
from app.storage import save_profile_snapshot, set_account_profile, upsert_post


def _seed() -> None:
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI models", "agents"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    # Minutes-ago, not hours-ago: an hours-based fixture crosses the UTC date
    # boundary when the suite runs shortly after midnight.
    upsert_post({"post_id": "today-1", "text": "Wait did the progress bar disappear for anyone else? Mine only shows all models now.",
                 "post_type": "post", "created_at": (now - timedelta(minutes=10)).isoformat()})
    for index in range(6):
        created = now - timedelta(days=index + 1)
        views = 3818 if index == 1 else 300 + index * 50
        text = ("Kimi K3 is out. If it is really near frontier level the eight month gap story collapses."
                if index == 1 else f"Older post {index} about AI models and agent tooling in production.")
        upsert_post({"post_id": f"old-{index}", "text": text, "post_type": "post",
                     "created_at": created.isoformat(),
                     "metrics": {"views": views, "likes": 5, "replies": 1},
                     "metrics_updated_at": (created + timedelta(days=1)).isoformat()})


def test_facts_carry_date_counts_and_recent_posts():
    _seed()
    facts = account_facts("")
    # Local time, not UTC. Reporting UTC made the model tell the user they
    # posted "Monday" about a Sunday-evening post, and forced them to convert
    # every time and window in their head.
    local_now = datetime.now().astimezone()
    assert local_now.date().isoformat() in facts["now"]
    assert facts["now"].startswith(local_now.strftime("%A"))
    assert "local time" in facts["now"]
    assert "LOCAL" in facts["timezone_note"]
    assert facts["posts_today"] == 1
    assert facts["recent_posts"][0]["text"].startswith("Wait did the progress bar")
    assert facts["total_posts_recorded"] == 7
    assert facts["median_views"] is not None
    assert facts["best_post_ever"]["views"] == 3818
    assert "Kimi" in facts["best_post_ever"]["text"]
    assert facts["niche"] and "AI models" in facts["niche"]
    assert facts["daily_post_budget"] >= 1


def test_mention_lookup_finds_the_asked_about_post():
    _seed()
    facts = account_facts("how many views did my Kimi K3 post get?")
    mentioned = facts.get("mentioned_posts") or []
    assert mentioned, "a rare token like 'kimi' must find the post"
    assert mentioned[0]["views"] == 3818
    # Generic questions must not attach random posts.
    assert "mentioned_posts" not in account_facts("how are you doing")


def test_followers_come_from_profile_snapshots():
    _seed()
    save_profile_snapshot({"handle": "user", "followers_count": 14, "following_count": 10, "posts_count": 79})
    facts = account_facts("")
    assert facts["followers"] == 14


def test_insights_median_views_is_plain_views():
    _seed()
    from app.performance import performance_insights
    insights = performance_insights()
    post_stats = insights["by_format"]["post"]
    assert post_stats["median_views"] < post_stats.get("median_reach_score", float("inf")) or \
        post_stats["median_views"] == post_stats.get("median_reach_score")
    # Plain views median must be a real observed views value, not a composite.
    assert post_stats["median_views"] in (300, 350, 400, 450, 500, 550, 3818)
