"""The shared evidence model (spec §1). Every conclusion says what it rests on.

The spec's rule is short and absolute: *unknown data must remain unknown*. A
missing reply count is not zero replies; a missing profile snapshot is not proof
the account did not grow. This module makes that mechanical rather than a habit
each caller has to remember — a value arrives wrapped in what is known about it,
and a `None` value can never be silently read as 0.

Four kinds of information, with different rights (§1):

    OBSERVED_FACT     can support a direct statement
    DERIVED_PATTERN   can rank options; must show sample size and confidence
    FORECAST          can influence a recommendation; is never stated as fact
    USER_PREFERENCE   overrides inferred style and strategy
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# --- the four evidence kinds (§1) ------------------------------------------
OBSERVED_FACT = "observed_fact"
DERIVED_PATTERN = "derived_pattern"
FORECAST = "forecast"
USER_PREFERENCE = "user_preference"

# --- freshness (§1) --------------------------------------------------------
FRESH = "fresh"
AGING = "aging"
STALE = "stale"
UNKNOWN_FRESHNESS = "unknown"

# --- confidence (§1) -------------------------------------------------------
STRONG = "strong"
DIRECTIONAL = "directional"
LIMITED = "limited"
UNKNOWN_CONFIDENCE = "unknown"

# --- scope (§1) ------------------------------------------------------------
ACCOUNT_WIDE = "account_wide"
FORMAT_SPECIFIC = "format_specific"
TOPIC_SPECIFIC = "topic_specific"
AUTHOR_SPECIFIC = "author_specific"
ONE_OFF = "one_off"

# Sources the spec names (§1). Free-form strings are accepted; these exist so
# the common ones are spelled the same way everywhere.
SOURCE_ARCHIVE = "archive"
SOURCE_ANALYTICS = "analytics_export"
SOURCE_USER_CONFIRMED = "user_confirmed_preference"
SOURCE_VISIBLE_X = "visible_x_page"
SOURCE_RESEARCH = "approved_public_research"
SOURCE_DRAFT_OUTCOME = "starling_draft_outcome"

# How quickly an observation ages. These are deliberately generous: the spec
# ties freshness to "the latest permitted observation with a timestamp", and a
# four-hour-old follower count is still a usable fact even though it is not new.
_FRESH_WITHIN = timedelta(minutes=30)
_AGING_WITHIN = timedelta(hours=6)

# Sample sizes behind a derived pattern before it may claim more than a hint.
# Mirrors the thresholds already used by the performance model and the bandit,
# so one number does not mean two different things in two places.
_DIRECTIONAL_SAMPLES = 8
_STRONG_SAMPLES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    """Best-effort ISO parse. Returns None rather than guessing a time."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def freshness_of(observed_at: Any, *, now: datetime | None = None) -> str:
    """fresh / aging / stale, or unknown when there is no usable timestamp.

    An unparseable or missing timestamp is UNKNOWN, never STALE: not knowing
    when something was seen is a different statement from knowing it was seen
    long ago, and the spec requires the product to be able to say which.
    """
    seen = parse_time(observed_at)
    if seen is None:
        return UNKNOWN_FRESHNESS
    age = (now or _now()) - seen
    if age <= _FRESH_WITHIN:
        return FRESH
    if age <= _AGING_WITHIN:
        return AGING
    return STALE


def confidence_for_samples(n: int | None) -> str:
    """Confidence a derived pattern earns from its sample size alone."""
    if n is None or n <= 0:
        return UNKNOWN_CONFIDENCE
    if n >= _STRONG_SAMPLES:
        return STRONG
    if n >= _DIRECTIONAL_SAMPLES:
        return DIRECTIONAL
    return LIMITED


@dataclass
class Evidence:
    """One value plus everything needed to judge it.

    `value` may be None. That is the point: an Evidence carrying None says "not
    known", which is a fact worth passing along, whereas a bare 0 would be a
    claim nobody made.
    """

    value: Any
    kind: str = OBSERVED_FACT
    source: str = SOURCE_ARCHIVE
    observed_at: str | None = None
    event_time: str | None = None
    freshness: str = UNKNOWN_FRESHNESS
    confidence: str = UNKNOWN_CONFIDENCE
    scope: str = ONE_OFF
    sample_size: int | None = None
    note: str | None = None

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def is_stale(self) -> bool:
        return self.freshness == STALE

    def may_state_as_fact(self) -> bool:
        """Only an observed fact that is actually known may be asserted flatly.

        A forecast never qualifies (§1: "cannot be stated as a fact"), and a
        derived pattern has to carry its sample size when it is used.
        """
        return self.known and self.kind == OBSERVED_FACT

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value, "kind": self.kind, "source": self.source,
            "observed_at": self.observed_at, "event_time": self.event_time,
            "freshness": self.freshness, "confidence": self.confidence,
            "scope": self.scope, "sample_size": self.sample_size,
            "note": self.note, "known": self.known,
        }

    def describe(self) -> str:
        """One human-readable line, for a card or a chat answer."""
        if not self.known:
            return self.note or "not known"
        parts = [str(self.value)]
        if self.kind == FORECAST:
            parts.append("(estimate)")
        if self.sample_size:
            parts.append(f"n={self.sample_size}")
        if self.freshness in (AGING, STALE):
            parts.append(f"observation {self.freshness}")
        if self.confidence in (LIMITED, UNKNOWN_CONFIDENCE):
            parts.append(f"{self.confidence} confidence")
        return " · ".join(parts)


