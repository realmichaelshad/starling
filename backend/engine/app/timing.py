"""Timing verdict engine: turn "here are your historical windows" into a
concrete call — post now, wait until a named time, or drop a stale topic.

Fuses three signals none of which is decisive alone:
- the account's own measured windows (performance.timing_recommendations),
- the topic's live lifecycle on X (a rising trend beats a calendar window —
  post age is a ranking input and conversations move on, so posting into a dead
  moment wastes the only window that compounds),
- today's cadence (author-diversity caps mean a fourth same-day original
  mostly competes with the first three).

Every verdict carries reasons and an honest confidence; correlations are
never presented as guarantees.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .performance import daily_post_budget, parse_datetime, timing_recommendations
from .storage import get_account_profile, performance_records, trend_frequencies

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _zone(timezone_name: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        # ValueError as well: `ZoneInfo("..")` and `ZoneInfo("")` raise that,
        # not ZoneInfoNotFoundError. The name is a stored profile field, so an
        # uncaught one is permanent rather than transient.
        #
        # And the fallback is `timezone.utc`, NOT `ZoneInfo("UTC")`. On Windows
        # `zoneinfo.TZPATH` is empty and the whole database comes from the
        # `tzdata` package — so on a machine without it, `ZoneInfo("UTC")`
        # raises too, from inside the handler, uncaught. A recovery path that
        # can fail the same way as the thing it is recovering from is not one.
        return timezone.utc


def _posts_today(rows: list[dict], now: datetime) -> int:
    """How many originals he has posted on HIS calendar day, not UTC's.

    This compared `created.astimezone(utc).date()` against
    `now.astimezone(utc).date()`, which is the mistake `_display_timezone` was
    written for — its docstring: "reported 'you posted ~19:24 UTC on Monday' for a
    post the user made on SUNDAY evening; the UTC calendar day had already rolled
    over."

    It is not cosmetic here, because this number gates the daily post budget and
    it was wrong in BOTH directions inside his actual posting hours (measured
    peak 18:00 local, with a real post at 20:06). On America/New_York:

      * a post at 20:06 on the 5th is 00:06 UTC on the 6th, so at 09:00 local on
        the 6th it still counted as "today" — the gate told him to hold off on a
        day he had not posted at all.
      * a post at 19:00 on the 5th is 23:00 UTC the same day, so at 21:00 local
        (01:00 UTC the 6th) it counted as ZERO — the gate let him post again over
        budget.

    The zone is resolved here rather than taken from `now`, because both callers
    (`planner.py` and `timing_verdict`) pass their own clock and only one of them
    is guaranteed to be in the account's zone.
    """
    # `account_facts._zone`, NOT the `_zone` in this module. Both resolve a zone
    # name; they differ on the failure that actually happens. Windows reports the
    # machine zone as a DISPLAY name — "Eastern Daylight Time" — which ZoneInfo
    # cannot parse, and `_display_timezone` returns exactly that whenever the
    # profile's timezone is unset or "UTC", i.e. for every user before their
    # profile is built. The local `_zone` answers UTC there, which is the very
    # thing this function was being fixed for; `account_facts._zone` answers the
    # machine's real tzinfo, which is right.
    from .account_facts import _display_timezone, _zone as _resolve_zone
    zone = _resolve_zone(_display_timezone(get_account_profile() or {}))
    today = now.astimezone(zone).date()
    count = 0
    for row in rows:
        if str(row.get("post_type") or "").lower() not in {"original", "post"}:
            continue
        created = parse_datetime(row.get("created_at"))
        if created and created.astimezone(zone).date() == today:
            count += 1
    return count


def _next_window_start(windows: list[dict], local_now: datetime) -> tuple[datetime | None, dict | None]:
    """The soonest upcoming (or currently open) window occurrence."""
    best: tuple[datetime, dict] | None = None
    for window in windows:
        try:
            day_index = _DAY_NAMES.index(str(window.get("day")))
            start_hour = int(window.get("start_hour"))
        except (ValueError, TypeError):
            continue
        days_ahead = (day_index - local_now.weekday()) % 7
        start = local_now.replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        if start < local_now - timedelta(hours=2):  # window already fully passed today
            start += timedelta(days=7)
        if best is None or start < best[0]:
            best = (start, window)
    return best if best else (None, None)


def _next_slot_after_today(windows: list[dict], local_now: datetime) -> datetime | None:
    """The earliest window occurrence on a strictly later day than today.

    Used when today's cadence budget is spent: the next clean slot is not later
    today (posting more today is the problem) but the next day that has a window.
    Falls back to ~24h out when there is no dated-window history to point at, so
    a schedule offer always has a concrete, honest target instead of guessing an
    hour it cannot support.
    """
    best: datetime | None = None
    for window in windows:
        try:
            day_index = _DAY_NAMES.index(str(window.get("day")))
            start_hour = int(window.get("start_hour"))
        except (ValueError, TypeError):
            continue
        days_ahead = (day_index - local_now.weekday()) % 7
        start = local_now.replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        if start.date() <= local_now.date():  # today or already passed — push a week
            start += timedelta(days=7 if days_ahead == 0 else 0)
            if start.date() <= local_now.date():
                start += timedelta(days=7)
        if best is None or start < best:
            best = start
    if best is None:
        return local_now + timedelta(hours=24)
    return best


def _window_open(window: dict | None, start: datetime | None, local_now: datetime) -> bool:
    if window is None or start is None:
        return False
    return start <= local_now < start + timedelta(hours=2)


def _friendly_hour(dt: datetime) -> str:
    """A 12-hour label like 'Tuesday 2 PM' without platform-specific strftime
    codes (%-I is glibc-only and raises on Windows)."""
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.strftime('%A')} {hour} {ampm}"


# Words that are three letters or longer and carry no subject. Without this a
# post about "the truth about local models" matched the trend "The Weeknd" on
# the token "the" and inherited that trend's lifecycle — which decides whether
# we say post-now, call the topic dead, or refuse to schedule at all.
_TOPIC_STOPWORDS = frozenset("""
the and but for with from you your our their they them was were are and its
this that these those has have had will would can could should not now just
about into what why how when who all any some more most than then there here
out off over new news top get got are why yes big one two via per
""".split())


def _subject_tokens(text: str) -> set[str]:
    """The words in a phrase that actually name a subject."""
    return {token.strip(".,:;!?\"'()[]").lower() for token in text.split()
            if len(token) >= 3} - _TOPIC_STOPWORDS


def _topic_lifecycle(topic: str) -> dict | None:
    """Live lifecycle for a topic straight from captured trends (no model call).

    A match has to be on a real subject word. Matching on filler produced
    confident lifecycle reads about entirely unrelated trends.
    """
    if not topic:
        return None
    topic_tokens = _subject_tokens(topic)
    if not topic_tokens:
        return None
    for trend in trend_frequencies(hours=8.0):
        # A lifecycle we could not measure says nothing about this topic; using
        # it would put "unknown" where the caller expects rising/peaking/fading.
        if str(trend.get("lifecycle") or "") == "unknown":
            continue
        if topic_tokens & _subject_tokens(str(trend["name"])):
            return trend
    return None


def timing_verdict(action: str = "post", topic: str | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """A concrete timing call: {verdict, wait_until?, reasons, confidence}."""
    from .algorithm_model import algorithm_brief
    profile = get_account_profile()
    timezone_name = str(profile.get("timezone") or "UTC")
    zone = _zone(timezone_name)
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(zone)
    rows = performance_records()
    objective = "conversation" if action == "reply" else "reach"
    timing = timing_recommendations(objective, timezone_name, rows)
    windows = timing.get("windows") or []
    window_objective = objective
    if not windows and action != "reply":
        # Reach shows no hour that beats this account's average. Conversation is
        # measured on the same posts and often does show one — a post published
        # into the hour its audience actually talks back still earns more than
        # one dropped into silence. It is a DIFFERENT claim, though (replies, not
        # impressions), so the fallback is labelled everywhere it surfaces
        # rather than quietly passed off as a reach window.
        fallback = timing_recommendations("conversation", timezone_name, rows)
        if fallback.get("windows"):
            timing, windows, window_objective = fallback, fallback["windows"], "conversation"
    reasons: list[str] = []
    confidence = timing.get("confidence", "limited")

    trend = _topic_lifecycle(topic) if topic else None
    lifecycle = str(trend.get("lifecycle")) if trend else None

    # Facts about the window read itself, true on every path out of here. Kept
    # in one place because a consumer that reads them must not have to know
    # which branch produced its verdict.
    window_facts: dict[str, Any] = {
        # Why there is (or is not) a window, and how much history it rests on.
        # "No hour beats your average" and "you have almost no dated posts" are
        # different facts, and a card that conflates them tells the user to fix
        # the wrong thing.
        "window_reason": timing.get("reason"),
        "dated_posts": timing.get("sample_size"),
        # Which metric the window was ranked on. "conversation" on a post means
        # reach showed nothing and this is the borrowed reply window.
        "window_objective": window_objective,
        "note": "Correlational signals, not guarantees.",
    }

    # A fading topic makes the clock irrelevant: the moment has passed.
    if lifecycle == "fading":
        return {
            **window_facts,
            "verdict": "topic_stale",
            "topic": topic,
            "reasons": [f"'{trend['name']}' is fading from X trends — by the time this lands, the moment has likely passed.",
                        "A fresh angle on a rising topic will earn more from the same effort."],
            "confidence": "directional",
            "windows": windows[:2],
            "topic_lifecycle": {"trend": trend["name"], "lifecycle": lifecycle, "velocity": trend.get("velocity")},
            "note": "Correlational signals, not guarantees.",
        }

    posted_today = _posts_today(rows, now)
    # Daily budget from the user's OWN cadence curve when it has enough data
    # ("your 4th post of the day lands at 60% of your median"), else 3.
    budget, budget_evidence = daily_post_budget(rows, timezone_name)
    if action == "post" and posted_today >= budget:
        # Over budget → the next clean slot is a LATER day's window, never later
        # today (posting more today is the problem the budget is guarding).
        next_slot = _next_slot_after_today(windows, local_now)
        wait_until = f"{_friendly_hour(next_slot)}" if windows else "tomorrow"
        # Far past budget (6+, or double the budget) is not a nuance call —
        # it is a hard stop, said bluntly.
        hard_stop = posted_today >= max(budget * 2, 6)
        if hard_stop:
            reasons = [f"{posted_today} originals today is actively hurting your earlier posts — every additional one "
                       "splits the same audience and feed slots. Hard stop until tomorrow.",
                       "Spend the rest of today answering replies on what you already posted — that compounds; "
                       "another original does not."]
        else:
            reasons = [f"{posted_today} standalone posts already dated today — feeds cap how often one author appears, "
                       "so another original mostly competes with your earlier posts.",
                       *([f"Measured on this account — {budget_evidence}."] if budget_evidence else []),
                       "A reply into a live conversation is the cleaner move for the rest of today."]
        return {
            **window_facts,
            "verdict": "wait_until",
            "wait_until": "tomorrow" if hard_stop else wait_until,
            "wait_until_iso": next_slot.astimezone(timezone.utc).isoformat(),
            "next_window_iso": next_slot.astimezone(timezone.utc).isoformat(),
            "next_window_label": f"{_friendly_hour(next_slot)} {timezone_name}",
            "severity": "hard" if hard_stop else "normal",
            "reasons": reasons,
            "confidence": "strong" if hard_stop else ("directional" if budget_evidence else "limited"),
            "daily_budget": budget,
            "windows": windows[:2],
            "note": "Correlational signals, not guarantees.",
        }

    next_start, next_window = _next_window_start(windows, local_now)
    in_window = _window_open(next_window, next_start, local_now)
    # Borrowed windows are named for what they actually measure, so no line
    # below can imply this hour was shown to earn more reach.
    borrowed = window_objective != objective
    what = "best window for replies" if borrowed else "measured best window"
    caveat = (" That hour is measured on conversation, not reach — your reach doesn't track the clock, "
              "but this is when your audience answers.") if borrowed else ""

    if lifecycle == "rising":
        reasons.append(f"'{trend['name']}' is rising on X right now — velocity beats the calendar; "
                       "a conversation that has moved on cannot be rejoined usefully.")
        if not in_window and windows:
            reasons.append("This is outside your usual best window, but a rising topic is worth the trade.")
        verdict = "post_now"
        confidence = "directional"
    elif in_window:
        window_label = f"{next_window['day']} {next_window['start_hour']:02d}:00–{next_window['end_hour']:02d}:00"
        # The window's OWN sample, not the size of the whole history — citing
        # 35 posts for an hour measured on 3 of them overstates the evidence.
        sample = int(next_window.get("sample_size") or 0)
        lift = next_window.get("relative_score")
        evidence = f"{sample} post{'s' if sample != 1 else ''} in that hour"
        if isinstance(lift, (int, float)) and lift > 1:
            evidence += f", running {round((float(lift) - 1) * 100)}% above your average"
        reasons.append(f"You are inside your {what} ({window_label} {timezone_name}, "
                       f"from {evidence}).{caveat}")
        verdict = "post_now"
    elif windows and next_start is not None:
        hours_away = (next_start - local_now).total_seconds() / 3600.0
        if hours_away <= 6:
            verdict = "wait_until"
            reasons.append(f"Your strongest {what} opens {next_start.strftime('%A %H:%M')} {timezone_name} "
                           f"(~{hours_away:.0f}h away); the first hour's engagement rate matters most, so posting "
                           f"into a quiet stretch spends that hour badly.{caveat}")
        else:
            # A day away is too far to sit on a good idea — freshness decays too.
            verdict = "post_now"
            reasons.append(f"Your next {what} ({next_start.strftime('%A %H:%M')}) is ~{hours_away:.0f}h away — "
                           "holding an idea that long usually costs more freshness than the window earns.")
    else:
        verdict = "post_now"
        confidence = "limited"
        reasons.append(str(timing.get("reason") or "Not enough dated history for a measured window — post when your "
                                                   "audience is most awake and measure the result."))

    result: dict[str, Any] = {
        **window_facts,
        "verdict": verdict,
        "reasons": reasons,
        "confidence": confidence,
        "windows": windows[:2],
        "algorithm_note": algorithm_brief("timing"),
    }
    if verdict == "wait_until" and next_start is not None:
        result["wait_until"] = next_start.strftime("%A %H:%M")
        # The concrete moment, so the scheduler can hold a draft to an exact time
        # rather than re-parsing a human label.
        result["wait_until_iso"] = next_start.astimezone(timezone.utc).isoformat()
    # Always expose the next window start (even on post_now) so a "schedule
    # anyway" option has a target.
    if next_start is not None:
        result["next_window_iso"] = next_start.astimezone(timezone.utc).isoformat()
        result["next_window_label"] = f"{_friendly_hour(next_start)} {timezone_name}"
    if trend:
        result["topic_lifecycle"] = {"trend": trend["name"], "lifecycle": lifecycle,
                                     "velocity": trend.get("velocity")}
    return result
