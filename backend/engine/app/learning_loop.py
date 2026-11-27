"""The closed feedback loop: posted outcomes → bandit rewards, scorer
calibration, and voice-pool growth.

Runs after the engine starts and debounced after snapshot ingest. Every learner
respects the honesty gates: rewards are percentiles against the account's own
same-format baseline; isotonic calibration fits only at ≥25 matured outcomes;
one post can never dominate (recency-weighted, shrunk posteriors downstream).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .bandit import classify_angle, record_observation
from .performance import normalized_objective
from .storage import (
    connect,
    get_job_state,
    get_post,
    list_metric_snapshots,
    records,
    set_job_state,
)

MATURITY_HOURS = 24.0
ISOTONIC_MIN_OUTCOMES = 25


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _format_baseline(all_rows: list[dict], post_type: str) -> list[float]:
    values = []
    for row in all_rows:
        if (row.get("post_type") or "post") != post_type:
            continue
        value = normalized_objective(row, "reach")
        if value is not None:
            values.append(float(value))
    return sorted(values)


def _percentile_rank(values: list[float], target: float) -> float:
    if not values:
        return 0.5
    below = sum(1 for value in values if value < target)
    return max(0.0, min(1.0, below / len(values)))


def _reward_for_post(post: dict, baselines: dict[str, list[float]], all_rows: list[dict]) -> tuple[float, dict]:
    post_type = str(post.get("post_type") or "post")
    if post_type not in ("reply", "quote", "post"):
        post_type = "post"
    reach_value = normalized_objective(post, "reach")
    conversation_value = normalized_objective(post, "conversation")
    baseline = baselines.setdefault(post_type, _format_baseline(all_rows, post_type))
    reach_pct = _percentile_rank(baseline, float(reach_value)) if reach_value is not None else 0.5
    conversation_pct = 0.5
    if conversation_value is not None:
        conversation_baseline = [float(v) for v in (normalized_objective(row, "conversation")
                                                    for row in all_rows
                                                    if (row.get("post_type") or "post") == post_type) if v is not None]
        conversation_pct = _percentile_rank(sorted(conversation_baseline), float(conversation_value))
    # 80/20 reach/conversation per the plan; real follows would add a bounded
    # bonus, but we never fabricate per-post follower attribution.
    reward = 0.8 * reach_pct + 0.2 * conversation_pct
    return reward, {"reach_pct": round(reach_pct, 3), "conversation_pct": round(conversation_pct, 3),
                    "format": post_type}


def _posted_generations(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT id, action, final_text, published_post_id, posted_at, request_json
        FROM generations
        WHERE lifecycle_status='posted' AND published_post_id IS NOT NULL
        ORDER BY posted_at DESC LIMIT 200
    """).fetchall()
    return [dict(row) for row in rows]


def recompute_learning() -> dict[str, Any]:
    """One reconciliation pass; idempotent via the processed-set in job_state."""
    state = get_job_state("learning_loop")
    processed: set[str] = set(state.get("processed", []))
    now = _utc_now()
    # A post he deleted must not set the bar the next post is judged against —
    # rewards here are percentiles against the account's own history.
    all_rows = [row for row in records() if not row.get("deleted_at")]
    baselines: dict[str, list[float]] = {}
    finalized = 0
    with connect() as conn:
        generations = _posted_generations(conn)
    for generation in generations:
        identifier = str(generation["id"])
        if identifier in processed:
            continue
        posted_at = _parse(generation.get("posted_at"))
        if posted_at is None or (now - posted_at).total_seconds() < MATURITY_HOURS * 3600.0:
            continue  # wait for the 24h checkpoint to mature
        post = get_post(str(generation["published_post_id"]))
        if not post or not post.get("metrics"):
            continue  # no visible metrics captured yet
        reward, parts = _reward_for_post(post, baselines, all_rows)
        action = str(generation.get("action") or "post")
        angle = _generation_angle(conn_row=generation) or classify_angle(str(generation.get("final_text") or ""))
        if angle:
            record_observation(action if action in ("reply", "quote", "post") else "post", angle, reward,
                               post_id=str(generation["published_post_id"]), generation_id=identifier,
                               parts=parts, observed_at=posted_at.isoformat())
        _fill_calibration_outcome(identifier, reward)
        processed.add(identifier)
        finalized += 1
    _maybe_fit_isotonic()
    set_job_state("learning_loop", {"processed": list(processed)[-500:],
                                    "last_run": now.isoformat(), "finalized_total": int(state.get("finalized_total", 0)) + finalized})
    return {"finalized": finalized, "processed_total": len(processed)}


