from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from typing import TYPE_CHECKING

if TYPE_CHECKING:                     # sklearn is imported where the models are fitted (cold start)
    from sklearn.preprocessing import StandardScaler

from .account_model import growth_diagnoses, infer_account_model
from .storage import get_post, performance_records

OBJECTIVES = ("reach", "conversation", "resonance")

# "reply" + "s" is "replys", and it shipped. Driving "when should i post" on
# 2026-08-07 returned: "At least 30 dated replys with this metric are needed
# before recommending a window." The naive plural is only wrong for one of the
# four post types, which is why it survived — "posts", "quotes" and "threads"
# all read fine and nothing pointed at the one that does not.
_IRREGULAR_PLURALS = {"reply": "replies"}


def plural_post_type(post_type: str | None) -> str:
    """The plural of a post_type, for a sentence the user reads (also `intelligence`'s
    "You don't have enough replies yet ..."): one rule, not two copies."""
    if not post_type:
        return "posts"
    return _IRREGULAR_PLURALS.get(str(post_type), f"{post_type}s")


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def raw_objective(metrics: dict[str, Any], objective: str) -> float | None:
    """Return None when the export did not provide any metric for an objective."""
    if not metrics:
        return None
    if objective == "reach":
        if not any(key in metrics for key in ("impressions", "views", "reposts")):
            return None
        return float(metrics.get("impressions", metrics.get("views", 0)) or 0) + float(metrics.get("reposts", 0) or 0) * 30.0
    if objective == "conversation":
        if not any(key in metrics for key in ("replies", "quotes")):
            return None
        return float(metrics.get("replies", 0) or 0) + float(metrics.get("quotes", 0) or 0) * 1.4
    if objective == "resonance":
        if not any(key in metrics for key in ("likes", "bookmarks")):
            return None
        return float(metrics.get("likes", 0) or 0) + float(metrics.get("bookmarks", 0) or 0) * 2.0
    if objective == "followers":
        return float(metrics["follows"]) if "follows" in metrics else None
    if objective == "profile_visits":
        return float(metrics["profile_clicks"]) if "profile_clicks" in metrics else None
    return None


def normalized_objective(row: dict, objective: str) -> float | None:
    raw = raw_objective(row.get("metrics", {}), objective)
    if raw is None:
        return None
    follower_scale = max(1.0, float(row.get("followers_count") or 1000) / 1000.0)
    created = parse_datetime(row.get("created_at"))
    captured = parse_datetime(row.get("metrics_updated_at"))
    exposure_scale = 1.0
    if created and captured and captured > created:
        days = min(30.0, max(0.25, (captured - created).total_seconds() / 86400.0))
        exposure_scale = math.sqrt(days)
    return math.log1p(max(0.0, raw) / follower_scale / exposure_scale)


def recency_weights(rows: list[dict], half_life_days: float = 90.0, floor: float = 0.25) -> np.ndarray:
    """Bounded recency weighting for local model fitting, never for scoring."""
    dates = [parse_datetime(row.get("created_at")) for row in rows]
    newest = max((date for date in dates if date), default=None)
    if not newest:
        return np.ones(len(rows))
    return np.asarray([
        max(floor, 0.5 ** (max(0.0, (newest - (date or newest)).total_seconds() / 86400.0) / half_life_days))
        for date in dates
    ])


def model_features(text: str, created_at: str | datetime | None, media_count: int, post_type: str = "post") -> list[float]:
    created = parse_datetime(created_at) or datetime.now(timezone.utc)
    hour_angle = 2 * math.pi * created.hour / 24
    day_angle = 2 * math.pi * created.weekday() / 7
    lowered = text.lower()
    topic_bins = [0.0] * 8
    for token in re.findall(r"[a-z0-9_]{3,}", lowered)[:80]:
        bucket = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=2).digest(), "big") % len(topic_bins)
        topic_bins[bucket] += 1.0
    topic_scale = max(1.0, sum(topic_bins))
    topic_bins = [value / topic_scale for value in topic_bins]
    return [
        min(len(text), 1000) / 1000.0,
        min(len(text.split()), 200) / 200.0,
        min(text.count("?"), 4) / 4.0,
        min(text.count("!"), 4) / 4.0,
        min(len(re.findall(r"#[\w]+", text)), 5) / 5.0,
        min(len(re.findall(r"@[\w]+", text)), 5) / 5.0,
        min(sum(ord(char) > 10000 for char in text), 12) / 12.0,
        1.0 if "\n" in text else 0.0,
        min(media_count, 4) / 4.0,
        math.sin(hour_angle), math.cos(hour_angle), math.sin(day_angle), math.cos(day_angle),
        1.0 if post_type == "reply" else 0.0,
        1.0 if post_type == "quote" else 0.0,
        1.0 if post_type == "thread" else 0.0,
        1.0 if any(token in lowered for token in ("why", "how", "what do you think")) else 0.0,
        *topic_bins,
    ]


