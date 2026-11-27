"""When this account is awake — measured from its own posts, in its own timezone.

TWO BUGS SAT ON TOP OF EACH OTHER HERE (found 2026-07-31).

**The frame was wrong.** `profile.timezone` is `TEXT DEFAULT 'UTC'` in the
schema and nothing ever sets it, so every hour the product reasoned about was
UTC while the user lived somewhere else. On this machine the account's posts
looked like they were spread from 01:00 to 05:00 and again from 12:00 to 23:00
with a dead patch at 06:00-11:00 — an odd, hard-to-read shape. Converted to the
machine's real zone (America/New_York) the same 46 records are an entirely
ordinary human day: nothing at all between 02:00 and 07:00, and a peak at 16:00.
The pattern was never strange; the clock was.

**The model was fit on 168 cells.** `performance.timing_recommendations`
buckets by (weekday, hour), which is 168 buckets for 46 posts, and it produced
exactly ONE window — Thursday 22:00, sample size 3. Every occurrence of it is
~150 hours away, so "wait for your window" was never actionable and
`timing_verdict` fell through to `post_now` at literally every hour of the day.
Hour-of-day is 24 buckets and is the most this much data can support.

NOTHING HERE IS TUNED TO ONE ACCOUNT. The timezone is the one the customer's
browser reports with each request (`X-Starling-Time-Zone`) or one they chose, the
histogram is the account's own posts, and the significance bar below adapts to
whatever sample that account has. (The engine runs on the server: its own clock
says nothing about the customer, so it is never asked.) An account
with a thin history gets NO dead hours and is never held back — silence about a
thing we cannot measure, rather than a guess.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

_HOURS_IN_DAY = 24

# A brand-new account must never be told to wait. Below this many dated posts
# we decline to have an opinion at all — a product floor, not a statistical
# one: the maths below can be "significant" on five posts simply because five
# posts cannot fill a day, and blocking somebody on that would be absurd.
MIN_POSTS_FOR_AN_OPINION = 20

# A dead run has to be long enough to be a real habit rather than a gap.
MIN_DEAD_RUN_HOURS = 3

# THE SIGNIFICANCE BAR, and the reason it is a formula rather than a constant.
# Under "this account posts at any hour", a run of L empty hours out of a
# sample of N has an expected occupancy of N*L/24. Requiring that expectation
# to reach 5 means a Poisson probability of seeing zero of about e^-5 ≈ 0.7%.
# It scales by itself: a 46-post account needs a 3-hour run, a 200-post account
# needs less, a 21-post account needs most of the night. No account-specific
# numbers anywhere.
MIN_EXPECTED_POSTS_IN_RUN = 5.0


def _zone(name: str | None) -> tzinfo:
    """Resolve a zone name, degrading to UTC (never the server's own clock).

    `timezone.utc`, not `ZoneInfo("UTC")`: without the tz database every name fails
    to load, INCLUDING "UTC", and a fallback that raises is no fallback.
    """
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:                                   # noqa: BLE001
            pass
    return timezone.utc


def effective_timezone() -> str | None:
    """The zone to reason in.

    A stored value that is not the schema default is a deliberate choice and
    always wins. `'UTC'` cannot be told apart from the untouched default, so
    otherwise the zone the browser reported for this request
    (`X-Starling-Time-Zone`) is used. The engine's own machine is never asked: it is
    a server, and its clock says nothing about the customer.
    """
    stored = ""
    try:
        from .storage import get_account_profile
        stored = str((get_account_profile() or {}).get("timezone") or "").strip()
    except Exception:                                       # noqa: BLE001
        stored = ""
    if stored and stored.upper() != "UTC":
        return stored
    from .request_context import time_zone
    requested = time_zone()
    if requested:
        return requested
    return stored or None


def local_now(now: datetime | None = None, tz: str | None = None) -> datetime:
    """`now` in the account's own clock.

    `tz` overrides the detected zone. It exists so the tests are deterministic
    on any machine: without it every expectation in them would be written in
    whatever zone the developer's laptop happens to be in, and the suite would
    pass in New York and fail in Amsterdam.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_zone(tz or effective_timezone()))


def _original_post_rows() -> list[dict[str, Any]]:
    try:
        from .storage import performance_records
        rows = performance_records() or []
    except Exception:                                       # noqa: BLE001
        return []
    kept = []
    for row in rows:
        if str(row.get("post_type") or "").lower() not in {"original", "post"}:
            continue
        if row.get("deleted_at"):
            continue
        kept.append(row)
    return kept