def _generation_angle(conn_row: dict) -> str | None:
    try:
        request = json.loads(conn_row.get("request_json") or "{}")
    except (TypeError, ValueError):
        return None
    # The selected candidate's angle is stored on the candidate row.
    with connect() as conn:
        row = conn.execute(
            "SELECT candidate_json FROM generation_candidates WHERE generation_id=? ORDER BY rank_index LIMIT 1",
            (str(conn_row.get("id")),)).fetchone()
    if row:
        try:
            candidate = json.loads(row[0] or "{}")
            if candidate.get("angle_id"):
                return str(candidate["angle_id"])
        except (TypeError, ValueError):
            pass
    del request
    return None


def _fill_calibration_outcome(generation_id: str, reward: float) -> None:
    now = _utc_now().isoformat()
    with connect() as conn:
        conn.execute(
            "UPDATE scorer_calibration SET posted=1, outcome_pct=?, outcome_at=? WHERE generation_id=?",
            (float(reward), now, generation_id))


def _maybe_fit_isotonic() -> None:
    """Fit score → P(above-median outcome) once ≥25 matured outcomes exist.

    Isotonic regression is monotone, so applying it re-scales confidence
    without re-ordering candidates — safe to auto-apply. Stored as breakpoint
    lists in job_state (no pickled models).
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT score, outcome_pct FROM scorer_calibration WHERE posted=1 AND outcome_pct IS NOT NULL").fetchall()
    if len(rows) < ISOTONIC_MIN_OUTCOMES:
        return
    pairs = sorted((float(row[0]), float(row[1])) for row in rows)
    scores = [pair[0] for pair in pairs]
    labels = [1.0 if pair[1] >= 0.5 else 0.0 for pair in pairs]
    try:
        from sklearn.isotonic import IsotonicRegression
        model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        fitted = model.fit_transform(scores, labels)
        # Deduplicate breakpoints for compact storage.
        xs: list[float] = []
        ys: list[float] = []
        for score, probability in zip(scores, fitted):
            if not xs or not math.isclose(score, xs[-1]):
                xs.append(round(score, 2))
                ys.append(round(float(probability), 4))
        set_job_state("scorer_isotonic", {"x": xs, "y": ys, "n": len(pairs),
                                          "fitted_at": _utc_now().isoformat()})
    except Exception:
        return


# --- Post-launch momentum ---------------------------------------------------
# The ranker compounds the FIRST hour's engagement rate, but the learning loop
# above deliberately waits 24h before drawing conclusions. This checkpoint
# fills the gap: it compares a fresh post's average velocity against the
# account's own history and turns it into advice — never into any automatic
# action.

EARLY_WINDOW_HOURS = 2.0
TAKING_OFF_RATIO = 3.0
QUIET_RATIO = 0.3


def account_velocity_baseline(post_type: str = "post") -> dict[str, Any]:
    """Median views-per-hour and replies-per-hour across the account's own
    measured posts of one format (velocity averaged over each post's captured
    exposure, capped at 48h — the honest proxy snapshots allow)."""
    views_rates: list[float] = []
    reply_rates: list[float] = []
    for row in records():
        if row.get("deleted_at"):
            continue    # a deleted post is not this account's normal pace
        if (str(row.get("post_type") or "post")) != post_type:
            continue
        metrics = row.get("metrics") or {}
        created = _parse(row.get("created_at"))
        captured = _parse(row.get("metrics_updated_at"))
        if not metrics or created is None or captured is None or captured <= created:
            continue
        hours = min(48.0, max(0.25, (captured - created).total_seconds() / 3600.0))
        if metrics.get("views") is not None:
            views_rates.append(float(metrics.get("views") or 0) / hours)
        if metrics.get("replies") is not None:
            reply_rates.append(float(metrics.get("replies") or 0) / hours)
    views_rates.sort()
    reply_rates.sort()
    return {
        "views_per_hour_median": views_rates[len(views_rates) // 2] if views_rates else None,
        "replies_per_hour_median": reply_rates[len(reply_rates) // 2] if reply_rates else None,
        "sample": len(views_rates),
    }


def momentum_verdict(views_per_hour: float, replies_per_hour: float,
                     baseline: dict[str, Any],
                     *, views: float | None = None) -> dict[str, Any] | None:
    """Compare a fresh post's average velocity against the account baseline.

    A RATE NEEDS A DENOMINATOR, and this one had none. The existing floor is on
    the BASELINE's sample size, not on the post's own views — so a post with 1
    view at 24 seconds old divides to a huge views-per-hour and was told
    "answer the best replies now", with zero replies to answer. Replayed over
    1,044 real snapshots: 217 firings, 84 of them under 30 views.

    `_RATE_MIN_VIEWS` is imported, never copied — it is the same floor
    `account_facts` uses to withhold an engagement percentage, and for the same
    reason: below it, one interaction moves the number more than the post does.

    Keyword-only and defaulted so the guard is opt-in per call site rather than
    silently skipped by a positional mismatch; both callers pass it.
    """
    from .account_facts import _RATE_MIN_VIEWS
    if views is not None and views < _RATE_MIN_VIEWS:
        return None
    median_views = baseline.get("views_per_hour_median")
    if baseline.get("sample", 0) < 8 or not median_views:
        return None
    ratio = views_per_hour / max(1e-6, float(median_views))
    if ratio >= TAKING_OFF_RATIO:
        status, advice = "taking_off", (
            f"Views are running ~{ratio:.0f}x your median pace. Answering the best replies now compounds it — "
            "author-engaged replies are the heaviest positive ranking signal.")
    elif ratio <= QUIET_RATIO:
        status, advice = "quiet", (
            "Early velocity is below your median pace. That is information, not failure — "
            "note the hook and timing, and let the 24h numbers settle before concluding anything.")
    else:
        status, advice = "normal", "Early velocity is in your normal range. Engage with substantive replies as they come."
    median_replies = baseline.get("replies_per_hour_median")
    reply_note = None
    if median_replies and replies_per_hour >= float(median_replies) * TAKING_OFF_RATIO:
        reply_note = f"Replies are ~{replies_per_hour / max(1e-6, float(median_replies)):.0f}x your median pace."
    return {"status": status, "advice": advice, "velocity_ratio": round(ratio, 2),
            "reply_note": reply_note,
            "baseline": {"views_per_hour_median": round(float(median_views), 1), "sample": baseline["sample"]},
            "note": "Average velocity from visible snapshots against your own history — a correlation, not a promise."}


def early_checkpoint(now: datetime | None = None) -> dict[str, Any]:
    """Momentum reads for generations posted within the early window."""
    now = now or _utc_now()
    with connect() as conn:
        generations = _posted_generations(conn)
    baseline = account_velocity_baseline("post")
    checks: list[dict[str, Any]] = []
    for generation in generations:
        posted_at = _parse(generation.get("posted_at"))
        if posted_at is None:
            continue
        age_hours = (now - posted_at).total_seconds() / 3600.0
        if age_hours > EARLY_WINDOW_HOURS or age_hours < 0:
            continue
        post = get_post(str(generation["published_post_id"]))
        metrics = (post or {}).get("metrics") or {}
        # A METRICS DICT IS NOT A VIEW COUNT.
        #
        # This guarded only on the dict being empty, so a post carrying likes but
        # no view count reached `momentum_verdict` as ZERO views per hour — and a
        # zero velocity against a real baseline reads as a post that is dying,
        # about a post nobody has measured yet. Measured on his archive: 3 of the
        # 53 post rows with a metrics dict have no usable view count, so this is
        # reachable, and it produces a verdict on a live post from a number that
        # was never observed.
        #
        # The baseline builder 60 lines above already does this correctly
        # ("if metrics.get('views') is not None"); this call site did not. Views
        # decide the verdict, so views are what must be present — replies stay
        # coerced, where absent genuinely does mean none.
        if metrics.get("views") is None:
            checks.append({"post_id": generation["published_post_id"], "status": "no_metrics_yet",
                           "age_minutes": round(age_hours * 60.0, 1)})
            continue
        hours = max(0.25, age_hours)
        verdict = momentum_verdict(float(metrics.get("views") or 0) / hours,
                                   float(metrics.get("replies") or 0) / hours, baseline,
                                   views=float(metrics.get("views") or 0))
        checks.append({
            "post_id": generation["published_post_id"],
            "age_minutes": round(age_hours * 60.0, 1),
            **(verdict or {"status": "insufficient_baseline",
                           "advice": "Not enough measured history to judge early velocity yet."}),
        })
    return {"checks": checks, "baseline": baseline, "generated_at": now.isoformat(),
            "window_hours": EARLY_WINDOW_HOURS}


def learning_status() -> dict[str, Any]:
    state = get_job_state("learning_loop")
    isotonic = get_job_state("scorer_isotonic")
    return {
        "last_run": state.get("last_run"),
        "finalized_total": state.get("finalized_total", 0),
        "calibration_fitted": bool(isotonic.get("x")),
        "calibration_samples": isotonic.get("n", 0),
    }
