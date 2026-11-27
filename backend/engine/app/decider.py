"""Decider: REPLY, QUOTE, POST or WAIT — and why (§6).

Takes the Analyzer's evidence and chooses ONE action. It does not gather
evidence and it does not write. The split matters: previously both happened in
one function, so a weak decision could be rescued by a strong draft, which is
exactly what §6.9 forbids.

    x_aligned_utility(action) =
        hard_eligibility × evidence_confidence × goal_multiplier
      × ( 0.24 candidate-route fit
        + 0.22 viewer-relevance hypothesis
        + 0.18 surface and context fit
        + 0.16 distinct-contribution value
        + 0.12 natural multi-action value
        + 0.08 account-conversion fit )
      - ( negative-action risk + duplication + factual/safety risk
        + saturation and cadence cost + opportunity cost )

Every weight is a Starling prior (§6.2), never an X weight. A hard gate
returns zero regardless of how attractive the other terms look, and WAIT wins
whenever the evidence is too thin to justify acting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import algorithm_model as am
from . import evidence as ev
from . import relationships

REPLY = "reply"
QUOTE = "quote"
POST = "post"
WAIT = "wait"

# §6.2 priors. Named so the decision card can show which term carried it.
UTILITY_WEIGHTS: dict[str, float] = {
    "candidate_route": 0.24,
    "viewer_relevance": 0.22,
    "surface_fit": 0.18,
    "distinct_contribution": 0.16,
    "multi_action_value": 0.12,
    "conversion_fit": 0.08,
}

# §6.3 reply priors — a different shape from the shared utility because a reply
# lives or dies on the conversation, not on the account's standing.
REPLY_WEIGHTS: dict[str, float] = {
    "viewer_bridge": 0.28,
    "missing_useful_point": 0.26,
    "live_thread_state": 0.18,
    "reply_context_clarity": 0.12,
    "natural_action_value": 0.10,
    "relationship_fit": 0.06,
}

# §6.2b candidate-route states.
ROUTE_EXISTING = "existing_audience"
ROUTE_ADJACENT = "adjacent_audience_hypothesis"
ROUTE_REPLY = "reply_surface_opportunity"
ROUTE_STANDALONE = "standalone_discovery"
ROUTE_NONE = "no_credible_route"

# §6.2a growth objectives.
OBJECTIVES = ("distribution", "conversation", "authority", "follower_growth",
              "timely_participation")

# Below this, acting is not justified and WAIT wins on its own merits.
ACT_THRESHOLD = 0.42

_CONFIDENCE_MULTIPLIER = {
    ev.STRONG: 1.0, ev.DIRECTIONAL: 0.85, ev.LIMITED: 0.65,
    ev.UNKNOWN_CONFIDENCE: 0.5,
}

# Scoring-term names in plain English, for the "Why?" line on the card.
_WHY_PHRASES = {
    "viewer_bridge": "It connects to what your audience already cares about.",
    "viewer_relevance": "It connects to what your audience already cares about.",
    "missing_useful_point": "You have a specific point to add that others have not.",
    "distinct_contribution": "You have a specific point to add that others have not.",
    "live_thread_state": "The thread is live and worth entering now.",
    "surface_fit": "The thread is live and worth entering now.",
    "reply_context_clarity": "The post is clear enough to reply to without guessing.",
    "natural_action_value": "A reply here earns real attention, not just a like.",
    "multi_action_value": "A reply here earns real attention, not just a like.",
    "relationship_fit": "Engaging this author fits how you build your audience.",
    "conversion_fit": "Engaging this author fits how you build your audience.",
    "reach": "The post already has real reach to build on.",
}

# The SAME term means different things per surface, and a flat lookup made the
# card explain "Post your own instead" with "the thread is live and worth
# entering now" — a reply's reason under a post's verdict. Overrides only where
# the meaning genuinely differs; everything else falls back to the table above.
_WHY_PHRASES_BY_ACTION: dict[str, dict[str, str]] = {
    POST: {
        "surface_fit": "This belongs in a post of your own, not in someone else's thread.",
        "multi_action_value": "A post like this earns saves and profile visits, not just likes.",
        "natural_action_value": "A post like this earns saves and profile visits, not just likes.",
        "conversion_fit": "This is the kind of post that turns readers into followers.",
        "relationship_fit": "This fits the audience you are building.",
    },
    QUOTE: {
        "surface_fit": "This is worth re-framing for your own audience rather than replying.",
        "multi_action_value": "A quote puts your take in front of your followers, not just theirs.",
        "natural_action_value": "A quote puts your take in front of your followers, not just theirs.",
        "conversion_fit": "Quoting this author fits how you build your audience.",
    },
}


def _why_phrase(action: str, term: str) -> str | None:
    """The human reason for a term, in the voice of the action it explains."""
    return _WHY_PHRASES_BY_ACTION.get(action, {}).get(term) or _WHY_PHRASES.get(term)


# What a term means when it is the thing that decided a near-tie.
_TIPPED_BY = {
    "live_thread_state": "the thread being live",
    "surface_fit": "where this belongs",
    "missing_useful_point": "having a specific point to add",
    "distinct_contribution": "having a specific point to add",
    "reply_context_clarity": "the post being clear enough to answer",
    "relationship_fit": "who the author is to you",
    "conversion_fit": "who the author is to you",
    "viewer_bridge": "what your audience cares about",
    "viewer_relevance": "what your audience cares about",
}
# Below this gap the two routes are genuinely arguable and the trade-off is
# worth stating; above it, naming the loser only adds noise.
_CLOSE_CALL_MARGIN = 0.15


def _close_call_line(runner_up_action: str, margin: float,
                     drivers: list[tuple[str, float]]) -> str | None:
    """"Close call over posting — the thread being live tipped it."

    A strategist says what a move was chosen OVER. That existed only as scores
    ("reply (0.67) over post (0.51)"), which the card cannot show and nobody
    can act on. Returns None when the race was not close.
    """
    if margin >= _CLOSE_CALL_MARGIN or margin < 0:
        return None
    term = next((name for name, value in drivers
                 if value is not None and value >= 0.6 and name in _TIPPED_BY), None)
    tipped = _TIPPED_BY.get(term or "")
    verb = {"reply": "replying", "post": "posting", "quote": "quoting"}.get(
        runner_up_action, f"{runner_up_action}ing")
    return f"Close call over {verb}" + (f" — {tipped} tipped it." if tipped else ".")

# A term that could not be scored is a gap in what Starling knows about YOU,
# not a mark against the post. Naming it tells the user the one thing that
# would actually change the verdict, instead of leaving a silent zero.
_UNSCORED_PHRASES: dict[str, str] = {
    "viewer_bridge": "Run Sync so I can judge how well this fits your audience.",
    "viewer_relevance": "Run Sync so I can judge how well this fits your audience.",
}


@dataclass
class Decision:
    """The §6.8 decision card. Exactly one primary call."""

    action: str = WAIT
    confidence: str = ev.LIMITED
    primary_objective: str | None = None
    candidate_route: str = ROUTE_NONE
    x_alignment: str = "weak"
    reply_surface: str = "n/a"
    image_decision: str = "text only"
    why_now: list[str] = field(default_factory=list)
    outweighed: str = ""
    risks: list[str] = field(default_factory=list)
    unknown_to_starling: str = am.VISIBILITY_BOUNDARY
    next_step: str = "save"
    recheck: str | None = None
    scores: dict[str, float] = field(default_factory=dict)
    terms: dict[str, dict[str, float]] = field(default_factory=dict)
    missing_useful_point: str | None = None
    blocked: bool = False
    # A terminal WAIT may not be silently turned into a draft (§6.9). The user
    # can override, but the override has to be explicit and is recorded.
    terminal: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "confidence": self.confidence,
            "primary_objective": self.primary_objective,
            "candidate_route": self.candidate_route,
            "x_alignment": self.x_alignment, "reply_surface": self.reply_surface,
            "image_decision": self.image_decision, "why_now": self.why_now,
            "what_outweighed_other_options": self.outweighed, "risks": self.risks,
            "unknown_to_starling": self.unknown_to_starling,
            "next_step": self.next_step, "recheck": self.recheck,
            "scores": self.scores, "terms": self.terms,
            "missing_useful_point": self.missing_useful_point,
            "blocked": self.blocked, "terminal": self.terminal,
        }


def _route(analysis, action: str) -> tuple[str, float]:
    """§6.2b: name the route and score it. Follower count is never a route."""
    fit = analysis.fit or {}
    subject = fit.get("subject", {}).get("score")
    rel = fit.get("relationship", {}) or {}

    if action == REPLY:
        if analysis.reply_surface in ("strong", "directional"):
            return ROUTE_REPLY, 0.9 if analysis.reply_surface == "strong" else 0.6
        return ROUTE_NONE, 0.0
    if subject:
        # A demonstrated pillar means people already recognise the account here.
        return ROUTE_EXISTING, min(1.0, 0.4 + subject)
    if rel.get("interactions"):
        return ROUTE_ADJACENT, 0.5
    if action == POST and analysis.readiness.get("immediate_comprehension"):
        return ROUTE_STANDALONE, 0.45
    return ROUTE_NONE, 0.0


def _objective(analysis, action: str) -> str:
    """§6.2a: choose the objective BEFORE the copy, from the evidence."""
    lead = (analysis.angles or [{}])[0]
    primary = lead.get("primary_action")
    if action == REPLY:
        return "conversation"
    if primary == "share":
        return "distribution"
    if lead.get("kind") in ("decision_framework", "clear_distinction",
                            "overlooked_tradeoff", "practical_implication"):
        return "authority"
    if analysis.readiness.get("action_elasticity"):
        return "distribution"
    return "timely_participation"


def _terms_for(analysis, action: str, route_score: float) -> dict[str, float | None]:
    fit = analysis.fit or {}
    # None means NOT YET MEASURED (no pillars synced), which is not the same
    # claim as "this post is irrelevant to your audience". Collapsing it to 0.0
    # silently zeroed viewer_bridge — the single largest reply weight (0.28) —
    # so a fresh profile pushed almost every good reply under ACT_THRESHOLD and
    # the card blamed an unrelated momentum note. _utility renormalises instead.
    subject = fit.get("subject", {}).get("score")
    readiness = analysis.readiness or {}
    angles = analysis.angles or []

    surface = {
        REPLY: {"strong": 1.0, "directional": 0.6, "weak": 0.2, "blocked": 0.0,
                "n/a": 0.0}.get(analysis.reply_surface, 0.0),
        # A quote needs the source to be essential AND a standalone thesis.
        QUOTE: 0.7 if readiness.get("immediate_comprehension") else 0.3,
        POST: 0.8 if readiness.get("native_payoff") else 0.4,
    }[action]

    return {
        "candidate_route": route_score,
        "viewer_relevance": subject,
        "surface_fit": surface,
        "distinct_contribution": min(1.0, 0.45 * len(angles)),
        "multi_action_value": 1.0 if readiness.get("action_elasticity") else (
            0.5 if angles else 0.0),
        "conversion_fit": 0.6 if subject else 0.2,
    }


def _penalties(analysis, action: str, handle: str | None,
               cadence_pressure: float) -> dict[str, float]:
    anti = set(analysis.anti_patterns or [])
    penalties = {
        "negative_action_risk": 0.25 if analysis.penalties else 0.0,
        "duplication": 0.35 if "recent_repeat" in anti else (
            0.20 if "restatement" in anti else 0.0),
        "factual_safety_risk": 0.30 if analysis.blockers else 0.0,
        # The daily budget meters what lands on the TIMELINE (posts and quotes).
        # A reply spends none of it — found live: with the post budget used up,
        # cadence 0.6 crushed a 0.67 reply on a hot fresh thread to 0.07 and the
        # card blamed "only one observation". Replying is exactly what an
        # account SHOULD do on a maxed posting day (registry H96).
        "saturation_cadence": cadence_pressure if action in (POST, QUOTE) else 0.0,
        # Replying to a crowded thread when a standalone post would carry the
        # point better is a real cost, not a neutral choice (§6.4).
        "opportunity_cost": 0.15 if (
            action == REPLY and analysis.readiness.get("native_payoff")
            and analysis.reply_surface == "weak") else 0.0,
    }
    if action == REPLY and handle:
        penalties["duplication"] += relationships.repeat_pressure(handle)["penalty"] * 0.3
    return penalties


def _utility(analysis, action: str, handle: str | None,
             cadence_pressure: float) -> dict[str, Any]:
    route, route_score = _route(analysis, action)
    terms = _terms_for(analysis, action, route_score)
    penalties = _penalties(analysis, action, handle, cadence_pressure)

    eligible = 0.0 if analysis.blockers else 1.0
    if action == REPLY and analysis.reply_surface == "blocked":
        eligible = 0.0
    # AN AD IS NOT A REASON TO POST. When the reply surface went weak because
    # the target was an ad or engagement bait, REPLY dropped out and POST won
    # by default — with a score built entirely from constants, because POST's
    # terms barely touch the target post. The card then said "This belongs in a
    # post of your own, not in someone else's thread" about a course ad, which
    # asserts he has something to write and is only ever a coincidence of the
    # weights. Nothing in this target supports any move.
    if getattr(analysis, "target_quality", "ok") in ("ad", "bait"):
        eligible = 0.0
    confidence_multiplier = _CONFIDENCE_MULTIPLIER.get(analysis.confidence, 0.5)

    if action == REPLY:
        weights, mapped = REPLY_WEIGHTS, {
            "viewer_bridge": terms["viewer_relevance"],
            "missing_useful_point": terms["distinct_contribution"],
            "live_thread_state": terms["surface_fit"],
            "reply_context_clarity": 1.0 if analysis.readiness.get(
                "immediate_comprehension") else 0.3,
            "natural_action_value": terms["multi_action_value"],
            "relationship_fit": terms["conversion_fit"],
        }
    else:
        weights, mapped = UTILITY_WEIGHTS, terms

    # An unmeasured term abstains rather than voting zero: the weighted average
    # is taken over the terms we actually know, renormalised by their weight
    # mass. Every weight table sums to 1.0, so when nothing is unknown this is
    # arithmetically identical to the old sum — only the unknown case changes.
    unknown = [name for name in weights if mapped[name] is None]
    known_mass = sum(weights[name] for name in weights if mapped[name] is not None)
    if known_mass:
        base = sum(weights[name] * mapped[name]
                   for name in weights if mapped[name] is not None) / known_mass
    else:
        base = 0.0
    score = eligible * confidence_multiplier * base - sum(penalties.values())
    return {
        "action": action, "score": round(max(0.0, score), 4), "route": route,
        "terms": {k: (round(v, 3) if v is not None else None)
                  for k, v in mapped.items()},
        "unknown_terms": unknown,
        "penalties": {k: round(v, 3) for k, v in penalties.items() if v},
        "eligible": bool(eligible),
    }


def _wait_utility(analysis, best: dict[str, Any]) -> tuple[float, list[str], str | None]:
    """§6.6: WAIT is calculated, not a shrug. Returns (score, reasons, recheck)."""
    reasons: list[str] = []
    score = 0.0
    if analysis.blockers:
        return 1.0, analysis.blockers, None
    # SITUATION FIRST. The reasons are read top-down and only the first two
    # reach the card, so a concrete fact about THIS thread ("the last reply was
    # ~14h ago") must outrank generic caveats. It used to be appended last and
    # never surfaced: a dead 15h thread was explained as "you have already said
    # something close to this", about a thread he had never touched.
    if analysis.reply_surface == "weak" and analysis.reply_surface_reason:
        score += 0.15
        reasons.append(analysis.reply_surface_reason)
    if not analysis.angles:
        score += 0.35
        # "No angle exists" is NOT "you already said this" — that claim belongs
        # to recent_repeat below, and stating it about a thread he has never
        # touched is simply false. Seen live on a 15h-old thread.
        # Only stated when the surface reason has not already said it: two
        # lines meaning the same thing read as padding, and the card only has
        # room for two.
        if not reasons:
            reasons.append("There is no distinct point to add that the thread "
                           "does not already have.")
    if "recent_repeat" in (analysis.anti_patterns or []):
        score += 0.25
        reasons.append("You recently made a near-identical point.")
    if analysis.confidence in (ev.LIMITED, ev.UNKNOWN_CONFIDENCE):
        score += 0.20
        reasons.append("The evidence behind this is thin.")
    trend = (analysis.momentum or {}).get("trend")
    if trend == "slowing":
        score += 0.20
        reasons.append("The post is slowing between snapshots.")
    # (the weak-surface term now runs FIRST, above — same weight, better order)
    if (analysis.momentum or {}).get("basis") in ("none", "average"):
        score += 0.10
        reasons.append("Only one observation, so movement is unconfirmed.")

    recheck = None
    if trend == "unknown" and (analysis.momentum or {}).get("basis") == "average":
        recheck = "Re-check after a second metric snapshot confirms movement."
    elif not analysis.angles:
        recheck = "Re-check if a new fact or a substantive reply changes the thread."
    return min(1.0, score), reasons, recheck


# THE MODEL DECIDES; THESE NEVER REACH IT (D1, 2026-09-21).
#
# The utility score above was the whole decision, and its "is there a point to
# add?" term came from English keyword lists: on 30 fresh, public, repliable
# posts it refused 21 without asking the model ("I could not find a point worth
# adding to this one"). The judgement now belongs to the model, in the same
# call that drafts, and the score stays as a TIMING PRIOR shown on the card.
#
# What stays deterministic is everything that must never be drafted however
# good the words would be: an unsafe or private context, a protected account,
# an ad or engagement bait, a thread already answered (`main` checks that one
# before analysis), a dead thread, and a pile of replies on a post hours old.
# A reply-restricted post only rules out the REPLY; see `main._press_action`.
HARD_STOP_CROWD = 500
HARD_STOP_CROWD_AGE_MINUTES = 120


def hard_stop(analysis, context) -> str | None:
    """Why no draft may be written here, in the reader's words, or None."""
    if getattr(analysis, "blockers", None):
        return str(analysis.blockers[0])
    if getattr(analysis, "target_quality", "ok") in ("ad", "bait"):
        return str(getattr(analysis, "reply_surface_reason", "") or
                   "This post is an ad or bait, so nothing here earns you anything.")
    if str(getattr(context, "visibility", "") or "") == "protected":
        return "The account is protected, so its posts stay out of drafts."
    try:
        from .conversation import thread_temperature
        temperature, line = thread_temperature(context)
    except Exception:                                       # noqa: BLE001
        temperature, line = "unknown", ""
    if temperature == "dead":
        return line or "This thread has gone quiet."
    crowd = getattr(context, "visible_reply_count", None)
    age = getattr(analysis, "age_minutes", None)
    if (isinstance(crowd, (int, float)) and crowd >= HARD_STOP_CROWD
            and (age or 0.0) > HARD_STOP_CROWD_AGE_MINUTES):
        return (f"~{int(crowd):,} replies are already here and the post is hours old, "
                "so a new one lands far down the pile.")
    return None


