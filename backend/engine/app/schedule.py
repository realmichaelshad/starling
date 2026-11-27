"""Scheduling recommendation: should this finished draft go out now, or be
held for a better moment — and if held, exactly when and why.

This is HOLD-AND-REMIND, never auto-post. Starling has no capability to post
to X and never will; a "scheduled" draft is stored with a target time and
surfaced back to the user as "ready to post now" when that time arrives. The
human always publishes. Nothing in this module talks to X.

The recommendation fuses four signals, none decisive alone, into one clear call
the create-post card can show ("more detailed and understandable"):

1. Freshness decay of THIS draft — a reactive "breaking / just shipped" post is
   worthless in a day; an analysis piece keeps. This is measured from the text
   and caps how far out we are willing to hold it.
2. The account's own best windows — measured hours when its posts earn the most.
3. Topic momentum — a rising conversation says post now (velocity beats the
   calendar); a fading one says the moment already passed.
4. Cadence / daily budget — feeds cap how often one author surfaces, so a fourth
   same-day original mostly competes with the earlier three; the next slot is
   tomorrow's window.

Signals 2-4 are already fused by timing.timing_verdict; this module adds the
freshness gate, turns the verdict into a concrete schedule offer with an exact
target time, and renders the plain-English "why" plus a topic prediction.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .timing import timing_verdict

# How long a draft of each freshness class stays worth posting. If the next
# good window is farther out than this, holding costs more freshness than the
# window earns — so we stop recommending the wait (the user may still choose it).
_MAX_HOLD_HOURS = {"fast": 4.0, "medium": 12.0, "slow": 30.0}

# No recommendation ever holds a draft longer than this, whatever the class.
# A measured window can legitimately sit days out (a Wednesday-only history
# read on a Sunday); "hold this until Wednesday" is never a serious answer to
# "should I post this?", so the ceiling bounds every branch below.
_ABSOLUTE_MAX_HOLD_HOURS = 36.0

# How long the CONVERSATION stays worth joining, by its live lifecycle. The
# window only pays if the topic is still alive when you arrive — a peaking
# trend is already spending its last hours, so it caps the hold regardless of
# how well the draft itself keeps.
_TOPIC_HOLD_HOURS = {"rising": 0.0, "peaking": 10.0, "fading": 0.0}

# Dated posts below which "no measured window" means "not enough data" rather
# than "your results genuinely do not track the clock". Mirrors the minimum
# performance.timing_recommendations needs before it will rank hours at all.
_ENOUGH_DATED_POSTS = 30

# Reactive / time-stamped language: value collapses within hours.
_FAST_CUES = re.compile(
    r"\b(breaking|just\s+(shipped|launched|dropped|announced|released|posted|saw|got)|"
    r"right\s+now|happening\s+now|live\s+now|today'?s|this\s+morning|this\s+afternoon|"
    r"tonight|moments?\s+ago|announced|unveiled|as\s+of\s+today)\b",
    re.IGNORECASE,
)
# Evergreen / analysis language: keeps its value for a day or more.
_SLOW_CUES = re.compile(
    r"\b(why|how\s+to|here'?s\s+(how|what|why)|lesson|framework|principle|"
    r"i\s+(learned|realized|think|believe)|prediction|predict|over\s+the\s+(next|coming)|"
    r"thread|deep\s+dive|the\s+truth\s+about|underrated|most\s+people)\b",
    re.IGNORECASE,
)
# Pegged to an event — a launch, a release, a version, a competitor's move.
# Commentary on news is only as fresh as the news: "why does Opus 5 being
# cheaper matter" reads like analysis, but it dies with the release cycle it is
# analysing. This is what stops an analysis cue from buying a multi-day hold on
# a post whose whole subject is three days old by then.
_NEWS_PEG_CUES = re.compile(
    r"\b(launch(ed|es|ing)?|releas(e|ed|es|ing)|ship(ped|ping|s)?|announc(e|ed|es|ement)|"
    r"unveil(ed|s|ing)?|debut(ed|s|ing)?|roll(ed|ing)?\s+out|out\s+now|now\s+available|"
    r"new\s+(model|version|release|update)|latest\s+(model|version|release)|"
    r"benchmark(s|ed)?|pricing|price\s+(cut|drop)|deprecat(ed|es|ing)|sunset(ting)?)\b"
    # "two months after", "a day ago", "last week" — dating one event against
    # another is the tell that the post is anchored to a moment in time.
    r"|\b(\d+|a|an|one|two|three|few)\s+(minute|hour|day|week|month)s?\s+(ago|after|later|before)\b"
    r"|\b(yesterday|last\s+(night|week))\b",
    re.IGNORECASE,
)


def classify_freshness(text: str) -> str:
    """fast | medium | slow — how quickly this specific draft goes stale.

    A draft can carry several cues at once; the shortest-lived one wins, because
    it is the half that dies first. A reactive hook on an evergreen point dies
    with the reaction; analysis of a launch dies with the launch.
    """
    body = text or ""
    if _FAST_CUES.search(body):
        return "fast"
    # A news peg outranks the analysis cues below: a post explaining why a fresh
    # release matters is not evergreen, however essay-like its phrasing.
    if _NEWS_PEG_CUES.search(body):
        return "medium"
    if _SLOW_CUES.search(body):
        return "slow"
    return "medium"


def _is_news_pegged(text: str) -> bool:
    """Is this draft anchored to an event rather than standing on its own?"""
    return bool(_NEWS_PEG_CUES.search(text or ""))


def _freshness_line(cls: str, text: str = "") -> str:
    if cls == "fast":
        return ("This reads as a reactive, time-stamped post — it loses most of its value "
                "within a few hours, so it is not worth holding overnight.")
    if cls == "slow":
        return ("This reads as an evergreen / analysis post — it keeps its value for a day or "
                "more, so holding it for a stronger window costs little.")
    if _is_news_pegged(text):
        return ("This is analysis pegged to a news event — it reads evergreen, but it is only as "
                "fresh as the thing it is about, so it does not survive a multi-day hold.")
    return "This reads as a general post — it keeps for a while, but freshness still fades across a day."


def _prediction(topic: str | None, lifecycle: dict | None) -> dict[str, Any]:
    """A plain read of where the topic is headed — the 'when will it do well'
    signal, straight from the live trend lifecycle (no model, no guess)."""
    if not topic:
        return {"read": "No specific trending topic attached — timing is driven by your own windows, not a conversation.",
                "lifecycle": None}
    if not lifecycle:
        return {"read": f"'{topic}' is not moving on X trends right now, so there is no live wave to time against — "
                        "your own best window is the better guide.",
                "lifecycle": None}
    name = lifecycle.get("trend") or topic
    phase = str(lifecycle.get("lifecycle") or "")
    velocity = lifecycle.get("velocity")
    if phase == "rising":
        read = (f"'{name}' is still climbing on X (velocity {velocity:+.2f}). Momentum is ahead of you right now; "
                "a conversation this alive is best joined immediately — waiting risks arriving after the peak.")
    elif phase == "fading":
        read = (f"'{name}' is past its peak on X (velocity {velocity:+.2f}). The wave has broken — post now if at all; "
                "scheduling into a dead topic earns nothing.")
    else:  # peaking / steady
        read = (f"'{name}' is at or near its peak on X (velocity {velocity:+.2f}) — steady, not surging. The topic "
                "itself won't reward a specific hour, so your own best window is what to optimize.")
    return {"read": read, "lifecycle": phase or None, "velocity": velocity, "trend": name}


def _hold_budget(freshness: str, lifecycle: dict | None) -> tuple[float, str | None]:
    """How long we are willing to hold this draft, and what set the limit.

    Two independent clocks run against a scheduled draft and the shorter one
    decides: how long the DRAFT keeps (freshness) and how long the CONVERSATION
    keeps (topic lifecycle). Fusing them here — rather than checking freshness
    in one branch and lifecycle in another — is what stops a window from being
    offered days out because each clock looked fine on its own.
    """
    budget = min(_MAX_HOLD_HOURS[freshness], _ABSOLUTE_MAX_HOLD_HOURS)
    phase = str((lifecycle or {}).get("lifecycle") or "")
    topic_budget = _TOPIC_HOLD_HOURS.get(phase)
    if topic_budget is not None and topic_budget < budget:
        return topic_budget, "topic"
    return budget, "freshness"


def _within_budget(hold_hours: float | None, budget: float) -> bool:
    """Is holding to this target defensible? Sub-30-minute holds are noise, and
    anything past the budget is the bug this gate exists to stop."""
    return hold_hours is not None and 0.5 < hold_hours <= budget


def _too_far_line(label: str | None, hold_hours: float, freshness: str, capped_by: str | None,
                  borrowed: bool = False) -> str:
    """Why we are declining to recommend a real, measured window."""
    where = f"({label}) " if label else ""
    when = f"~{hold_hours:.0f}h away" if hold_hours < 48 else f"~{hold_hours / 24:.0f} days away"
    window = "best reply window" if borrowed else "next measured window"
    if capped_by == "topic":
        return (f"Post now — your {window} {where}is {when}, and the conversation "
                "will have moved on well before then.")
    if freshness == "fast":
        return (f"Post now — your {window} {where}is {when}, and a reactive post is stale "
                "long before then.")
    return (f"Post now — your {window} {where}is {when}. Nothing keeps that long; "
            "holding it costs more than the window earns.")


def _hours_between(target_iso: str, now: datetime) -> float | None:
    try:
        target = datetime.fromisoformat(target_iso)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return (target - now).total_seconds() / 3600.0


def recommend_schedule(draft_text: str, action: str = "post", topic: str | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
    """One clear call for a finished draft: post now, or hold to a named time.

    Returns a card-ready dict:
      default        "post_now" | "schedule"  — what we recommend
      offer          bool                     — is scheduling actually worthwhile?
      target_iso     str | None               — exact UTC moment to hold until
      target_label   str | None               — "Tuesday 2 PM ET"
      freshness      "fast" | "medium" | "slow"
      hold_hours     float | None             — how long the hold would be
      hold_budget    float                    — the longest hold we would back
      allow_manual   bool                     — may the user pick their own time?
      confidence     "strong" | "directional" | "limited"
      reason         str                      — plain-English why, naming signals
      topic_prediction dict
      signals        {best_window, topic_momentum, freshness, cadence}
    """
    now = now or datetime.now(timezone.utc)
    freshness = classify_freshness(draft_text)

    verdict = timing_verdict(action=action, topic=topic, now=now)
    kind = verdict.get("verdict")
    reasons = list(verdict.get("reasons") or [])
    confidence = verdict.get("confidence", "limited")
    lifecycle = verdict.get("topic_lifecycle")
    prediction = _prediction(topic, lifecycle)

    # The one budget every branch below answers to. Whichever clock runs out
    # first — the draft's freshness or the conversation's — decides.
    max_hold, capped_by = _hold_budget(freshness, lifecycle)

    target_iso = verdict.get("wait_until_iso") or verdict.get("next_window_iso")
    target_label = verdict.get("next_window_label")
    hold_hours = _hours_between(target_iso, now) if target_iso else None
    # The measured window is a fact about the account and stays true in the
    # breakdown even when we decline to hold the draft for it.
    measured_label = target_label

    # Per-signal breakdown so the card can show the four-way reasoning.
    windows = verdict.get("windows") or []
    # A window borrowed from the conversation ranking answers a different
    # question than the one the user asked about a post, so it is never
    # described as though reach had produced it.
    borrowed = verdict.get("window_objective") == "conversation" and action != "reply"
    window_is = ("Your best window for replies is" if borrowed else "Your measured best window is")
    borrowed_note = (" It is measured on conversation, not reach — no hour beats your average on reach, "
                     "so this is when your audience answers rather than when you travel furthest.") if borrowed else ""
    if measured_label and hold_hours is not None and hold_hours > max_hold:
        best_window = (f"{window_is} {measured_label} — but that is "
                       f"{'~%.0fh' % hold_hours if hold_hours < 48 else '~%.0f days' % (hold_hours / 24)} out, "
                       f"further than this draft stays worth posting.{borrowed_note}")
    elif measured_label:
        best_window = f"{window_is} {measured_label}.{borrowed_note}"
    else:
        # Two different situations, and the fix for each is different: thin
        # history is solved by posting more, a flat clock is not solved at all.
        best_window = str(verdict.get("window_reason") or
                          "Not enough dated history yet for a measured window — post when your audience is most awake.")
    momentum_line = prediction["read"]
    cadence_line = (f"You have already posted your daily budget ({verdict.get('daily_budget')}) — the next clean slot is "
                    f"{verdict.get('wait_until')}." if kind == "wait_until" and verdict.get("daily_budget")
                    else "Cadence has room for another original today.")

    signals = {
        "best_window": best_window,
        "topic_momentum": momentum_line,
        "freshness": _freshness_line(freshness, draft_text),
        "cadence": cadence_line,
    }

    over_budget = kind == "wait_until" and verdict.get("daily_budget") is not None
    # A firm post-now call (a live or dead conversation) is the one case where
    # scheduling is not a choice we support at all — holding a rising topic
    # throws away the whole opportunity, and a faded one cannot be revived.
    allow_manual = True

    # ---- Combine into a single call ---------------------------------------
    if over_budget:
        # Cadence is a hard signal — being over today's budget outranks freshness.
        # Posting now is the thing the budget is guarding against, so we do not
        # tell the user to post now even for a reactive draft; we hold to the
        # next day's window and, for a fast-decay draft, name the tradeoff.
        # The ceiling still applies: "you posted too much today" justifies
        # tomorrow, never the back half of the week.
        default, offer = "schedule", True
        # Cadence, not freshness, is the binding constraint here — report the
        # allowance we actually applied rather than a freshness budget this
        # branch is deliberately overriding.
        max_hold, capped_by = _ABSOLUTE_MAX_HOLD_HOURS, "cadence"
        if hold_hours is None or hold_hours > _ABSOLUTE_MAX_HOLD_HOURS:
            # Either the account has no window to point at, or its only one is
            # days out. Both resolve the same way: the point is to clear today,
            # not to hit a window, so pin the hold to tomorrow. Without this the
            # headline rendered the literal string "Hold for None".
            target_iso = (now + timedelta(hours=24)).isoformat()
            target_label = "tomorrow"
            hold_hours = 24.0
        when = target_label or verdict.get("wait_until") or "tomorrow"
        if freshness == "fast":
            headline = (f"Hold for {when} — but heads up: you're over today's "
                        "budget AND this is a reactive post, so by tomorrow it may be stale. Posting it now would "
                        "just compete with today's posts. Your call.")
        elif (lifecycle or {}).get("lifecycle") == "rising":
            # Over budget AND the conversation is live: both moves cost something,
            # so name that instead of presenting the hold as clean.
            subject = prediction.get("trend") or topic or "that conversation"
            headline = (f"Hold for {when} — you've hit today's post budget. But {subject} "
                        "is climbing now and won't wait, so posting anyway is a defensible call. Your choice.")
        else:
            headline = (f"Schedule for {when} — you've hit today's post budget, "
                        "so tomorrow's window is the clean slot.")
    elif kind == "topic_stale":
        # A fading topic: the moment passed. Holding a draft cannot bring it back.
        default, offer, allow_manual = "post_now", False, False
        headline = "Post now (or drop it) — don't schedule."
        target_iso = target_label = None
        hold_hours = None
    elif kind == "post_now" and (lifecycle or {}).get("lifecycle") == "rising":
        # A rising conversation: live momentum is the whole opportunity. Holding
        # it is actively wrong, so we do not even offer a schedule target — a
        # conversation that has moved on cannot be rejoined later.
        default, offer, allow_manual = "post_now", False, False
        headline = "Post now — the topic is live and holding it loses the momentum."
        target_iso = target_label = None
        hold_hours = None
    elif kind == "wait_until" and _within_budget(hold_hours, max_hold):
        # A real better window, close enough that both clocks survive the wait.
        default, offer = "schedule", True
        headline = f"Schedule for {target_label or verdict.get('wait_until')} — the wait is worth it."
    else:
        # Everything else is a post-now call. The only question left is whether
        # a measured window is close enough to be worth naming as an
        # alternative — and it is worth naming only when holding to it is
        # actually defensible. A window past the budget is dropped entirely
        # rather than offered with a caveat: an offered time reads as a
        # recommendation no matter what the sentence around it says.
        default = "post_now"
        offer = _within_budget(hold_hours, max_hold) and freshness != "fast"
        if offer:
            # Close enough to hold, but not clearly better than posting now —
            # a genuine second option rather than the recommendation.
            headline = (f"Post now — or hold it for {target_label or verdict.get('wait_until')} "
                        f"(~{hold_hours:.0f}h) if you'd rather catch your measured window.")
        elif hold_hours is not None and hold_hours > max_hold and measured_label:
            headline = _too_far_line(measured_label, hold_hours, freshness, capped_by, borrowed)
        elif windows:
            headline = "Post now — you're in (or near) your best window."
        elif int(verdict.get("dated_posts") or 0) >= _ENOUGH_DATED_POSTS:
            # There IS history; it just shows no clock effect. Telling the user
            # to go collect more data would be advice for a problem they do not
            # have — the honest read is that timing is not their lever.
            headline = ("Post now — no hour in your history reliably beats your average, "
                        "so there is no better moment to wait for.")
        else:
            headline = "Post now — not enough history to time this; post and measure."
        if not offer:
            target_iso = target_label = None
            hold_hours = None

    reason = _compose_reason(headline, signals, freshness, confidence)

    return {
        "default": default,
        "offer": offer,
        "allow_manual": allow_manual,
        "target_iso": target_iso,
        "target_label": target_label,
        "freshness": freshness,
        "hold_hours": round(hold_hours, 1) if hold_hours is not None else None,
        "hold_budget": round(max_hold, 1),
        "confidence": confidence,
        "headline": headline,
        "reason": reason,
        "topic_prediction": prediction,
        "signals": signals,
        "note": "Hold-and-remind only — Starling never posts for you. Correlational signals, not guarantees.",
    }


def _compose_reason(headline: str, signals: dict[str, str], freshness: str, confidence: str) -> str:
    """A single readable paragraph that names every signal's contribution."""
    # "moderate" reaches here from timing_recommendations and was silently
    # falling through to the directional wording, dressing up a thinner read.
    conf_word = {"strong": "This is well-supported", "moderate": "This is a moderate read",
                 "directional": "This is directional",
                 "limited": "This is a low-confidence read"}.get(confidence, "This is a directional read")
    return (
        f"{headline}\n\n"
        f"• Best window — {signals['best_window']}\n"
        f"• Topic — {signals['topic_momentum']}\n"
        f"• Freshness — {signals['freshness']}\n"
        f"• Cadence — {signals['cadence']}\n\n"
        f"{conf_word} from your own history. You always post it yourself — scheduling just holds the draft and "
        "reminds you when the moment arrives."
    )
