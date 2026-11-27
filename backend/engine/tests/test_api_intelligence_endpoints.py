"""The intelligence behind the old timing, next-move and momentum endpoints:
timing verdict, next-move plan, and post-launch momentum, all offline-safe.

The HTTP routes are gone; the library functions that answered them remain."""
from __future__ import annotations

from app.learning_loop import early_checkpoint
from app.planner import next_moves
from app.timing import timing_verdict


def test_timing_plan_and_momentum_answer_offline():
    verdict = timing_verdict()
    assert verdict["verdict"] in ("post_now", "wait_until", "topic_stale")
    assert verdict["reasons"]

    plan = next_moves(llm=None)
    assert plan["moves"]
    assert all(move["move"] in ("post", "reply", "wait") for move in plan["moves"])
    assert "guarantee" in plan["note"]

    momentum = early_checkpoint()
    assert "checks" in momentum and "baseline" in momentum