@dataclass
class ObjectiveFit:
    model: Any
    scaler: StandardScaler | None
    model_type: str
    targets: np.ndarray
    sample_size: int
    holdout_size: int
    holdout_mae: float | None
    baseline_mae: float | None
    skill: float | None
    residual_std: float


class ChronologicalPerformanceModel:
    def __init__(self, rows: list[dict] | None = None, action_type: str | None = None) -> None:
        source_rows = rows if rows is not None else performance_records()
        self.action_type = action_type
        def comparable(row: dict) -> bool:
            if not action_type:
                return True
            row_type = str(row.get("post_type") or "post")
            return row_type == action_type or (action_type == "post" and row_type in ("post", "original"))
        self.rows = sorted([row for row in source_rows if comparable(row)], key=lambda row: row.get("created_at") or "")
        self.fits: dict[str, ObjectiveFit] = {}
        self._fit()

    def _fit(self) -> None:
        for objective in OBJECTIVES:
            samples = [(row, normalized_objective(row, objective)) for row in self.rows]
            samples = [(row, target) for row, target in samples if target is not None and row.get("created_at")]
            # Below 30 comparable records Starling deliberately avoids numerical forecasts.
            if len(samples) < 30:
                continue
            # Imported here, not at module level: sklearn is about half of the engine's
            # import time, and nothing needs it until a model is really fitted (cold
            # start; the worker warms it in the background after READY).
            from sklearn.ensemble import HistGradientBoostingRegressor
            from sklearn.linear_model import BayesianRidge
            from sklearn.metrics import mean_absolute_error
            from sklearn.preprocessing import StandardScaler
            x = np.asarray([model_features(row["text"], row.get("created_at"), row.get("media_count", 0), row.get("post_type", "post")) for row, _ in samples])
            y = np.asarray([float(target) for _, target in samples])
            split = max(24, int(len(samples) * 0.8))
            split = min(split, len(samples) - 6)
            train_x, train_y, test_x, test_y = x[:split], y[:split], x[split:], y[split:]
            train_weights = recency_weights([row for row, _ in samples[:split]])
            final_weights = recency_weights([row for row, _ in samples])
            baseline = np.full_like(test_y, float(np.median(train_y)))
            baseline_mae = float(mean_absolute_error(test_y, baseline))

            bayes_scaler = StandardScaler().fit(train_x)
            bayes_evaluator = BayesianRidge(compute_score=True)
            bayes_evaluator.fit(bayes_scaler.transform(train_x), train_y, sample_weight=train_weights)
            bayes_predictions = bayes_evaluator.predict(bayes_scaler.transform(test_x))
            bayes_mae = float(mean_absolute_error(test_y, bayes_predictions))

            selected_type = "bayesian_linear"
            selected_mae = bayes_mae
            selected_predictions = bayes_predictions
            if len(samples) >= 200:
                nonlinear = HistGradientBoostingRegressor(
                    learning_rate=0.05,
                    max_iter=180,
                    max_leaf_nodes=15,
                    min_samples_leaf=12,
                    l2_regularization=0.5,
                    random_state=17,
                )
                nonlinear.fit(train_x, train_y, sample_weight=train_weights)
                nonlinear_predictions = nonlinear.predict(test_x)
                nonlinear_mae = float(mean_absolute_error(test_y, nonlinear_predictions))
                # A nonlinear model earns deployment only when the future holdout is materially better.
                if nonlinear_mae < bayes_mae * 0.99 and nonlinear_mae < baseline_mae:
                    selected_type = "gradient_boosted"
                    selected_mae = nonlinear_mae
                    selected_predictions = nonlinear_predictions

            final_scaler: StandardScaler | None = None
            if selected_type == "gradient_boosted":
                model = HistGradientBoostingRegressor(
                    learning_rate=0.05,
                    max_iter=180,
                    max_leaf_nodes=15,
                    min_samples_leaf=12,
                    l2_regularization=0.5,
                    random_state=17,
                ).fit(x, y, sample_weight=final_weights)
            else:
                final_scaler = StandardScaler().fit(x)
                model = BayesianRidge(compute_score=True).fit(final_scaler.transform(x), y, sample_weight=final_weights)
            residual_std = float(np.std(test_y - selected_predictions)) if len(test_y) else float(np.std(y))
            skill = 1.0 - selected_mae / baseline_mae if baseline_mae > 1e-9 else 0.0
            self.fits[objective] = ObjectiveFit(
                model=model,
                scaler=final_scaler,
                model_type=selected_type,
                targets=y,
                sample_size=len(samples),
                holdout_size=len(test_y),
                holdout_mae=selected_mae,
                baseline_mae=baseline_mae,
                skill=skill,
                residual_std=max(0.01, residual_std),
            )

    def predict(self, text: str, media_count: int = 0, post_type: str = "post",
                planned_at: datetime | str | None = None) -> tuple[dict[str, float | str | None], list[str]]:
        output: dict[str, float | str | None] = {name: None for name in OBJECTIVES}
        reasons: list[str] = []
        if not self.fits:
            output["confidence"] = "limited"
            return output, ["Limited evidence: at least 30 dated posts with comparable analytics are needed before Starling shows a numerical forecast."]
        x = np.asarray([model_features(text, planned_at or datetime.now(timezone.utc), media_count, post_type)])
        confidences: list[str] = []
        model_types: list[str] = []
        for objective, fit in self.fits.items():
            if fit.model_type == "bayesian_linear":
                transformed = fit.scaler.transform(x) if fit.scaler is not None else x
                predicted, predicted_std = fit.model.predict(transformed, return_std=True)
                prediction, uncertainty = float(predicted[0]), max(float(predicted_std[0]), fit.residual_std)
            else:
                prediction, uncertainty = float(fit.model.predict(x)[0]), fit.residual_std

            def percentile(value: float) -> float:
                rank = float((np.sum(fit.targets <= value) + 0.5) / (len(fit.targets) + 1))
                return round(min(0.95, max(0.05, rank)), 3)

            output[objective] = percentile(prediction)
            # 1.44 standard deviations is an approximately 85% interval. It remains intentionally broad.
            output[f"{objective}_low"] = percentile(prediction - 1.44 * uncertainty)
            output[f"{objective}_high"] = percentile(prediction + 1.44 * uncertainty)
            model_types.append(fit.model_type)
            if fit.sample_size >= 200 and fit.skill is not None and fit.skill > 0.05:
                confidences.append("strong")
            elif fit.sample_size >= 30 and fit.skill is not None and fit.skill > 0:
                confidences.append("moderate")
            else:
                confidences.append("limited")
        output["confidence"] = "strong" if confidences and all(item == "strong" for item in confidences) else "moderate" if any(item in ("strong", "moderate") for item in confidences) else "limited"
        output["model_type"] = ",".join(sorted(set(model_types)))
        reasons.append("Scores are personal historical percentiles with approximate 85% intervals, normalized for available follower and post-age evidence—not promised results.")
        if any(fit.skill is not None and fit.skill <= 0 for fit in self.fits.values()):
            reasons.append("Recent holdout results are no better than a simple baseline; treat the forecast as directional.")
            output["confidence"] = "limited"
        return output, reasons

    def report(self) -> dict:
        return {
            objective: {
                "sample_size": fit.sample_size, "holdout_size": fit.holdout_size,
                "holdout_mae": fit.holdout_mae, "baseline_mae": fit.baseline_mae,
                "skill": fit.skill, "model_type": fit.model_type,
                "uncertainty": "approximate_85_percent_interval",
            }
            for objective, fit in self.fits.items()
        }


