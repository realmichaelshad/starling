"""Post-launch momentum: early-velocity reads against the account's own
baseline become advice — never automatic action."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.learning_loop import account_velocity_baseline, early_checkpoint, momentum_verdict
from app.opportunities import assess_opportunity
from app.schemas import PostContext
from app.storage import create_generation, record_outcome, upsert_post


def _seed_baseline(count: int = 10, views: int = 1200) -> None:
    """Measured posts averaging ~50 views/hour over their first day."""
    now = datetime.now(timezone.utc)
    for index in range(count):
        created = now - timedelta(days=5 + index)
        upsert_post({
            "post_id": f"base-{index}",
            "text": f"Baseline post {index} about agent tooling with enough text to count as a real post.",
            "post_type": "post",
            "created_at": created.isoformat(),
            "metrics": {"views": views, "replies": 3, "likes": 10},
            "metrics_updated_at": (created + timedelta(hours=24)).isoformat(),
        })


def test_baseline_needs_enough_history():
    _seed_baseline(count=3)
    verdict = momentum_verdict(1000.0, 5.0, account_velocity_baseline("post"))
    assert verdict is None  # under 8 measured posts → no confident read


def test_taking_off_and_quiet_verdicts():
    _seed_baseline()
    baseline = account_velocity_baseline("post")
    assert baseline["sample"] >= 8
    hot = momentum_verdict(baseline["views_per_hour_median"] * 5, 10.0, baseline)
    assert hot["status"] == "taking_off"
    assert "repl" in hot["advice"].lower()  # points at answering replies, the compounding move
    cold = momentum_verdict(baseline["views_per_hour_median"] * 0.1, 0.0, baseline)
    assert cold["status"] == "quiet"
    assert "not failure" in cold["advice"]
    assert "promise" not in hot["advice"]


def test_own_post_opportunity_upgrades_with_momentum():
    _seed_baseline()
    context = PostContext(
        author="You", text="My fresh post about agent tooling that seems to be moving fast right now.",
        visibility="public", is_own_post=True,
        post_created_at=(datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat(),
        visible_metrics={"views": 20_000, "replies": 25},
    )
    result = assess_opportunity(context)
    assert result["action"] == "follow_up"
    assert result["momentum"]["status"] == "taking_off"
    assert "taking off" in result["headline"]
    assert result["should_draft"] is False  # advice only, nothing automated


def test_early_checkpoint_reads_fresh_generations():
    _seed_baseline()
    now = datetime.now(timezone.utc)
    generation_id = create_generation({"action": "post"}, {})
    upsert_post({
        "post_id": "fresh-1",
        "text": "The fresh post that just went out about agent tooling and early velocity.",
        "post_type": "post",
        "created_at": (now - timedelta(minutes=30)).isoformat(),
        "metrics": {"views": 8000, "replies": 12},
        "metrics_updated_at": now.isoformat(),
    })
    record_outcome({"generation_id": generation_id, "status": "posted", "published_post_id": "fresh-1",
                    "posted_at": (now - timedelta(minutes=30)).isoformat()})
    result = early_checkpoint(now)
    assert result["checks"], "the fresh generation must be inspected"
    check = result["checks"][0]
    assert check["post_id"] == "fresh-1"
    assert check["status"] in ("taking_off", "normal", "quiet")