def hour_histogram(rows: list[dict[str, Any]] | None = None,
                   tz: str | None = None) -> dict[int, int]:
    """How many of this account's own posts fall in each LOCAL hour."""
    from .performance import parse_datetime
    zone = _zone(tz or effective_timezone())
    counts = {hour: 0 for hour in range(_HOURS_IN_DAY)}
    for row in (_original_post_rows() if rows is None else rows):
        stamp = parse_datetime(row.get("created_at") or row.get("first_observed_at") or "")
        if not stamp:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        counts[stamp.astimezone(zone).hour] += 1
    return counts


def _longest_zero_run(counts: dict[int, int]) -> list[int]:
    """The longest stretch of hours with no posts, wrapping past midnight.

    Wrapping matters and is easy to miss: a person who sleeps 23:00-06:00 has
    their dead run split across the end of the array, and a non-wrapping scan
    would report two short runs instead of one long one and find neither
    significant.
    """
    if not any(counts.values()):
        return []
    best: list[int] = []
    current: list[int] = []
    # Two laps so a run that crosses midnight is seen whole.
    for index in range(_HOURS_IN_DAY * 2):
        hour = index % _HOURS_IN_DAY
        if counts.get(hour, 0) == 0:
            current.append(hour)
            if len(current) > len(best):
                best = list(current)
        else:
            current = []
    return best[:_HOURS_IN_DAY]


def dead_hours(rows: list[dict[str, Any]] | None = None,
               tz: str | None = None) -> dict[str, Any]:
    """The hours this account has measurably never posted in.

    Returns `{hours, sample, run, expected, confident, timezone}`. `confident`
    is False whenever we do not have the evidence to hold anybody back, and
    every caller must treat that as "say nothing", never as "no dead hours
    found, so post".
    """
    counts = hour_histogram(rows, tz=tz)
    sample = sum(counts.values())
    zone_name = tz or effective_timezone()
    run = _longest_zero_run(counts)
    expected = sample * len(run) / _HOURS_IN_DAY if run else 0.0
    confident = (sample >= MIN_POSTS_FOR_AN_OPINION
                 and len(run) >= MIN_DEAD_RUN_HOURS
                 and expected >= MIN_EXPECTED_POSTS_IN_RUN)
    return {"hours": sorted(run) if confident else [],
            "run": run if confident else [],
            "sample": sample,
            "expected": round(expected, 2),
            "confident": confident,
            "timezone": zone_name}


def _friendly(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12} {suffix}"


def hour_verdict(now: datetime | None = None,
                 rows: list[dict[str, Any]] | None = None,
                 tz: str | None = None) -> dict[str, Any]:
    """Is this hour one the account never posts in, and when does that end?

    `{"dead": bool, "reason": str, "until_local": str, "hours_left": int, ...}`
    """
    facts = dead_hours(rows, tz=tz)
    here = local_now(now, tz=tz)
    if not facts["confident"] or here.hour not in set(facts["hours"]):
        return {"dead": False, "reason": "", "local_hour": here.hour, **facts}
    # The end of the run as the user experiences it, walking forward from now.
    dead = set(facts["hours"])
    ahead = 0
    cursor = here
    while cursor.hour in dead and ahead <= _HOURS_IN_DAY:
        cursor = cursor + timedelta(hours=1)
        ahead += 1
    wake = cursor.replace(minute=0, second=0, microsecond=0)
    span = len(facts["run"])
    reason = (
        f"It is {_friendly(here.hour)} where you are, and you have never "
        f"posted at this hour — not once in {facts['sample']} posts. Your "
        f"quiet stretch runs {_friendly(min(facts['hours']))} to "
        f"{_friendly((max(facts['hours']) + 1) % _HOURS_IN_DAY)}. "
        f"Save it for {_friendly(wake.hour)}, about "
        f"{ahead} hour{'s' if ahead != 1 else ''} from now."
    )
    return {"dead": True, "reason": reason, "local_hour": here.hour,
            "until_local": wake.isoformat(), "hours_left": ahead,
            "run_length": span, **facts}


# SAYING "WAIT" MUST NOT MEAN "NO". The card tells him he can have it anyway,
# and a promise the code cannot keep is worse than not offering. Note which
# direction the errors run: a false positive here just writes the post, which
# is exactly what happened before this gate existed, while a false negative
# traps him in a refusal he was explicitly invited to override. So this list is
# deliberately permissive — the opposite of how a REJECTING gate is tuned.
_OVERRIDE_PHRASES = (
    "anyway", "any way", "regardless", "i don't care", "i dont care",
    "ignore the time", "ignore timing", "i know", "do it", "just write",
    "write it now", "post it now", "post now", "right now", "override",
    "no wait", "dont wait", "don't wait", "yes", "go ahead",
)


def wants_it_anyway(message: str | None) -> bool:
    """Did the user explicitly ask for the post despite the hour?"""
    text = " ".join(str(message or "").lower().split())
    if not text:
        return False
    return any(phrase in text for phrase in _OVERRIDE_PHRASES)