def observed(value: Any, *, source: str = SOURCE_ARCHIVE, observed_at: Any = None,
             event_time: Any = None, scope: str = ONE_OFF,
             note: str | None = None, now: datetime | None = None) -> Evidence:
    """Something Starling actually saw."""
    stamp = observed_at if isinstance(observed_at, str) else (
        observed_at.isoformat() if isinstance(observed_at, datetime) else None)
    fresh = freshness_of(stamp, now=now)
    return Evidence(
        value=value, kind=OBSERVED_FACT, source=source, observed_at=stamp,
        event_time=event_time if isinstance(event_time, str) else None,
        freshness=fresh, scope=scope, note=note,
        # An observation is only as trustworthy as it is current: a stale one is
        # still a fact, but not a strong basis for a "right now" claim.
        confidence=(UNKNOWN_CONFIDENCE if value is None else
                    STRONG if fresh in (FRESH, AGING) else
                    LIMITED if fresh == STALE else DIRECTIONAL),
    )


def derived(value: Any, *, sample_size: int | None, scope: str = ACCOUNT_WIDE,
            source: str = SOURCE_ARCHIVE, note: str | None = None,
            confidence: str | None = None) -> Evidence:
    """A repeatable pattern computed from observed facts. Carries its n."""
    return Evidence(
        value=value, kind=DERIVED_PATTERN, source=source, scope=scope,
        sample_size=sample_size, note=note,
        confidence=confidence or confidence_for_samples(sample_size),
        freshness=UNKNOWN_FRESHNESS,
    )


def forecast(value: Any, *, confidence: str = LIMITED, scope: str = ONE_OFF,
             sample_size: int | None = None, note: str | None = None) -> Evidence:
    """A calibrated estimate. Never assertable as fact (§1)."""
    return Evidence(value=value, kind=FORECAST, source=SOURCE_DRAFT_OUTCOME,
                    confidence=confidence, scope=scope, sample_size=sample_size,
                    note=note, freshness=UNKNOWN_FRESHNESS)


def preference(value: Any, *, scope: str = ACCOUNT_WIDE,
               note: str | None = None) -> Evidence:
    """A direct user instruction, or one they confirmed. Overrides inference."""
    return Evidence(value=value, kind=USER_PREFERENCE, source=SOURCE_USER_CONFIRMED,
                    confidence=STRONG, scope=scope, note=note,
                    freshness=UNKNOWN_FRESHNESS)


def unknown(note: str, *, scope: str = ONE_OFF, kind: str = OBSERVED_FACT) -> Evidence:
    """An explicit 'we do not know', which is itself worth passing along."""
    return Evidence(value=None, kind=kind, scope=scope, note=note,
                    confidence=UNKNOWN_CONFIDENCE, freshness=UNKNOWN_FRESHNESS)


def weakest(*items: Evidence | None) -> str:
    """The confidence a conclusion drawn from several pieces may claim.

    A chain is as strong as its weakest link, so a strong baseline combined with
    a limited freshness read is limited — not strong.
    """
    order = [UNKNOWN_CONFIDENCE, LIMITED, DIRECTIONAL, STRONG]
    present = [item.confidence for item in items if item is not None]
    if not present:
        return UNKNOWN_CONFIDENCE
    return min(present, key=lambda level: order.index(level)
               if level in order else 0)


_CONFIDENCE_ORDER = [UNKNOWN_CONFIDENCE, LIMITED, DIRECTIONAL, STRONG]


def best_of(*levels: str | None) -> str:
    """The strongest of several INDEPENDENT lines of support.

    The complement of `weakest`. Use it when the inputs are separate reasons to
    be confident rather than links in one chain: a post's visible engagement and
    its remaining angle space each stand on their own, so one strong line means
    the conclusion is well-evidenced even while another is unknown. Collapsing
    those with `weakest` let a single unknown (no second metric snapshot yet)
    report "thin evidence" for a post with millions of visible views.
    """
    present = [level for level in levels if level in _CONFIDENCE_ORDER]
    if not present:
        return UNKNOWN_CONFIDENCE
    return max(present, key=_CONFIDENCE_ORDER.index)


def freshness_basis(observed_at: Any, *, what: str = "this",
                    now: datetime | None = None) -> str:
    """The user-facing freshness sentence the spec asks for in §3.1.

    "Live" must never mean pretending to know current X activity, so the answer
    always states when the observation happened — or that there isn't one.
    """
    seen = parse_time(observed_at)
    if seen is None:
        return f"I do not have a current observation, so I cannot confirm {what}."
    age = (now or _now()) - seen
    minutes = int(age.total_seconds() // 60)
    if minutes < 1:
        return f"Observed {what} moments ago."
    if minutes < 60:
        return f"Observed {what} {minutes} minute{'s' if minutes != 1 else ''} ago."
    hours = minutes // 60
    if hours < 24:
        return f"Observed {what} {hours} hour{'s' if hours != 1 else ''} ago."
    days = hours // 24
    return f"Observed {what} {days} day{'s' if days != 1 else ''} ago."