# An hour has to clear the account's own average by a real margin to count as a
# window. Shrinkage already pulls a 3-post bucket ~62% of the way to the mean,
# so 1.05 here means the raw hour ran roughly 13% above average — a signal, not
# a rounding artifact.
_WINDOW_MIN_RELATIVE = 1.05
# Posts in the winning bucket before its window is more than a hint.
_WINDOW_MODERATE_SAMPLES = 8

# A THIN BUCKET IS JUDGED ON ITS RAW RECORD, NOT ON ITS SHRUNK SCORE.
#
# Reporting a thin window is deliberate — `test_a_thin_winning_hour_stays_a_low_
# confidence_read` pins it, and it is right: a real lift on 3 posts is a hint
# worth having, labelled as one. What was missing is any way to tell that hint
# apart from noise.
#
# Measured on his live account, 2026-08-05, `/timing/recommend`:
#
#     Thursday 18:00-20:00   sample_size 3   relative_score 1.11
#
# 59 dated posts across 168 weekday-hour buckets is 0.35 posts expected per
# bucket, so a bucket of 3 is ordinary clustering. The panel called it "your next
# measured best window" and the verdict reasoned about holding a draft 26 hours
# for it. That is noise wearing the word "measured".
#
# The shrunk score cannot separate the two, and this is the part worth writing
# down: shrinkage deliberately pulls a 3-post bucket most of the way to the mean,
# so it compresses a genuine signal and a rounding artifact into almost the same
# number. Measured on the fixtures:
#
#     the thin test's genuine signal, n=3   ->  shrunk relative 1.22
#     his live clustering artifact,   n=3   ->  shrunk relative 1.11
#     the well-sampled peak,          n=8   ->  shrunk relative 1.27
#
# 0.11 apart. A threshold splitting those is fitted to two data points, which is
# the one thing calibration here is not allowed to be. So the decision moves to
# the bucket's RAW mean, which is measured on its own record rather than on one
# already designed to distrust it:
#
#     genuine signal, n=3   ->  raw lift 1.58
#     his artifact,   n=3   ->  raw lift 1.28
#
# HONEST ABOUT THE MARGIN: 0.30, not the order of magnitude the raw impressions
# suggest. These values are `log1p`-normalized, so the fixture's eighty-fold
# impressions difference lands at 1.58 — log space is why the shrunk numbers were
# so close in the first place. 1.5 sits between the two known cases with roughly
# 0.08 either side, which is a real bar but a narrow one, and it is calibrated on
# exactly ONE genuine case and ONE artifact. Re-measure it the moment there are
# more of either; do not treat it as settled.
#
# What it means in practice: a thin hour has to look DRAMATICALLY better, not
# marginally better. A real 2x hour on three posts is suppressed too, and that is
# the intended reading — on three posts a 2x is not distinguishable from noise,
# and `posting_hours.py` already states the house rule for that case: "silence
# about a thing we cannot measure, rather than a guess."
#
# Well-sampled buckets are unaffected — there the evidence carries itself and
# `_WINDOW_MIN_RELATIVE` stands alone.
#
# When nothing clears it the function already had the honest answer written and
# was simply never reaching it: "No hour in your N dated posts reliably beats
# your own average - your results vary more by what you post than by when."
_WINDOW_THIN_MIN_RAW_LIFT = 1.5


