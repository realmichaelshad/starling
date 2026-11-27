"""The writing brief for a FROM-SCRATCH POST, built from the radar grounding.

WHY THIS EXISTS. A reply gets `reply_plan.plan_reply`; a quote gets
`quote_plan.plan_quote`. A post gets one static direction, and the measured
consequence is that the product has produced **0% shitposts against an archive
that is 48% shitpost** (n=65, hand-read). Every generated post is a bulletin.

THE REGISTER IS SELECTED BY THE MOMENT, AND THE MOMENT DEFAULTS TO QUIET.
Tested against his archive: a fact-stating post appeared in a loud window
14 times out of 14 and in a quiet window 0 times — so quiet => never facts is
strongly supported. The converse is false: 10 of his 31 shitposts landed in
loud windows, six of them on the loudest day in the archive. **The moment is a
GATE that unlocks the fact register, never a switch that forces it.** Built as a
switch, he would see the product change on exactly the days he posts most.

`unknown` RESOLVES TO SHITPOST, DELIBERATELY. Today 87% of pool headlines carry
no date and the product treats every one as newsworthy — that is the draft he
complained about. Defaulting the other way makes honest uncertainty produce the
register that is 22% of his archive instead of the one that is 48%. It fails in
the cheaper direction: a shitpost about something that turned out big is a
missed opportunity; a confident bulletin about something stale is the complaint.

PURE BY CONSTRUCTION. Takes the grounding dict rather than a context — a
from-scratch post has no visible post to read a room off, so its whole situation
is the radar output. No DB, no network, no clock but the injected one, and every
failure path returns a shitpost brief rather than raising: a post is never lost
because the planner could not read its inputs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

# A KNOWN age under this is the only age-based route to the fact register.
# ~12.5% of a real pool clears it, against his 22% archive fact rate;
# `release.status == "shipped"` is expected to close the gap. If the observed
# rate lands outside 15-35% the threshold is wrong — but it is re-derived from
# the age distribution of posts he APPROVES, never tuned to hit 22%.
BREAKING_AGE_SECONDS = 6 * 3600
# THE BAND BOUNDARY, and it is load-bearing rather than decorative. An earlier
# draft of this module defined it and never read it: two branches produced the
# same "quiet" label and differed only in their reason wording, so collapsing
# them looked safe and silently orphaned the constant. The routing was still
# correct — everything past 6h is a shitpost — but a 7-hour story and a
# 400-hour story recorded as the same moment, which contaminates the very
# column we are adding to answer "which register performs", and leaves a knob
# that reads as tunable and does nothing.
QUIET_AGE_SECONDS = 24 * 3600
# His longest real shitpost is 30 words; the register plans short and medium
# only. Measured split of his 31: 19 short / 12 medium / 0 long.
SHITPOST_SIZE_CLASSES = ("short", "medium")

Register = Literal["facts", "shitpost"]
# THE MOMENT IS A PURE AGE BAND, shared verbatim with the candidate dict so the
# card and the register can never disagree about the same story. Boundaries are
# 6h and 24h, agreed as one contract rather than two implementations — the
# `TREND_FRESH_HOURS` bug in this codebase came from a gate using 2h while its
# consumers used 48h, and they disagreed on 82.9% of cases.
Moment = Literal["breaking", "recent", "old", "unknown"]


@dataclass(frozen=True)
class PostRoom:
    """What the moment is, and what the post should therefore be."""

    register: Register = "shitpost"
    move: str = "riff"
    moment: Moment = "unknown"
    reason: str = ""
    signals: dict[str, Any] = field(default_factory=dict)
    size_classes: tuple[str, ...] = SHITPOST_SIZE_CLASSES
    tone: str = "funny"
    # POPULATED AT STEP 8 BY THE READER, which owns both hook tables
    # (`HOOK_STYLES` and `SHITPOST_HOOK_STYLES` live together in
    # `intelligence.py`). Empty here means "the caller uses its own default",
    # which is exactly the behaviour while this module ships dark.
    hooks: tuple[str, ...] = ()


_MOVES: dict[str, str] = {
    "state_the_fact": (
        " Something just shipped and the reader may not know it yet. Lead with "
        "what happened, name it, and say the one thing that changes. This is "
        "the register that earns a headline — use it only because the moment "
        "is real."),
    "riff": (
        " Something is not breaking right now, so this is a post, not a "
        "bulletin. One line. Say the thing you would actually say — the joke, "
        "the aside, the thing that is funny about it. Do not build up to it, do "
        "not explain it, and do not add a second sentence to justify the first. "
        "Naming the product is fine; a thesis is not."),
}


def _dig(grounding: Any, *path: str) -> Any:
    """A nested read that never raises on a malformed grounding."""
    node: Any = grounding
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _age_seconds(grounding: Any, now: float) -> tuple[float | None, str]:
    """(age, source). `published_at` is preferred; `created_at` is the fallback.

    Returns (None, "none") when neither is present or parseable — the honest
    reading of a pool where 87% of headlines carried no date, and the input that
    must land in `shitpost` rather than `facts`.
    """
    for key in ("published_at", "created_at"):
        raw = _dig(grounding, key)
        if raw is None:
            raw = _dig(grounding, "candidate", key)
        if raw in (None, "", 0):
            continue
        try:
            stamp = float(raw)
        except (TypeError, ValueError):
            continue
        if stamp <= 0:
            continue
        age = now - stamp
        if age < 0:
            # A future timestamp is a broken timestamp, not fresh news.
            continue
        return age, key
    return None, "none"


def read_the_moment(grounding: Any, *, now: float | None = None) -> PostRoom:
    """Which register this moment admits, and why.

    The signals, in the order they may decide anything:

      1  release verified shipped      -> breaking          (decisive)
      2  a KNOWN age under 6 hours     -> breaking          (decisive)
         a KNOWN age of 24h or more    -> quiet             (decisive)
      3  `is_release_topic`            -> unknown, never breaking
      4  two or more evidence sources  -> unknown, never breaking

    The trend-lifecycle signal is deliberately absent: 49 captures in a month
    means an 8-hour window is usually empty, so it is advisory at best and never
    decisive. It is not read here at all, rather than read and ignored.
    """
    clock = time.time() if now is None else float(now)
    signals: dict[str, Any] = {}

    shipped = str(_dig(grounding, "release", "status") or "").lower() == "shipped"
    signals["release_shipped"] = shipped

    age, age_source = _age_seconds(grounding, clock)
    signals["age_source"] = age_source
    signals["age_seconds"] = None if age is None else round(age, 1)

    topic = str(_dig(grounding, "topic") or "")
    release_topic = False
    if topic:
        try:
            from .release_radar import is_release_topic
            release_topic = bool(is_release_topic(topic))
        except Exception:                                    # noqa: BLE001
            release_topic = False
    signals["release_topic"] = release_topic

    sources = _dig(grounding, "evidence_sources")
    distinct = (len({str(s) for s in sources if s})
                if isinstance(sources, (list, tuple)) else 0)
    signals["evidence_sources"] = distinct

    if shipped:
        return PostRoom(register="facts", move="state_the_fact", moment="breaking",
                        reason="a release is verified shipped", signals=signals,
                        size_classes=(), tone="direct")
    if age is not None and age < BREAKING_AGE_SECONDS:
        return PostRoom(register="facts", move="state_the_fact", moment="breaking",
                        reason=f"the material is {age / 3600.0:.1f}h old, inside the breaking window",
                        signals=signals, size_classes=(), tone="direct")

    # EVERYTHING BELOW IS A SHITPOST — the register is a two-way decision and
    # only `breaking` admits facts. What remains is the PROVENANCE label, which
    # is a pure function of the age we hold.
    if age is None:
        moment: Moment = "unknown"
        reason = "no date on the material"
        # `is_release_topic` and the source count are recorded in `signals` and
        # deliberately change nothing here: the vocabulary matcher fires on
        # week-old retrospectives, so letting it colour the moment would smuggle
        # "smells like news" into a field that means "how old is this".
    elif age < QUIET_AGE_SECONDS:
        moment = "recent"
        reason = f"the material is {age / 3600.0:.1f}h old, past the breaking window"
    else:
        moment = "old"
        reason = f"the material is {age / 3600.0:.0f}h old"

    return PostRoom(register="shitpost", move="riff", moment=moment,
                    reason=reason, signals=signals,
                    size_classes=SHITPOST_SIZE_CLASSES, tone="funny")


def post_move(room: PostRoom) -> str:
    """The move text for this room. Empty when the move is unknown."""
    return _MOVES.get(room.move, "")


def plan_post(grounding: Any = None, *, now: float | None = None) -> tuple[str, PostRoom]:
    """The whole post brief as one string to append, plus the room.

    Mirrors `plan_reply` / `plan_quote`: `(instruction, room)`. Never raises — a
    grounding this cannot read yields the shitpost brief, which is the register
    48% of his archive is written in.
    """
    try:
        room = read_the_moment(grounding, now=now)
    except Exception:                                        # noqa: BLE001
        room = PostRoom(reason="the moment could not be read",
                        signals={"age_source": "none", "error": True})
    return post_move(room), room
