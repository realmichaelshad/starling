"""Bounded recency weighting for the account-learning paths.

The precedent is `performance.recency_weights` (half-life 90d, floor 0.25,
"for local model fitting, never for scoring") — kept numerically identical
here so the two half-life laws cannot silently diverge. This module exists
because performance.py imports account_model, and the voice/topic learners
need the same weights without creating a cycle; it stays a leaf (stdlib
only) on purpose.

Voice learning additionally discounts posts Starling itself wrote
(origin='starling_verified') to half weight: his hand-typed posts are the
strongest signal of where the account is going, and drafts must never learn
primarily from earlier drafts. Every consumer normalizes by the weight sum,
so an all-Starling corpus measures identically to an unmarked one — the
factor only matters in mixed corpora, which is exactly the drift case.

The floor matters as much as the decay: 0.25 means the oldest measured
voice never fully vanishes, so a handful of unusual recent posts cannot
erase the archive's medians — the account follows its author, it does not
whipsaw.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DEFAULT_HALF_LIFE_DAYS = 90.0
# Used only while the approach detector has CONFIRMED a topical shift; the
# account asked us to hop onto its new direction, so recent posts speak
# three times louder than the default law.
FAST_HALF_LIFE_DAYS = 30.0
WEIGHT_FLOOR = 0.25
STARLING_VERIFIED_VOICE_FACTOR = 0.5


def parse_when(value: Any) -> datetime | None:
    """ISO string or datetime → aware UTC datetime, else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def row_when(row: dict[str, Any]) -> datetime | None:
    """When a post happened: created_at, else when the watcher first saw it."""
    return parse_when(row.get("created_at") or row.get("first_observed_at"))


def recency_weights(rows: list[dict[str, Any]], *,
                    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
                    floor: float = WEIGHT_FLOOR) -> list[float]:
    """One weight per row against the NEWEST dated row.

    No dated rows at all → every weight 1.0, and an undated row inside a
    dated set also weighs 1.0 (the `date or newest` convention from
    performance.recency_weights). Both make "weighted equals unweighted on
    an undated archive" true by construction rather than by test.
    """
    dates = [row_when(row) for row in rows]
    newest = max((date for date in dates if date), default=None)
    if newest is None:
        return [1.0] * len(rows)
    weights: list[float] = []
    for date in dates:
        age_days = max(0.0, (newest - (date or newest)).total_seconds() / 86400.0)
        weights.append(max(floor, 0.5 ** (age_days / half_life_days)))
    return weights


def voice_weights(rows: list[dict[str, Any]], *,
                  half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> list[float]:
    """Style/voice weights: recency, with Starling-written posts at half."""
    base = recency_weights(rows, half_life_days=half_life_days)
    return [weight * (STARLING_VERIFIED_VOICE_FACTOR
                      if str(row.get("origin") or "") == "starling_verified" else 1.0)
            for row, weight in zip(rows, base)]


def topic_weights(rows: list[dict[str, Any]], *,
                  half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> list[float]:
    """Topic/cadence weights: recency only — a published post is account
    content at full weight whoever drafted it."""
    return recency_weights(rows, half_life_days=half_life_days)