def timing_recommendations(objective: str = "reach", timezone_name: str = "UTC",
                           rows: list[dict] | None = None,
                           post_type: str | None = None) -> dict:
    """Best windows, optionally for ONE surface. Posts and replies live on
    different clocks — a reply's moment is the parent post's first hours, a
    post's is the audience's active hours — so one blended table answers
    neither question honestly (registry B20/B22)."""
    rows = rows if rows is not None else performance_records()
    if post_type:
        accepted = {"post", "original", "poll", "thread"} if post_type == "post" else {post_type}
        rows = [row for row in rows if str(row.get("post_type") or "post") in accepted]
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        # ValueError too: `ZoneInfo("..")` and `ZoneInfo("")` raise it, not
        # ZoneInfoNotFoundError, and the timezone is a stored account-profile
        # field. One bad value written once made every call through here 500
        # forever, with no way to correct it from the UI that reads it.
        #
        # `timezone.utc`, not `ZoneInfo("UTC")`: on Windows the tz database
        # comes entirely from the `tzdata` package, so where the first call
        # failed for want of it the recovery would fail identically.
        zone, timezone_name = timezone.utc, "UTC"
    samples: list[tuple[int, int, float]] = []
    for row in rows:
        created = parse_datetime(row.get("created_at"))
        target = normalized_objective(row, objective)
        if created and target is not None:
            local = created.astimezone(zone)
            samples.append((local.weekday(), local.hour, target))
    if len(samples) < 30:
        kind = plural_post_type(post_type)
        return {"objective": objective, "timezone": timezone_name, "confidence": "limited",
                "sample_size": len(samples), "windows": [], "post_type": post_type,
                "reason": f"At least 30 dated {kind} with this metric are needed before recommending a window."}
    overall = float(np.mean([sample[2] for sample in samples]))
    buckets: dict[tuple[int, int], list[float]] = {}
    for day, hour, value in samples:
        buckets.setdefault((day, hour), []).append(value)
    ranked = []
    for (day, hour), values in buckets.items():
        if len(values) < 3:
            continue
        shrunk = (sum(values) + overall * 5) / (len(values) + 5)
        # The RAW mean travels with the shrunk score: shrinkage is the right
        # ranker and a poor discriminator on thin buckets, so the two questions
        # ("which hour is best" and "is this hour worth naming") need different
        # numbers. See `_WINDOW_THIN_MIN_RAW_LIFT`.
        ranked.append((shrunk, day, hour, len(values), float(sum(values)) / len(values)))
    ranked.sort(reverse=True)
    names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    # Ranking finds the BEST bucket; it does not make that bucket GOOD. On an
    # account whose results vary more by post than by clock, every bucket
    # shrinks to just under the mean and the top of the list is still a
    # below-average hour — reporting it as "your best window" is how a draft
    # ends up held for a slot that measurably underperforms posting now. A
    # window has to beat the account's own average to earn the name.
    windows = []
    for score, day, hour, count, raw_mean in ranked:
        relative = round(score / overall, 2) if overall > 0 else 1.0
        if relative < _WINDOW_MIN_RELATIVE:
            continue
        # A thin bucket must also have a raw record worth mentioning — see
        # `_WINDOW_THIN_MIN_RAW_LIFT`. This is what tells a genuine thin signal
        # apart from three posts that happened to land in the same hour.
        if count < _WINDOW_MODERATE_SAMPLES and overall > 0                 and raw_mean / overall < _WINDOW_THIN_MIN_RAW_LIFT:
            continue
        windows.append({"day": names[day], "start_hour": hour, "end_hour": (hour + 2) % 24,
                        "sample_size": count, "relative_score": relative})
        if len(windows) == 3:
            break
    # Confidence tracks the evidence behind the WINDOW, not the size of the
    # whole history: three posts in one hour is a hint, not a measurement.
    strongest = max((window["sample_size"] for window in windows), default=0)
    confidence = "moderate" if strongest >= _WINDOW_MODERATE_SAMPLES else "limited"
    if windows:
        reason = ("Windows use your normalized historical results with shrinkage toward your account average; "
                  "they are correlations, not guarantees.")
    else:
        reason = (f"No hour in your {len(samples)} dated posts reliably beats your own average — your results vary "
                  "more by what you post than by when. Post when you have something worth posting.")
    return {"objective": objective, "timezone": timezone_name, "confidence": confidence, "sample_size": len(samples),
            "windows": windows, "reason": reason}


