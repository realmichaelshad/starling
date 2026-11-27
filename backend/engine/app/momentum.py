"""Real post momentum from the snapshot series (spec §5.2).

The database has stored a full `metric_snapshots` series since migration 2, but
nothing ever differenced two rows: every "momentum" read in the product was a
LIFETIME AVERAGE (`current / age`), honestly labelled but unable to tell a post
that is accelerating from one that got all its views in the first ten minutes.

    metric velocity     = (current - prior) / elapsed minutes
    metric acceleration = current velocity - prior velocity

The spec is strict about what may be claimed from how much data:

- one snapshot  -> an average, and "this is taking off" is forbidden (§5.2);
- two snapshots -> a real velocity;
- three         -> acceleration, which is what "accelerating" actually means.

Everything returns Evidence, so a caller cannot read a missing metric as zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import evidence as ev
from .storage import list_metric_snapshots

# Two observations closer together than this are treated as one: X's counters
# update in bursts, so differencing them measures rounding, not movement.
MIN_GAP_MINUTES = 2.0

# The metrics worth tracking movement on, in the order they are preferred when
# a caller does not name one. Views first because it is the denominator the
# spec's "early qualified response" ratio needs; replies/reposts next because
# they are the meaningful actions.
DEFAULT_METRICS = ("views", "impressions", "likes", "replies", "reposts", "quotes")


def _minutes_between(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 60.0


def _metric_value(snapshot: dict, metric: str) -> float | None:
    metrics = snapshot.get("metrics") or {}
    value = metrics.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass
class Momentum:
    """What the snapshot series supports saying about one post's movement."""

    post_id: str
    metric: str
    snapshots: int = 0
    latest_value: float | None = None
    latest_at: str | None = None
    # Per minute. None when fewer than two usable snapshots exist.
    velocity: float | None = None
    # Change in velocity between the two most recent intervals. Needs three.
    acceleration: float | None = None
    # current / age. Always available with one snapshot, and never evidence of
    # acceleration — kept separate from `velocity` on purpose.
    lifetime_average: float | None = None
    basis: str = "none"          # none | average | velocity | acceleration
    trend: str = "unknown"       # unknown | accelerating | steady | slowing
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id, "metric": self.metric,
            "snapshots": self.snapshots, "latest_value": self.latest_value,
            "latest_at": self.latest_at, "velocity": self.velocity,
            "acceleration": self.acceleration,
            "lifetime_average": self.lifetime_average,
            "basis": self.basis, "trend": self.trend, "note": self.note,
        }

    def may_claim_takeoff(self) -> bool:
        """§5.2/§1.5: a single snapshot must never produce 'this is taking off'.

        Takeoff needs at least two snapshots AND rising movement — a large raw
        count proves nothing about whether the post is still moving.
        """
        return self.snapshots >= 2 and self.trend == "accelerating"

    def as_evidence(self) -> ev.Evidence:
        if self.basis == "none":
            return ev.unknown(f"No metric snapshots for {self.post_id}.")
        if self.basis == "average":
            return ev.observed(
                self.lifetime_average, observed_at=self.latest_at,
                source=ev.SOURCE_VISIBLE_X, scope=ev.ONE_OFF,
                note="Lifetime average from a single snapshot — not proof of movement.")
        return ev.derived(
            self.velocity, sample_size=self.snapshots, scope=ev.ONE_OFF,
            source=ev.SOURCE_VISIBLE_X,
            note=f"{self.metric}/min between {self.snapshots} observations.")


def _usable_series(post_id: str, metric: str, limit: int = 40) -> list[tuple[datetime, float]]:
    """Oldest-first (time, value) pairs, deduped by the minimum gap.

    Snapshots come from several sources (extension capture, manual sync), so the
    same instant can appear more than once; a pair separated by less than
    MIN_GAP_MINUTES is collapsed to the later reading.
    """
    rows = list_metric_snapshots(post_id, limit=limit)
    points: list[tuple[datetime, float]] = []
    for row in rows:
        when = ev.parse_time(row.get("captured_at"))
        value = _metric_value(row, metric)
        if when is None or value is None:
            continue
        points.append((when, value))
    points.sort(key=lambda item: item[0])
    collapsed: list[tuple[datetime, float]] = []
    for when, value in points:
        if collapsed and _minutes_between(when, collapsed[-1][0]) < MIN_GAP_MINUTES:
            collapsed[-1] = (when, value)
            continue
        collapsed.append((when, value))
    return collapsed


