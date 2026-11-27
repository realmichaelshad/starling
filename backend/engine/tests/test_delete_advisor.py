"""Delete-post advisor: honest verdicts from comparables, freshness, and the
user's own deletion history — advice only, nothing is ever deleted by the app."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.performance import delete_advice
from app.storage import set_account_profile, upsert_archive_records, upsert_post
from app.strategy import local_strategy_answer


def _seed_history(count: int = 12) -> None:
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    for index in range(count):
        created = now - timedelta(days=5 + index)
        upsert_post({
            "post_id": f"hist-{index}",
            "text": f"History post {index} about model benchmark comparisons and what the benchmark gap hides in production.",
            "post_type": "post",
            "created_at": created.isoformat(),
            "metrics": {"views": 10_000 + index * 500, "likes": 30, "replies": 5},
            "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
        })
    # A second, weak theme so the benchmark theme is measurably STRONG.
    for index in range(6):
        created = now - timedelta(days=6 + index)
        upsert_post({
            "post_id": f"weak-{index}",
            "text": f"Career advice note {index} for junior engineers about mentorship patience and career fundamentals.",
            "post_type": "post",
            "created_at": created.isoformat(),
            "metrics": {"views": 120, "likes": 1, "replies": 0},
            "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
        })


def test_fresh_post_is_too_early_to_delete():
    _seed_history()
    now = datetime.now(timezone.utc)
    upsert_post({"post_id": "fresh", "text": "A fresh post about benchmark comparisons from this morning.",
                 "post_type": "post", "created_at": (now - timedelta(hours=3)).isoformat(),
                 "metrics": {"views": 50, "replies": 0}, "metrics_updated_at": now.isoformat()})
    advice = delete_advice("fresh")
    assert advice["verdict"] == "too_early"
    assert any("old" in reason for reason in advice["reasons"])


def test_underperformer_is_reasonable_but_replies_save_it():
    _seed_history()
    now = datetime.now(timezone.utc)
    upsert_post({"post_id": "flop", "text": "An older underperforming post about benchmark comparisons nobody viewed.",
                 "post_type": "post", "created_at": (now - timedelta(days=3)).isoformat(),
                 "metrics": {"views": 10, "replies": 0, "likes": 0},
                 "metrics_updated_at": (now - timedelta(days=2)).isoformat()})
    advice = delete_advice("flop")
    assert advice["verdict"] == "reasonable"

    upsert_post({"post_id": "talker", "text": "A low-view post about benchmark comparisons that keeps earning replies.",
                 "post_type": "post", "created_at": (now - timedelta(days=3)).isoformat(),
                 "metrics": {"views": 10, "replies": 6, "likes": 1},
                 "metrics_updated_at": (now - timedelta(days=2)).isoformat()})
    saved = delete_advice("talker")
    assert saved["verdict"] == "keep"
    assert any("conversation" in reason or "repl" in reason for reason in saved["reasons"])


def test_deletion_history_pattern_warns():
    _seed_history()
    now = datetime.now(timezone.utc)
    # Archive: many deleted posts that RESEMBLE the strong benchmark theme.
    upsert_archive_records("test-pack", [
        {"text": f"Deleted take {index} about model benchmark comparisons and the benchmark gap in production."}
        for index in range(8)
    ], "deleted_tweet")
    upsert_post({"post_id": "flop2", "text": "Another older underperforming post about benchmark comparisons.",
                 "post_type": "post", "created_at": (now - timedelta(days=4)).isoformat(),
                 "metrics": {"views": 8, "replies": 0}, "metrics_updated_at": (now - timedelta(days=3)).isoformat()})
    advice = delete_advice("flop2")
    assert advice["deletion_history"]["deleted_count"] == 8
    assert advice["deletion_history"]["resembling_strong_theme"] >= 3
    assert advice["verdict"] == "pause"
    assert any("habitually" in reason for reason in advice["reasons"])


def test_unknown_post_returns_none_and_chat_answers_generally():
    assert delete_advice("nope") is None
    answer = local_strategy_answer("should i delete this post?", context=None)
    assert "delet" in answer.lower()
    assert "repl" in answer.lower()  # the honest replies-beat-views heads-up