def cadence_curve(rows: list[dict] | None = None, timezone_name: str = "UTC") -> dict:
    """Does the user's Nth post of the day underperform THEIR OWN median?

    Groups standalone posts by local calendar day, orders them, and reports the
    median normalized reach per day-position (1st, 2nd, 3rd, 4th+). The first
    position whose median drops ≥20% below the account median becomes
    `underperform_after` — the personalized daily budget the timing engine and
    opportunity notes use instead of a hardcoded number."""
    rows = rows if rows is not None else performance_records()
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc             # see the note on the same guard above
    dated: list[tuple[datetime, float]] = []
    for row in rows:
        if str(row.get("post_type") or "post") not in ("post", "original"):
            continue
        created = parse_datetime(row.get("created_at"))
        captured = parse_datetime(row.get("metrics_updated_at"))
        # A snapshot taken minutes after posting reads ~0 views and would count
        # as a real outcome — it poisoned the curve into "your FIRST post of
        # the day underperforms". Only metrics with ≥4h exposure count.
        if created and captured and (captured - created).total_seconds() < 4 * 3600.0:
            continue
        value = normalized_objective(row, "reach")
        if created and value is not None:
            dated.append((created.astimezone(zone), float(value)))
    dated.sort(key=lambda item: item[0])
    by_day: dict[str, list[float]] = {}
    for local, value in dated:
        by_day.setdefault(local.date().isoformat(), []).append(value)
    buckets: dict[str, list[float]] = {"1": [], "2": [], "3": [], "4+": []}
    for values in by_day.values():
        for position, value in enumerate(values, start=1):
            buckets["4+" if position >= 4 else str(position)].append(value)
    all_values = [value for _, value in dated]
    overall = median(all_values) if all_values else None
    positions: dict[str, dict] = {}
    underperform_after: int | None = None
    for key in ("1", "2", "3", "4+"):
        values = buckets[key]
        entry: dict[str, Any] = {"sample_size": len(values)}
        if len(values) >= 5 and overall:
            bucket_median = median(values)
            entry["median"] = round(bucket_median, 4)
            entry["vs_account_median"] = round(bucket_median / overall, 3) if overall else None
            # A bucket needs REAL evidence (≥8 samples) before it may shrink
            # the user's daily budget — thin buckets clamped the budget to 1.
            # Position 1 can never "underperform the day": there is no budget
            # below one post; the scan starts at the SECOND post of the day.
            if key != "1" and len(values) >= 8 and bucket_median <= overall * 0.8 and underperform_after is None:
                underperform_after = 4 if key == "4+" else int(key)
        positions[key] = entry
    return {
        "positions": positions,
        "account_median": round(overall, 4) if overall else None,
        "underperform_after": underperform_after,
        "sample_days": len(by_day),
        "note": "Medians of your own normalized reach by post-of-day position — a correlation, not a rule.",
    }


DEFAULT_DAILY_BUDGET = 3
DAILY_BUDGET_FLOOR = 2      # below this only overwhelming evidence may go
DAILY_BUDGET_CEILING = 5    # no history justifies more originals per day


def daily_post_budget(rows: list[dict] | None = None, timezone_name: str = "UTC") -> tuple[int, str | None]:
    """(budget, evidence). Personalized when the curve has real evidence, with
    a floor of 2 (unless ≥12 samples prove otherwise) and a hard ceiling of 5 —
    author-diversity caps make more originals self-competing regardless of
    history."""
    curve = cadence_curve(rows, timezone_name)
    after = curve.get("underperform_after")
    if after:
        position = curve["positions"].get("4+" if after >= 4 else str(after), {})
        ratio = position.get("vs_account_median")
        samples = int(position.get("sample_size") or 0)
        budget = max(1, after - 1)
        if budget < DAILY_BUDGET_FLOOR and samples < 12:
            budget = DAILY_BUDGET_FLOOR
        budget = min(budget, DAILY_BUDGET_CEILING)
        evidence = (f"your own history: post #{after} of the day lands at ~{ratio:.0%} of your account median "
                    f"({samples} samples)") if ratio is not None else None
        return budget, evidence
    return DEFAULT_DAILY_BUDGET, None


