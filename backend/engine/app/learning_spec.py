"""Learning-loop additions the spec requires (§9.2, §9.2a, §9.2b, §9.3).

The existing loop already does the hard part well — shrinkage toward a prior,
recency decay, "one viral outlier cannot take over", isotonic calibration. Three
things were missing.

1. **A checkpoint series** (§9.2). Maturity was one 24h reading, so nothing
   could compare the first hour with the first day, and the spec's 7-day
   horizon did not exist.
2. **The pre-post action hypothesis** (§9.2a). Nothing recorded what the post
   was *expected* to earn, which makes actual-vs-hypothesis comparison
   impossible — you cannot learn from a prediction you never wrote down.
3. **A one-variable experiment taxonomy** (§9.2a). Changing topic, format, tone
   and timing at once means no outcome can explain what worked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import evidence as ev
from . import momentum as mom

# §9.2 checkpoints. Early ones inform live conversation decisions; the mature
# ones inform strategy. 7d is included because a post can still be found.
CHECKPOINTS: tuple[tuple[str, float], ...] = (
    ("15m", 0.25), ("1h", 1.0), ("6h", 6.0), ("24h", 24.0), ("7d", 168.0),
)

# Below this, a checkpoint reading informs conversation only — never strategy.
MATURE_FROM_HOURS = 24.0

# §9.2a: exactly one of these may change per experiment.
EXPERIMENT_VARIABLES: tuple[str, ...] = (
    "archetype", "format", "primary_objective", "audience_route", "creative_form",
)


@dataclass
class ActionHypothesis:
    """What the post was expected to earn, written down BEFORE it went out."""

    primary_action: str
    secondary_action: str | None = None
    core_audience: str | None = None
    adjacent_audience: str | None = None
    archetype: str | None = None
    failure_mode: str | None = None
    evidence_quality: str = ev.LIMITED
    recorded_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_action": self.primary_action,
            "secondary_action": self.secondary_action,
            "core_audience": self.core_audience,
            "adjacent_audience": self.adjacent_audience,
            "archetype": self.archetype,
            "failure_mode": self.failure_mode,
            "evidence_quality": self.evidence_quality,
            "recorded_at": self.recorded_at or ev._now().isoformat(),
        }


def hypothesis_from(decision, analysis) -> ActionHypothesis:
    """Capture the forecast at decision time, not after the fact."""
    forecast = getattr(analysis, "audience_forecast", {}) or {}
    angles = getattr(analysis, "angles", []) or []
    return ActionHypothesis(
        primary_action=forecast.get("primary_action") or "favorite",
        secondary_action=forecast.get("secondary_action"),
        core_audience=forecast.get("core"),
        adjacent_audience=forecast.get("adjacent"),
        archetype=(angles[0].get("kind") if angles else None),
        failure_mode=forecast.get("failure_mode"),
        evidence_quality=forecast.get("evidence_quality", ev.LIMITED),
    )


def checkpoint_for(age_hours: float) -> str | None:
    """Which checkpoint an observation belongs to, if any."""
    best: str | None = None
    for label, hours in CHECKPOINTS:
        if age_hours >= hours:
            best = label
    return best


def checkpoint_series(post_id: str, *, posted_at: Any,
                      metric: str = "views", now: datetime | None = None) -> dict[str, Any]:
    """The metric at each checkpoint the snapshots actually reach.

    A checkpoint with no observation is reported as unknown rather than
    interpolated: the spec's whole learning discipline rests on not inventing
    the readings that were never taken.
    """
    from .storage import list_metric_snapshots

    started = ev.parse_time(posted_at)
    current = now or ev._now()
    rows = list_metric_snapshots(post_id, limit=200)
    readings: dict[str, Any] = {}
    for label, hours in CHECKPOINTS:
        if started is None:
            readings[label] = {"known": False, "reason": "post time unknown"}
            continue
        target = started + timedelta(hours=hours)
        # A checkpoint whose moment has not arrived is NOT known — carrying an
        # earlier reading forward would make a two-hour-old post look mature at
        # 24h and let the strategy loop learn from a number nobody measured.
        if target > current:
            readings[label] = {"known": False, "reason": "not reached yet",
                               "mature": hours >= MATURE_FROM_HOURS}
            continue
        # The last observation at or before the checkpoint.
        best = None
        for row in rows:
            when = ev.parse_time(row.get("captured_at"))
            if when is None or when > target:
                continue
            if best is None or when > best[0]:
                best = (when, row)
        if best is None:
            readings[label] = {"known": False, "reason": "no observation by then"}
            continue
        value = (best[1].get("metrics") or {}).get(metric)
        readings[label] = {
            "known": value is not None,
            "value": value,
            "observed_at": best[1].get("captured_at"),
            "mature": hours >= MATURE_FROM_HOURS,
        }
    known = [label for label, item in readings.items() if item.get("known")]
    return {
        "post_id": post_id, "metric": metric, "checkpoints": readings,
        "known_checkpoints": known,
        "mature": any(readings[label].get("known") and readings[label].get("mature")
                      for label in readings),
        "note": ("Missing checkpoints are reported as unknown, never "
                 "interpolated."),
    }


def compare_to_hypothesis(hypothesis: ActionHypothesis, post: dict[str, Any]
                          ) -> dict[str, Any]:
    """§9.2a: did the actions we predicted actually happen?

    Records BOTH directions — a high-view post with weak profile conversion is
    a distribution win and a growth miss, and flattening that into one verdict
    is how an account learns the wrong lesson.
    """
    metrics = post.get("metrics") or {}
    observed = {
        "favorite": metrics.get("likes"),
        "reply": metrics.get("replies"),
        "share": metrics.get("reposts"),
        "quote": metrics.get("quotes"),
        "profile_click": metrics.get("profile_clicks"),
        "follow": metrics.get("follows"),
    }
    predicted = hypothesis.primary_action
    value = observed.get(predicted)
    others = {name: count for name, count in observed.items()
              if name != predicted and count}

    if value is None:
        verdict = "unmeasurable"
        note = f"The predicted action ({predicted}) is not visible on this post."
    elif not value and others:
        verdict = "wrong_action"
        note = (f"Predicted {predicted}, but the response was "
                f"{max(others, key=others.get)}.")
    elif value:
        verdict = "as_predicted"
        note = f"{predicted} occurred as expected."
    else:
        verdict = "no_response"
        note = "Neither the predicted action nor another one occurred."

    return {
        "hypothesis": hypothesis.as_dict(),
        "observed": observed,
        "verdict": verdict,
        "note": note,
        # §9.2a: never a single verdict — the spec wants both wins and misses.
        "distribution_signal": observed.get("share") or 0,
        "conversion_signal": observed.get("follow") or observed.get("profile_click") or 0,
        "evidence": ev.observed(
            verdict, source=ev.SOURCE_DRAFT_OUTCOME,
            observed_at=post.get("metrics_updated_at"),
            note="One outcome is not a rule (§9.2).").as_dict(),
    }


def experiment_is_clean(previous: dict[str, Any], current: dict[str, Any]
                        ) -> dict[str, Any]:
    """§9.2a: exactly one variable may change, or nothing is learnable."""
    changed = [name for name in EXPERIMENT_VARIABLES
               if previous.get(name) != current.get(name)]
    return {
        "clean": len(changed) == 1,
        "changed": changed,
        "reason": ("One variable changed, so the outcome is attributable."
                   if len(changed) == 1 else
                   "Nothing changed, so there is nothing to learn."
                   if not changed else
                   f"{len(changed)} variables changed at once "
                   f"({', '.join(changed)}), so no outcome can explain what worked."),
    }


# §9.3 — what may update silently, and what must be confirmed.
AUTO_UPDATE: tuple[str, ...] = (
    "metric_snapshots", "post_counts", "normalized_percentiles",
    "relationship_recency", "calibrated_confidence",
)
NEEDS_CONFIRMATION: tuple[str, ...] = (
    "permanent_voice_rules", "content_pillars", "audience_definitions",
    "hard_avoidances", "deletion_reason", "subjective_success",
    "privacy_constraints",
)


def update_policy(field_name: str) -> dict[str, Any]:
    """Whether a learned change may be applied without asking (§9.3)."""
    if field_name in AUTO_UPDATE:
        return {"auto": True, "reason": "Measured and order-preserving."}
    if field_name in NEEDS_CONFIRMATION:
        return {"auto": False,
                "reason": "Changes voice or strategy durably; must be confirmed."}
    return {"auto": False,
            "reason": "Unknown field: default to asking rather than assuming."}
