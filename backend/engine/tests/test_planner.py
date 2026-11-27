"""Next-best-move planner: cached radar + timing + cadence fuse into a ranked
plan that stays honest and never automates anything."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.planner import next_moves
from app.storage import save_trend_snapshots, set_account_profile, set_job_state, upsert_post


def _seed_radar_cache() -> None:
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI agents", "local models"], "timezone": "UTC"})
    now = datetime.now(timezone.utc)
    for minutes in (5, 15, 30):
        save_trend_snapshots("https://x.com/explore", [
            {"name": "Agent Frameworks", "category": "Technology", "volume_text": "30K posts", "rank": 1},
        ], captured_at=(now - timedelta(minutes=minutes)).isoformat())
    set_job_state("viral_radar", {
        "trends": [{"name": "Agent Frameworks", "lifecycle": "rising", "rising_score": 4.0, "count": 3}],
        "web": {"answer": "", "sources": []},
        "emerging": [
            {"topic": "Agent Frameworks", "angle": "Compare against last year's stacks", "why": "Rising on X"},
            {"topic": "Local model routers", "angle": "What breaks in production", "why": "Steady interest"},
        ],
        "summary": "",
        "generated_at": now.isoformat(),
    })


def test_plan_leads_with_post_on_fresh_topic():
    _seed_radar_cache()
    plan = next_moves(llm=None)
    assert plan["moves"]
    top = plan["moves"][0]
    assert top["move"] == "post"
    assert top["topic"] == "Agent Frameworks"
    assert top["when"] == "now"
    assert "guarantee" in plan["note"]


def test_plan_offers_reply_as_second_move():
    _seed_radar_cache()
    plan = next_moves(llm=None)
    moves = [move["move"] for move in plan["moves"]]
    assert "reply" in moves


def test_heavy_cadence_leads_with_restraint():
    _seed_radar_cache()
    # Fixed midday clock: hours-ago fixtures must not cross the UTC date line.
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    for index in range(3):
        upsert_post({
            "post_id": f"today-{index}",
            "text": f"Original post {index} from today about AI agent tooling in production.",
            "post_type": "post",
            "created_at": (now - timedelta(hours=index + 1)).isoformat(),
        })
    plan = next_moves(llm=None, now=now)
    assert plan["moves"][0]["move"] == "wait"
    assert plan["posted_today"] >= 3
    # Nothing suggests automation — the "instead" is advice for the user.
    assert "instead" in plan["moves"][0]


def test_empty_state_is_honest():
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI"], "timezone": "UTC"})
    plan = next_moves(llm=None)
    assert plan["moves"][0]["move"] == "wait"
    assert "No fresh radar signal" in plan["moves"][0]["why"]