def diagnose_post(post_id: str, rows: list[dict] | None = None) -> dict | None:
    rows = rows if rows is not None else performance_records()
    post = next((row for row in rows if str(row.get("post_id")) == str(post_id)), None)
    if not post:
        post = get_post(post_id)
    if not post:
        return None
    comparable = [row for row in rows if row.get("post_type") == post.get("post_type") and bool(row.get("has_image")) == bool(post.get("has_image")) and row.get("post_id") != post.get("post_id")]
    observations, factors = [], []
    for objective in OBJECTIVES:
        value = normalized_objective(post, objective)
        peers = [target for row in comparable if (target := normalized_objective(row, objective)) is not None]
        if value is None or len(peers) < 5:
            continue
        percentile = sum(peer <= value for peer in peers) / len(peers)
        observations.append({"objective": objective, "percentile": round(percentile, 3), "sample_size": len(peers)})
    peer_lengths = [len(row.get("text", "")) for row in comparable if row.get("text")]
    if peer_lengths:
        baseline = median(peer_lengths)
        ratio = len(post.get("text", "")) / max(1, baseline)
        if ratio > 1.6:
            factors.append({"factor": "length", "direction": "possible_drag", "confidence": "low", "label": "correlation",
                            "evidence": f"This post is {ratio:.1f}× longer than the median of {len(peer_lengths)} comparable posts."})
        elif ratio < 0.55:
            factors.append({"factor": "length", "direction": "possible_constraint", "confidence": "low", "label": "correlation",
                            "evidence": "This post is much shorter than comparable posts; the idea may need more context."})
    if post.get("created_at"):
        factors.append({"factor": "timing", "direction": "unknown", "confidence": "low", "label": "inference",
                        "evidence": "A single post cannot establish that timing caused its result; test the timing engine across repeated posts."})
    return {"post_id": post_id, "observations": observations, "comparable_count": len(comparable), "likely_factors": factors,
            "limitations": ["Diagnostics identify associations in your own history and cannot determine why an individual person did or did not engage."]}


