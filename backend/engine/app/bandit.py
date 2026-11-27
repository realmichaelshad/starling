"""Angle bandit: learn which strategy-manual angles work for THIS account.

Arms are format × angle (the manual's §6 angle library — 27 arms total).
Rewards are outcome percentiles versus the account's own same-format baseline,
so a small account is never judged against someone else's numbers.

Thompson sampling with a Gaussian posterior shrunk toward the account baseline
(prior mean 0.5, strength 8 pseudo-observations): an angle needs several real
wins to escape the prior, and one viral outlier cannot take over. Recency
weights follow the manual's §33.4 (14d/60d steps). The bandit NEVER overrides
a wait/skip decision — it only diversifies angles once drafting was chosen.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from .storage import connect, mark_corpus_dirty
from .strategy_v2 import angle_ids, angle_title

PRIOR_MEAN = 0.5
PRIOR_STRENGTH = 8.0
PRIOR_VARIANCE = 0.08
EXPLORE_SLOT_FRACTION = 1  # of k slots, exactly one free Thompson draw

RECENCY_STEPS = ((14.0, 1.0), (60.0, 0.5), (float("inf"), 0.2))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _recency_weight(observed_at: str, now: datetime) -> float:
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.2
    age_days = max(0.0, (now - observed).total_seconds() / 86_400.0)
    for boundary, weight in RECENCY_STEPS:
        if age_days <= boundary:
            return weight
    return 0.2


def arm_id(fmt: str, angle: str) -> str:
    return f"{fmt}:{angle}"


def record_observation(fmt: str, angle: str, reward: float, *, post_id: str | None = None,
                       generation_id: str | None = None, weight: float = 1.0,
                       parts: dict[str, Any] | None = None, observed_at: str | None = None) -> None:
    identifier = arm_id(fmt, angle)
    now = _utc_now().isoformat()
    payload = dict(parts or {})
    payload["weight"] = weight
    with connect() as conn:
        conn.execute(
            "INSERT INTO bandit_observations(arm_id,post_id,generation_id,reward,reward_parts_json,observed_at)"
            " VALUES(?,?,?,?,?,?)",
            (identifier, post_id, generation_id, float(reward), json.dumps(payload), observed_at or now))
        # bandit_arms rows are an inspectable summary; posteriors are computed
        # from raw observations so recency weighting stays exact.
        conn.execute(
            """INSERT INTO bandit_arms(arm_id,format,angle_id,n,reward_sum,reward_sq,last_observation_at,updated_at)
               VALUES(?,?,?,1,?,?,?,?)
               ON CONFLICT(arm_id) DO UPDATE SET n=bandit_arms.n+1,
                 reward_sum=bandit_arms.reward_sum+excluded.reward_sum,
                 reward_sq=bandit_arms.reward_sq+excluded.reward_sq,
                 last_observation_at=excluded.last_observation_at, updated_at=excluded.updated_at""",
            (identifier, fmt, angle, float(reward), float(reward) ** 2, observed_at or now, now))
    mark_corpus_dirty()


def _posteriors(fmt: str) -> dict[str, dict[str, float]]:
    """Gaussian posterior per angle for one format, recency-weighted."""
    now = _utc_now()
    with connect() as conn:
        rows = conn.execute(
            "SELECT arm_id,reward,reward_parts_json,observed_at FROM bandit_observations WHERE arm_id LIKE ?",
            (f"{fmt}:%",)).fetchall()
    stats: dict[str, dict[str, float]] = {}
    for angle in angle_ids():
        stats[angle] = {"weight": 0.0, "weighted_sum": 0.0}
    for row in rows:
        angle = str(row[0]).split(":", 1)[1] if ":" in str(row[0]) else ""
        if angle not in stats:
            continue
        try:
            observation_weight = float(json.loads(row[2] or "{}").get("weight", 1.0))
        except (ValueError, TypeError):
            observation_weight = 1.0
        weight = _recency_weight(str(row[3]), now) * observation_weight
        stats[angle]["weight"] += weight
        stats[angle]["weighted_sum"] += weight * float(row[1])
    posteriors: dict[str, dict[str, float]] = {}
    for angle, item in stats.items():
        effective_n = item["weight"]
        mean = ((PRIOR_MEAN * PRIOR_STRENGTH) + item["weighted_sum"]) / (PRIOR_STRENGTH + effective_n)
        variance = PRIOR_VARIANCE / (1.0 + effective_n / PRIOR_STRENGTH)
        posteriors[angle] = {"mean": mean, "variance": variance, "n": effective_n}
    return posteriors


def _deterministic_gauss(seed: str) -> float:
    """Unit-variance-ish deterministic pseudo-gaussian from a seed (no RNG state)."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    # Sum of 6 uniform bytes approximates a normal (CLT), centered and scaled.
    total = sum(digest[:6]) / (6 * 255.0)  # 0..1, mean .5
    return (total - 0.5) * math.sqrt(12 * 6) / math.sqrt(6)