def measure(post_id: str, metric: str = "views", *,
            posted_at: Any = None, now: datetime | None = None) -> Momentum:
    """Momentum for one post and metric, claiming only what the data supports."""
    series = _usable_series(post_id, metric)
    result = Momentum(post_id=post_id, metric=metric, snapshots=len(series))
    if not series:
        result.note = "No usable snapshots for this metric."
        return result

    latest_at, latest_value = series[-1]
    result.latest_at = latest_at.isoformat()
    result.latest_value = latest_value

    started = ev.parse_time(posted_at)
    if started is not None:
        age = _minutes_between(latest_at, started)
        if age > 0:
            result.lifetime_average = latest_value / age

    if len(series) == 1:
        result.basis = "average" if result.lifetime_average is not None else "none"
        result.note = ("One snapshot only: this is a lifetime average, not "
                       "evidence that the post is accelerating.")
        return result

    prev_at, prev_value = series[-2]
    gap = _minutes_between(latest_at, prev_at)
    if gap <= 0:
        result.basis = "average"
        result.note = "Snapshots share a timestamp, so no rate can be derived."
        return result
    result.velocity = (latest_value - prev_value) / gap
    result.basis = "velocity"
    result.trend = "steady"
    result.note = f"Velocity from {len(series)} observations."

    if len(series) >= 3:
        older_at, older_value = series[-3]
        prior_gap = _minutes_between(prev_at, older_at)
        if prior_gap > 0:
            prior_velocity = (prev_value - older_value) / prior_gap
            result.acceleration = result.velocity - prior_velocity
            result.basis = "acceleration"
            # A 10% band around zero: counters are integers reported in bursts,
            # so tiny differences are noise rather than a change in direction.
            threshold = max(abs(prior_velocity) * 0.10, 1e-9)
            if result.acceleration > threshold:
                result.trend = "accelerating"
            elif result.acceleration < -threshold:
                result.trend = "slowing"
            else:
                result.trend = "steady"
    return result


def best_available(post_id: str, *, posted_at: Any = None,
                   metrics: tuple[str, ...] = DEFAULT_METRICS) -> Momentum:
    """Momentum on the first metric this post actually has a series for."""
    best: Momentum | None = None
    for metric in metrics:
        found = measure(post_id, metric, posted_at=posted_at)
        if found.basis == "acceleration":
            return found
        if best is None or found.snapshots > best.snapshots:
            best = found
    return best or Momentum(post_id=post_id, metric=metrics[0])


def early_qualified_response(post_id: str, *,
                             actions: tuple[str, ...] = ("replies", "reposts", "quotes"),
                             denominator: str = "views") -> ev.Evidence:
    """§1.5's early qualified response: meaningful actions over observed views.

    Deliberately Evidence rather than a float. Where only view counts are
    visible Starling cannot see the true impression denominator, so the spec
    requires the result to be labelled lower-confidence — that caveat travels
    with the number instead of being lost at the call site.
    """
    rows = list_metric_snapshots(post_id, limit=40)
    if not rows:
        return ev.unknown(f"No snapshots for {post_id}.")
    latest = max(rows, key=lambda row: str(row.get("captured_at") or ""))
    views = _metric_value(latest, denominator)
    if not views:
        return ev.unknown(
            "No observed view count, so an early-response rate cannot be computed.")
    total = 0.0
    seen_any = False
    for action in actions:
        value = _metric_value(latest, action)
        if value is not None:
            total += value
            seen_any = True
    if not seen_any:
        return ev.unknown("No meaningful-action counts are visible on this post.")
    return ev.observed(
        round(total / views, 5), source=ev.SOURCE_VISIBLE_X,
        observed_at=latest.get("captured_at"), scope=ev.ONE_OFF,
        note=("Actions per observed view. Lower confidence: the true impression "
              "denominator and per-viewer ranking are not visible to Starling."),
    )