def delete_advice(post_id: str, rows: list[dict] | None = None) -> dict | None:
    """Should this post be deleted? An honest read, never a command.

    Three layers: (1) how the post actually ranks against comparable posts,
    (2) how fresh it is (deleting early wastes the replies already earned),
    (3) the user's own deletion history — if past deletions resemble their
    strongest themes, they may habitually delete the kind of post that works.
    Deleting is not known to penalise an account; this is about not
    throwing away posts that are quietly doing their job."""
    rows = rows if rows is not None else performance_records()
    post = next((row for row in rows if str(row.get("post_id")) == str(post_id)), None)
    if not post:
        post = get_post(post_id)
    if not post:
        return None
    created = parse_datetime(post.get("created_at"))
    age_hours = ((datetime.now(timezone.utc) - created).total_seconds() / 3600.0) if created else None
    diagnosis = diagnose_post(post_id, rows) or {}
    observations = diagnosis.get("observations") or []
    reach = next((item for item in observations if item["objective"] == "reach"), None)
    conversation = next((item for item in observations if item["objective"] == "conversation"), None)
    replies = float((post.get("metrics") or {}).get("replies") or 0)

    reasons: list[str] = []
    if age_hours is not None and age_hours < 24:
        verdict = "too_early"
        # THE REPLIES CLAUSE WAS UNCONDITIONAL AND `replies` IS COMPUTED FIVE
        # LINES ABOVE, UNREAD. A post with none was told it had some — an
        # invented reason to keep something, on the branch that fires for the
        # first 24 hours, which is exactly when he is deciding. Same family as
        # the engagement rate on three views: a true-sounding number about a
        # thing that has not happened yet.
        earned = (f" and deleting now discards the {int(replies)} "
                  f"{'reply' if replies == 1 else 'replies'} it already earned"
                  if replies else "")
        reasons.append(f"This post is only {age_hours:.1f}h old — distribution "
                       f"often continues past the first day{earned}.")
    elif reach and reach["percentile"] <= 0.25:
        verdict = "reasonable"
        reasons.append(f"It ranks at p{int(reach['percentile'] * 100)} for reach against {reach['sample_size']} "
                       "comparable posts — deleting a clear underperformer is common practice and costs little.")
        if replies >= 3 or (conversation and conversation["percentile"] >= 0.5):
            verdict = "keep"
            reasons.append("But it IS earning conversation — replies outweigh raw views in the ranking economics; "
                           "a low-view post with real replies is quietly doing its job.")
    elif reach:
        verdict = "keep"
        reasons.append(f"It ranks at p{int(reach['percentile'] * 100)} for reach against {reach['sample_size']} "
                       "comparable posts — this is not an underperformer worth erasing.")
    else:
        verdict = "insufficient_evidence"
        reasons.append("Not enough comparable measured posts to rank this one honestly.")

    # Deletion-history read: metrics are NOT retained for deleted posts (the
    # importer removes them from the posts table), so this is a THEME match —
    # do the user's past deletions look like their strongest current themes?
    history: dict[str, Any] = {"deleted_count": 0, "resembling_strong_theme": 0, "themes": []}
    try:
        from .storage import archive_texts
        from .account_model import personal_topic_profile, topic_performance_for
        deleted = archive_texts(["deleted_tweet"], limit=200)
        history["deleted_count"] = len(deleted)
        profile = personal_topic_profile(rows)
        hits: dict[str, int] = {}
        for text in deleted:
            match = topic_performance_for(text, profile)
            if match and match["median_percentile"] is not None and match["median_percentile"] >= 0.6:
                history["resembling_strong_theme"] += 1
                hits[match["label"]] = hits.get(match["label"], 0) + 1
        history["themes"] = [{"theme": label, "count": count}
                             for label, count in sorted(hits.items(), key=lambda item: -item[1])[:3]]
    except Exception:
        pass
    if history["deleted_count"] and history["resembling_strong_theme"] >= max(3, history["deleted_count"] // 4):
        reasons.append(f"Pattern in your archive: {history['resembling_strong_theme']} of your "
                       f"{history['deleted_count']} deleted posts resemble your strongest measured themes — "
                       "you may habitually delete the kind of post that works for you. Worth pausing on.")
        if verdict == "reasonable":
            verdict = "pause"

    return {
        "post_id": post_id,
        "verdict": verdict,  # keep | reasonable | too_early | pause | insufficient_evidence
        "reasons": reasons,
        "observations": observations,
        "deletion_history": history,
        "notes": [
            "Deleted posts keep no metrics, so the history read matches themes, not numbers.",
            "The ranking system does not penalize an account for deleting; this is only about not discarding posts that are working.",
            "Your call either way — Starling never deletes anything itself.",
        ],
    }


def diagnose_account(rows: list[dict] | None = None) -> dict:
    rows = rows if rows is not None else performance_records()
    modeled = {objective: sum(normalized_objective(row, objective) is not None for row in rows) for objective in OBJECTIVES}
    missing_created = sum(not row.get("created_at") for row in rows)
    missing_followers = sum(row.get("followers_count") is None for row in rows)
    model = ChronologicalPerformanceModel(rows)
    return {
        "posts": len(rows), "modeled_samples": modeled, "model_report": model.report(),
        "account_model": infer_account_model(rows), "growth_diagnoses": growth_diagnoses(rows),
        "data_quality": {"missing_created_at": missing_created, "missing_followers_at_publish": missing_followers},
        "limitations": ["Performance patterns are correlational and do not reveal private viewer motivations.", "Follower conversion requires explicit follow-attribution metrics; profile clicks are not treated as follows."],
    }


def performance_insights(rows: list[dict] | None = None) -> dict:
    """Honest 'what's working' summary for chat: per-format medians against
    the account's own numbers, best measured windows, and follower movement.
    Every figure carries its sample size — small samples say so plainly."""
    rows = rows if rows is not None else performance_records()
    by_format: dict[str, dict] = {}
    for fmt in ("reply", "quote", "post"):
        fmt_rows = [row for row in rows if (row.get("post_type") or "post") == fmt]
        # median_views is PLAIN views only. The reach composite (views +
        # reposts×30) is a separate, clearly-named score — the probes caught
        # the composite being quoted to the user as "median views".
        # NOT MEASURED IS NOT ZERO.
        #
        # `... or 0` turned a post whose metrics dict exists but carries no view
        # count into a post that got zero views, and those zeros went into the
        # median. Measured on his live archive, 2026-08-05: 89 post rows, 53 with
        # a metrics dict, THREE of them with no usable view count — and those
        # three pulled the median from 289.5 down to 226.0. He was being told his
        # typical post gets 226 views when the measured figure is 290, a 22%
        # understatement of his own account, in the number the diagnosis leads
        # with and the reach answer quotes.
        #
        # `raw_objective` is explicit about this in its own docstring — "Return
        # None when the export did not provide any metric for an objective" — so
        # coercing its None to 0 contradicted the contract it states.
        #
        # Likes are deliberately left alone: a real zero-like post is common and
        # indistinguishable here from a missing count, so excluding them would
        # trade a known bias for an unknown one. That is worth revisiting with a
        # per-metric presence flag rather than guessed at now.
        def _view_count(row: dict) -> float | None:
            metrics = row.get("metrics") or {}
            raw = metrics.get("views", metrics.get("impressions"))
            if raw in (None, ""):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        views = sorted(value for value in (_view_count(row) for row in fmt_rows)
                       if value is not None)
        reach_values = []
        for row in fmt_rows:
            if not row.get("metrics"):
                continue
            value = raw_objective(row.get("metrics", {}), "reach")
            if value is not None:
                reach_values.append(float(value))
        reach = sorted(reach_values)
        likes = sorted(float(row.get("metrics", {}).get("likes") or 0) for row in fmt_rows if row.get("metrics"))
        # `posts` is every row of this format; `measured_views` is the sample the
        # median is actually computed from. Reporting the first as the second is
        # how "a median of 226 views across 89 recorded" got said about 50 posts.
        entry: dict[str, Any] = {"posts": len(fmt_rows), "measured_views": len(views)}
        if views:
            entry["median_views"] = views[len(views) // 2]
        if reach:
            entry["median_reach_score"] = reach[len(reach) // 2]
        if likes:
            entry["median_likes"] = likes[len(likes) // 2]
        # Evidence tracks what was MEASURED, not how many rows exist. On his
        # account that is the difference between calling 50 measured posts a
        # "solid" sample because 89 rows are on file.
        graded = len(views) if views else len(fmt_rows)
        entry["evidence"] = "solid" if graded >= 30 else "limited" if graded >= 8 else "very limited"
        by_format[fmt] = entry
    ranked = sorted((item for item in by_format.items() if item[1].get("median_views") is not None),
                    key=lambda item: item[1]["median_views"], reverse=True)
    timing = timing_recommendations("reach", rows=rows)
    followers = None
    try:
        from .storage import list_profile_snapshots
        snapshots = list_profile_snapshots(limit=60)
        counts = [(snapshot.get("captured_at"), snapshot.get("followers_count"))
                  for snapshot in snapshots if snapshot.get("followers_count") is not None]
        if len(counts) >= 2:
            followers = {"latest": counts[0][1], "earliest_in_window": counts[-1][1],
                         "delta": counts[0][1] - counts[-1][1], "snapshots": len(counts)}
    except Exception:
        followers = None
    topic_themes = None
    try:
        from .account_model import personal_topic_profile
        profile = personal_topic_profile(rows)
        topic_themes = [{key: cluster[key] for key in ("label", "size", "measured", "median_percentile", "evidence")}
                        for cluster in profile.get("clusters", [])[:5]] or None
    except Exception:
        topic_themes = None
    return {
        "by_format": by_format,
        "strongest_format": ranked[0][0] if ranked else None,
        "topic_themes": topic_themes,
        "best_windows": (timing.get("windows") or [])[:2],
        "timing_sample_size": timing.get("sample_size"),
        "followers": followers,
        "note": "Medians against this account's own history; small samples are labeled and never treated as proof.",
    }


def evaluate_experiment_observations(
    observations: dict[str, list[float]],
    variants: list[str],
    minimum_per_variant: int = 5,
    control_variant: str | None = None,
) -> dict:
    minimum = max(5, int(minimum_per_variant))
    cleaned = {
        variant: [float(value) for value in observations.get(variant, []) if math.isfinite(float(value))]
        for variant in variants
    }
    counts = {variant: len(values) for variant, values in cleaned.items()}
    if len(variants) < 2 or any(counts.get(variant, 0) < minimum for variant in variants):
        return {
            "outcome": "collect_more",
            "promoted": False,
            "minimum_per_variant": minimum,
            "counts": counts,
            "posterior_confidence": None,
            "estimated_uplift": None,
            "reason": f"At least {minimum} comparable observations are required for every variant.",
        }
    control = control_variant if control_variant in variants else variants[0]
    challengers = [variant for variant in variants if variant != control]
    best = max(challengers, key=lambda variant: float(np.mean(cleaned[variant])))
    control_values = np.asarray(cleaned[control], dtype=float)
    best_values = np.asarray(cleaned[best], dtype=float)
    control_mean, best_mean = float(np.mean(control_values)), float(np.mean(best_values))
    difference = best_mean - control_mean
    variance = float(np.var(control_values, ddof=1) / len(control_values) + np.var(best_values, ddof=1) / len(best_values))
    standard_error = math.sqrt(max(0.0, variance))
    if standard_error <= 1e-12:
        posterior_confidence = 1.0 if difference > 0 else 0.5
    else:
        z_score = difference / standard_error
        posterior_confidence = 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))
    uplift = difference / max(abs(control_mean), 1e-6)
    promoted = posterior_confidence > 0.85 and uplift > 0.10
    return {
        "outcome": "promoted" if promoted else "inconclusive",
        "promoted": promoted,
        "control_variant": control,
        "leading_variant": best,
        "counts": counts,
        "posterior_confidence": round(posterior_confidence, 4),
        "estimated_uplift": round(uplift, 4),
        "means": {variant: round(float(np.mean(values)), 6) for variant, values in cleaned.items()},
        "reason": "The finding clears both the 85% confidence and 10% practical-uplift gates." if promoted else "The result is directional only; it does not clear both promotion gates.",
    }