def choose_angles(fmt: str, k: int, seed: str = "") -> list[dict[str, Any]]:
    """Pick k distinct angles: k-1 exploit slots from the top posterior means,
    1 exploration slot via a Thompson draw over the remainder."""
    k = max(1, min(k, len(angle_ids())))
    posteriors = _posteriors(fmt)
    ranked = sorted(posteriors.items(), key=lambda item: item[1]["mean"], reverse=True)
    exploit_count = max(0, k - EXPLORE_SLOT_FRACTION)
    chosen = [angle for angle, _ in ranked[:exploit_count]]
    remainder = [angle for angle, _ in ranked[exploit_count:]]
    if remainder and len(chosen) < k:
        draws = []
        for angle in remainder:
            posterior = posteriors[angle]
            noise = _deterministic_gauss(f"{seed}:{fmt}:{angle}")
            draws.append((posterior["mean"] + math.sqrt(posterior["variance"]) * noise, angle))
        draws.sort(reverse=True)
        chosen.append(draws[0][1])
    return [{"angle_id": angle, "title": angle_title(angle),
             "posterior_mean": round(posteriors[angle]["mean"], 3),
             "observations": round(posteriors[angle]["n"], 1)} for angle in chosen[:k]]


def classify_angle(text: str) -> str | None:
    """Heuristic angle classification for back-filling historical posts.

    Deliberately conservative: an unmatched post returns None (weight-0.5
    back-fill only happens on a confident keyword match).
    """
    lowered = text.lower()
    checks: list[tuple[str, bool]] = [
        ("exact_prediction", "prediction" in lowered or bool(_month_and_number(lowered))),
        ("direct_comparison", any(token in lowered for token in (" same level as ", " better than ", " worse than ", " vs "))),
        ("real_user_pain", any(token in lowered for token in ("limit", "pricing", "can't use", "cant use", "restricted", "waitlist"))),
        ("contradiction", any(token in lowered for token in ("even the", "don't trust", "meanwhile", "the same company", "they said"))),
        ("open_vs_closed", any(token in lowered for token in ("open source", "open-source", "open weights", "local model"))),
        ("benchmarks_vs_use", "benchmark" in lowered),
        ("release_cadence", any(token in lowered for token in ("release", "launch", "ship", "cadence"))),
        ("money_compute", any(token in lowered for token in ("$", "billion", "compute", "gpu"))),
        ("question_with_premise", "?" in lowered and len(lowered) > 60),
    ]
    for angle, matched in checks:
        if matched:
            return angle
    return None


def _month_and_number(lowered: str) -> bool:
    months = ("january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december")
    return any(month in lowered for month in months) and any(char.isdigit() for char in lowered)


def bandit_summary(fmt: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """Top arms for the strategy brief so chat can explain its choices."""
    formats = [fmt] if fmt else ["reply", "quote", "post"]
    rows: list[dict[str, Any]] = []
    for item_format in formats:
        posteriors = _posteriors(item_format)
        for angle, posterior in posteriors.items():
            if posterior["n"] > 0:
                rows.append({"format": item_format, "angle": angle_title(angle),
                             "posterior_mean": round(posterior["mean"], 3),
                             "observations": round(posterior["n"], 1)})
    rows.sort(key=lambda item: item["posterior_mean"], reverse=True)
    return rows[:limit]