def decide(analysis, *, handle: str | None = None,
           cadence_pressure: float = 0.0,
           allowed: tuple[str, ...] = (REPLY, QUOTE, POST)) -> Decision:
    """Choose one action from the Analyzer's evidence."""
    decision = Decision()
    decision.reply_surface = analysis.reply_surface
    decision.x_alignment = (analysis.alignment or {}).get("label", "weak")
    if analysis.angles:
        decision.missing_useful_point = analysis.angles[0]["why"]

    # Hard gates (§6.5) short-circuit everything.
    if analysis.blockers:
        decision.action, decision.blocked, decision.terminal = WAIT, True, True
        decision.confidence = ev.STRONG
        decision.risks = list(analysis.blockers)
        # Say WHY, in the blocker's own words ("People were hurt here…").
        # "A safety gate blocked this context" is the machine describing itself,
        # which is the exact complaint that got the WAIT card rewritten.
        decision.why_now = [str(analysis.blockers[0])]
        decision.outweighed = "No action is available while a hard gate is set."
        decision.next_step = "skip"
        return decision

    scored = [_utility(analysis, action, handle, cadence_pressure)
              for action in allowed]
    scored.sort(key=lambda item: item["score"], reverse=True)
    best = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None

    wait_score, wait_reasons, recheck = _wait_utility(analysis, best)
    decision.scores = {item["action"]: item["score"] for item in scored}
    decision.scores[WAIT] = round(wait_score, 4)
    decision.terms = {item["action"]: item["terms"] for item in scored}

    if best["score"] < ACT_THRESHOLD or wait_score >= best["score"]:
        decision.action = WAIT
        decision.candidate_route = ROUTE_NONE
        decision.confidence = analysis.confidence
        # The card must read cleanly: a plain human reason, no scores, no
        # internal term names (those live in `outweighed`, for logs only — the
        # user complained the card showed "scored 0.17, just under the 0.42 bar"
        # and "the heaviest reply term"). At most two short lines.
        # Momentum-basis noise ("only one observation") is an internal caveat,
        # not a reason a person acts on — it is NEVER shown. It was the single
        # most-hated line on the card, and it kept resurfacing as the headline
        # whenever nothing filtered it.
        human = [r for r in (wait_reasons or [])
                 if r and "observation" not in r.lower()]
        # When the posting budget is the real constraint and nothing more
        # specific explains the wait, SAY the budget (never as noise on top of
        # a better reason like "this thread is dead").
        if not human and cadence_pressure >= 0.3 and any(
                item["penalties"].get("saturation_cadence", 0) >= 0.3 for item in scored):
            human = ["You've hit today's posting budget — hold this for the next window."]
        if wait_score < best["score"] and not human:
            human = ["Nothing here is worth acting on right now."]
        # One short, actionable hint if a term was genuinely unmeasurable.
        for name in best.get("unknown_terms") or []:
            phrase = _UNSCORED_PHRASES.get(name)
            if phrase and phrase not in human:
                human.append(phrase)
                break
        decision.why_now = human[:2] or ["No option cleared the bar."]
        decision.outweighed = (
            f"The best option ({best['action']}, {best['score']:.2f}) did not beat "
            f"waiting ({wait_score:.2f}).")
        decision.risks = list(analysis.penalties)
        decision.recheck = recheck
        decision.next_step = "save"
        # Terminal only when the reason is structural rather than timing.
        decision.terminal = bool(not analysis.angles)
        return decision

    decision.action = best["action"]
    decision.candidate_route = best["route"]
    decision.primary_objective = _objective(analysis, best["action"])
    decision.confidence = analysis.confidence
    decision.risks = list(analysis.penalties) + [
        f"{name.replace('_', ' ')}" for name in best["penalties"]]
    decision.next_step = "draft"

    # Plain reasons, not scoring internals. This printed "missing useful point:
    # 1.00" on the card - a term name and a raw number that mean nothing to the
    # user. Only the terms actually carrying the decision are named, in English.
    drivers = sorted(((name, value) for name, value in best["terms"].items()
                      if value is not None),
                     key=lambda item: item[1], reverse=True)
    seen_phrases: list[str] = []
    for name, value in drivers:
        if value < 0.6:
            continue
        phrase = _why_phrase(best["action"], name)
        if phrase and phrase not in seen_phrases:
            seen_phrases.append(phrase)
        if len(seen_phrases) >= 3:
            break
    decision.why_now = seen_phrases
    if not decision.why_now:
        decision.why_now = [f"{best['action'].title()} is the strongest available move here."]

    # WHY NOT A REPLY? He pressed ✦ on someone else's post, so when the answer
    # is anything but "reply", the reason the reply was ruled out is the most
    # useful line on the card — and it was being computed and then dropped.
    # Measured on a 26h-old post: the analyzer knew "the conversation has almost
    # certainly moved on", POST won on its own constant floor, and the card said
    # only "This belongs in a post of your own, not in someone else's thread" —
    # which asserts he has something to write and never mentions the closed
    # window. A gate that fires has to say so (the same rule the reply gates
    # already follow), even when the verdict is still to act.
    if (best["action"] != REPLY and analysis.reply_surface == "weak"
            and analysis.reply_surface_reason):
        decision.why_now = [analysis.reply_surface_reason] + [
            line for line in decision.why_now
            if line != analysis.reply_surface_reason]
    if runner_up:
        decision.outweighed = (
            f"{best['action']} ({best['score']:.2f}) over {runner_up['action']} "
            f"({runner_up['score']:.2f}); waiting scored {wait_score:.2f}.")
        # THE TRADE-OFF, in words. A strategist does not just name a move, they
        # say what it was chosen OVER and why — and when two routes are nearly
        # tied, that is the most useful sentence on the card. `outweighed`
        # already held this, but only as scores, which the card cannot show.
        if len(decision.why_now) < 3:
            close = _close_call_line(runner_up["action"],
                                     best["score"] - runner_up["score"], drivers)
            if close:
                decision.why_now.append(close)
    else:
        decision.outweighed = f"{best['action']} scored {best['score']:.2f}."
    return decision
