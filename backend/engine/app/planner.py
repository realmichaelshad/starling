"""Next-best-move planner: one ranked, situational answer to "what should I
do on X right now?".

Fuses signals that already exist separately — the viral radar (what's rising
and non-stale), the angle bandit (which format×angle is working for THIS
account), the timing verdict (post now / wait), today's cadence, and the
active goal — into a short ordered plan. Every move carries its why and an
honest note; nothing here posts anything, ever.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .radar import compute_radar, topic_staleness
from .storage import get_job_state, latest_bridge_context, list_goals, performance_records
from .timing import _posts_today, timing_verdict

_ESTIMATE_NOTE = "Estimates from your account's own signals — attention is heavy-tailed and cannot be guaranteed."


def _cached_radar(llm) -> dict[str, Any]:
    """Radar from the live path when a model client is supplied, else the most
    recent cached computation — the planner must work offline."""
    if llm is not None:
        try:
            return compute_radar(llm)
        except Exception:
            pass
    return get_job_state("viral_radar")


def _best_angle(action: str) -> dict[str, Any] | None:
    try:
        from .bandit import bandit_summary
        summary = bandit_summary(action, limit=1)
        return summary[0] if summary else None
    except Exception:
        return None


def _active_goal() -> dict | None:
    try:
        return next((goal for goal in list_goals() if goal.get("status") == "active"), None)
    except Exception:
        return None


def _release_reply_move(fresh_topics: list[dict]) -> dict[str, Any] | None:
    """A reply move pointed at a launch conversation that is actually live.

    Only fires when the WEB confirms the thing shipped — a rumor is not a
    conversation to enter, and an X trend blurb is not evidence (it said Opus 5
    had missed its launch on the day it shipped). Returns None otherwise so the
    generic hint still applies.
    """
    from .release_radar import conversation_url, is_release_topic, release_status

    for candidate in fresh_topics[:3]:
        topic = str(candidate.get("topic") or "").strip()
        if not topic or not is_release_topic(topic):
            continue
        try:
            status = release_status(topic)
        except Exception:
            continue
        if status.get("status") != "shipped":
            continue
        handles = status.get("handles") or []
        who = handles[0] if handles else "the vendor's own account"
        return {
            "move": "reply",
            "target_hint": f"{who}'s launch post — {status.get('headline') or topic}",
            "why": ("This shipped, and the launch thread is where your replies land best. "
                    "Get in early with the specific thing you noticed using it — the "
                    "author engaging back is the heaviest ranking signal there is."),
            "open": conversation_url(topic, handles),
            "release": {"subject": topic, "status": "shipped",
                        "headline": status.get("headline"),
                        "handles": handles},
        }
    return None


def next_moves(llm=None, limit: int = 3, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    radar = _cached_radar(llm) or {}
    emerging = [entry for entry in (radar.get("emerging") or []) if isinstance(entry, dict) and entry.get("topic")]
    fresh_topics = []
    for candidate in emerging:
        verdict = topic_staleness(str(candidate["topic"]), radar)
        if not verdict.get("stale"):
            fresh_topics.append(candidate)
    rows = performance_records()
    posted_today = _posts_today(rows, now)
    goal = _active_goal()
    moves: list[dict[str, Any]] = []

    # Cadence gate first: when today's original-post budget (measured from the
    # user's own cadence curve, fallback 3) is spent, lead with restraint.
    try:
        from .performance import daily_post_budget
        budget, budget_evidence = daily_post_budget(rows)
    except Exception:
        budget, budget_evidence = 3, None
    cadence_spent = posted_today >= budget
    if cadence_spent:
        if posted_today >= max(budget * 2, 6):
            why = (f"{posted_today} originals today is actively hurting your earlier posts — hard stop until tomorrow; "
                   "every extra post splits the same audience.")
        else:
            why = (f"{posted_today} standalone post{'s' if posted_today != 1 else ''} today has used your daily budget "
                   f"of {budget} — another original mostly competes with your own earlier posts.")
            if budget_evidence:
                why += f" Measured on this account — {budget_evidence}."
        moves.append({
            "move": "wait",
            "why": why,
            "instead": "Answer the best replies on today's posts; author-engaged replies are the strongest compounding signal.",
        })

    # The post move: strongest fresh radar topic + the account's best-learning
    # angle + a concrete timing call.
    if fresh_topics and not cadence_spent:
        top = fresh_topics[0]
        timing = timing_verdict("post", str(top["topic"]), now=now)
        angle = _best_angle("post")
        move: dict[str, Any] = {
            "move": "post",
            "topic": top.get("topic"),
            "angle": top.get("angle"),
            "why": str(top.get("why") or "Rising for this account's pillars right now."),
            "when": "now" if timing["verdict"] == "post_now" else str(timing.get("wait_until") or "later"),
            "timing": {key: timing.get(key) for key in ("verdict", "wait_until", "confidence") if timing.get(key)},
        }
        if angle:
            move["proven_angle"] = {"angle": angle.get("angle"),
                                    "evidence": f"best-performing angle for your posts so far "
                                                f"({angle.get('observations', 0)} observations)"}
        moves.append(move)

    # The reply move: a live selected conversation beats a cold start; without
    # one, point at the next rising trend as reply territory.
    bridge = (latest_bridge_context("x_context") or {}).get("data") or {}
    context = bridge.get("context", bridge) if isinstance(bridge, dict) else {}
    if isinstance(context, dict) and context.get("text") and not context.get("is_own_post"):
        moves.append({
            "move": "reply",
            "target_hint": f"the {str(context.get('author') or 'selected')} post you were viewing",
            "why": "A sharp reply into a live conversation borrows its reach — replies the author engages with "
                   "are the heaviest positive ranking signal.",
        })
    elif release_move := _release_reply_move(fresh_topics):
        # A shipping event outranks a generic reply hint: this account is built
        # on replying to releases and bug updates, and on Opus 5 launch day the
        # planner offered "find a fresh post with traction" while the launch
        # conversation was wide open. Name the conversation instead.
        moves.append(release_move)
    elif len(fresh_topics) > 1:
        moves.append({
            "move": "reply",
            "target_hint": f"a strong conversation about {fresh_topics[1].get('topic')}",
            "why": "Replies acquire attention; find a fresh (<90 min) post with real traction and add the missing point.",
        })

    if not moves:
        moves.append({
            "move": "wait",
            "why": "No fresh radar signal and no live conversation selected — saving attention for a real "
                   "opening beats manufacturing one.",
            "instead": "Open X and let the radar recompute, or develop one useful idea for your strongest theme.",
        })

    return {
        "moves": moves[:limit],
        "posted_today": posted_today,
        "goal": {"objective": goal.get("objective"), "name": goal.get("name")} if goal else None,
        "generated_at": now.isoformat(),
        "note": _ESTIMATE_NOTE,
    }
