from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.performance import ChronologicalPerformanceModel, diagnose_post, evaluate_experiment_observations, normalized_objective, raw_objective, recency_weights, timing_recommendations


def rows(count: int = 60):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    values = []
    for index in range(count):
        created = start + timedelta(days=index, hours=index % 6 + 8)
        values.append({
            "post_id": str(index), "text": ("Question? " if index % 2 else "Observation. ") + f"example {index}",
            "created_at": created.isoformat(), "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
            "post_type": "post", "media_count": index % 2, "has_image": index % 2,
            "followers_count": 1000 + index * 10,
            "metrics": {"impressions": 100 + index * 12, "reposts": index % 5, "replies": index % 7, "quotes": index % 3,
                        "likes": 10 + index, "bookmarks": index % 4},
        })
    return values


def test_missing_objective_is_not_silently_zero():
    assert raw_objective({"impressions": 100}, "conversation") is None
    assert normalized_objective({"metrics": {"impressions": 100}}, "conversation") is None


def test_recency_weights_favor_newer_visible_records_without_erasing_history():
    weights = recency_weights([
        {"created_at": "2025-01-01T00:00:00+00:00"},
        {"created_at": "2026-04-16T00:00:00+00:00"},
        {"created_at": "2026-07-15T00:00:00+00:00"},
    ])
    assert weights[0] == 0.25
    assert 0.25 < weights[1] < 1.0
    assert weights[2] == 1.0


def test_model_uses_chronological_holdout_and_calibrated_percentiles():
    model = ChronologicalPerformanceModel(rows())
    report = model.report()
    assert report["reach"]["holdout_size"] >= 5
    assert report["reach"]["sample_size"] == 60
    prediction, reasons = model.predict("A clear question?", 0, "post", "2026-08-01T12:00:00+00:00")
    assert 0.05 <= prediction["reach"] <= 0.95
    assert prediction["reach_low"] <= prediction["reach"] <= prediction["reach_high"]
    assert report["reach"]["model_type"] == "bayesian_linear"
    assert any("percentiles" in reason for reason in reasons)


def test_model_contract_at_29_30_199_and_200_records():
    below = ChronologicalPerformanceModel(rows(29))
    prediction, _ = below.predict("A careful idea")
    assert below.report() == {}
    assert prediction["reach"] is None and prediction["confidence"] == "limited"

    minimum = ChronologicalPerformanceModel(rows(30))
    assert minimum.report()["reach"]["model_type"] == "bayesian_linear"
    assert minimum.predict("A careful idea")[0]["reach"] is not None

    middle = ChronologicalPerformanceModel(rows(199))
    assert middle.report()["reach"]["model_type"] == "bayesian_linear"

    large = ChronologicalPerformanceModel(rows(200))
    # Nonlinear deployment is allowed only if the chronological holdout improves;
    # otherwise the regularized Bayesian model remains in service.
    assert large.report()["reach"]["model_type"] in ("bayesian_linear", "gradient_boosted")


def test_timing_suppresses_small_samples_and_shrinks_larger_samples():
    assert timing_recommendations("reach", "UTC", rows(20))["confidence"] == "limited"
    result = timing_recommendations("reach", "UTC", rows(80))
    assert result["sample_size"] == 80
    assert result["confidence"] in ("limited", "moderate")
    if result["windows"]:
        assert result["windows"][0]["sample_size"] >= 3


def _hourly_rows(spec: dict[int, list[float]]) -> list[dict]:
    """Dated posts at chosen UTC hours with chosen reach."""
    base = datetime(2026, 3, 2, tzinfo=timezone.utc)   # a Monday
    out, index = [], 0
    for hour, values in spec.items():
        for week, impressions in enumerate(values):
            created = base + timedelta(weeks=week, hours=hour)
            out.append({"post_id": f"h{index}", "text": "A post about systems and agents.",
                        "post_type": "post", "created_at": created.isoformat(),
                        "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
                        "followers_count": 1000,
                        "metrics": {"impressions": impressions, "likes": 10, "replies": 2,
                                    "reposts": 1, "quotes": 0, "bookmarks": 1}})
            index += 1
    return out


def test_a_window_must_beat_the_account_average_to_be_named():
    # Every hour performs the same. Ranking still orders them, but the top of a
    # flat list is not a "best window" — naming one would send a draft to an
    # hour with no measured advantage over posting right now.
    flat = _hourly_rows({hour: [5_000] * 5 for hour in (9, 12, 15, 18, 21, 22, 23)})
    result = timing_recommendations("reach", "UTC", flat)
    assert result["sample_size"] >= 30
    assert result["windows"] == []
    assert "beats your own average" in result["reason"]
    assert result["confidence"] == "limited"


def test_a_genuinely_better_hour_is_still_reported():
    # One hour really does outperform: the filter must not suppress real signal.
    peaky = _hourly_rows({9: [500] * 6, 12: [500] * 6, 15: [500] * 6,
                          18: [500] * 6, 20: [40_000] * 8})
    result = timing_recommendations("reach", "UTC", peaky)
    assert [w["start_hour"] for w in result["windows"]] == [20]
    assert result["windows"][0]["relative_score"] > 1.05
    # Eight posts in the winning hour is enough to call the read moderate.
    assert result["confidence"] == "moderate"


def test_a_thin_winning_hour_stays_a_low_confidence_read():
    # A real lift measured on 3 posts is a hint, not a measurement — the
    # confidence must reflect the window's own evidence, not the whole history.
    thin = _hourly_rows({9: [500] * 9, 12: [500] * 9, 15: [500] * 9, 20: [40_000] * 3})
    result = timing_recommendations("reach", "UTC", thin)
    assert [w["start_hour"] for w in result["windows"]] == [20]
    assert result["windows"][0]["sample_size"] == 3
    assert result["confidence"] == "limited"


def test_diagnosis_labels_inference_and_denies_private_motivation_claims():
    result = diagnose_post("59", rows())
    assert result and result["comparable_count"] > 10
    assert all(factor["label"] in ("correlation", "inference") for factor in result["likely_factors"])
    assert "cannot determine" in result["limitations"][0]


def test_experiment_promotion_requires_sample_confidence_and_practical_uplift():
    small = evaluate_experiment_observations({"control": [1, 1, 1, 1], "new": [2, 2, 2, 2]}, ["control", "new"])
    assert small["outcome"] == "collect_more" and not small["promoted"]

    promoted = evaluate_experiment_observations({"control": [10] * 6, "new": [12] * 6}, ["control", "new"])
    assert promoted["promoted"]
    assert promoted["posterior_confidence"] > 0.85
    assert promoted["estimated_uplift"] > 0.10

    inconclusive = evaluate_experiment_observations({"control": [10] * 6, "new": [10.5] * 6}, ["control", "new"])
    assert inconclusive["outcome"] == "inconclusive" and not inconclusive["promoted"]


def test_three_posts_that_merely_cluster_are_not_named_as_a_window():
    """His live account, 2026-08-05: `/timing/recommend` answered "Thursday
    18:00-20:00, sample_size 3, relative_score 1.11" and the verdict called it
    "your next measured best window", reasoning about holding a draft 26 hours
    for it. 59 dated posts over 168 weekday-hour buckets is 0.35 expected per
    bucket, so a bucket of three is ordinary clustering.

    Reporting a thin window stays deliberate (see the test above). What is gated
    is a thin window with an unremarkable RAW record — the shrunk score cannot
    tell those apart, being designed to distrust thin buckets in the first place.
    """
    # A hair above the account average in every hour, and one hour with three
    # posts a little better. Nothing here is a measurement.
    noise = _hourly_rows({9: [5_000] * 9, 12: [5_000] * 9, 15: [5_000] * 9,
                          20: [9_000] * 3})
    result = timing_recommendations("reach", "UTC", noise)
    assert result["sample_size"] >= 30
    assert result["windows"] == [], result["windows"]
    assert "beats your own average" in result["reason"]


def test_the_thin_bar_is_narrow_and_says_so():
    """The margin between the genuine thin signal and the artifact is 0.30 on a
    log-normalized scale, calibrated on one case of each. Pinned so the next
    person sees how little room there is before they move it."""
    from app import performance as perf

    genuine = _hourly_rows({9: [500] * 9, 12: [500] * 9, 15: [500] * 9,
                            20: [40_000] * 3})
    assert timing_recommendations("reach", "UTC", genuine)["windows"], (
        "the genuine thin signal must survive - it sits at raw lift 1.58 "
        "against a 1.5 bar, which is 0.08 of headroom")
    assert perf._WINDOW_THIN_MIN_RAW_LIFT == 1.5


def test_a_post_with_no_view_count_is_not_counted_as_zero_views():
    """Measured on his live archive 2026-08-05: 89 post rows, 53 with a metrics
    dict, THREE of them carrying no usable view count. `... or 0` fed those three
    into the median as zeros and pulled it from 289.5 down to 226.0 — he was told
    his typical post gets 226 views when the measured figure is ~290, a 22%
    understatement in the number the diagnosis leads with.

    `raw_objective` states the rule in its own docstring: "Return None when the
    export did not provide any metric for an objective."
    """
    from app.performance import performance_insights

    now = datetime(2026, 3, 2, tzinfo=timezone.utc)
    rows = []
    for index in range(9):
        rows.append({"post_id": f"m{index}", "post_type": "post",
                     "created_at": (now + timedelta(hours=index)).isoformat(),
                     "metrics_updated_at": now.isoformat(), "followers_count": 1000,
                     "text": "a measured post, long enough to read back",
                     "metrics": {"views": 300, "likes": 1, "replies": 0}})
    # Three posts observed but never given a view count.
    for index in range(3):
        rows.append({"post_id": f"u{index}", "post_type": "post",
                     "created_at": (now + timedelta(days=1, hours=index)).isoformat(),
                     "metrics_updated_at": now.isoformat(), "followers_count": 1000,
                     "text": "an unmeasured post, long enough to read back",
                     "metrics": {"likes": 0, "replies": 0}})

    post = (performance_insights(rows).get("by_format") or {}).get("post") or {}
    assert post["median_views"] == 300, f"unmeasured posts dragged the median: {post}"
    # And the sample the median came from is reported, not the row count.
    assert post["measured_views"] == 9, post
    assert post["posts"] == 12, post


def test_evidence_grades_what_was_measured_not_how_many_rows_exist():
    """"solid sample" was decided on 89 rows while the median rested on 50."""
    from app.performance import performance_insights

    now = datetime(2026, 3, 2, tzinfo=timezone.utc)
    rows = [{"post_id": f"r{index}", "post_type": "post",
             "created_at": (now + timedelta(hours=index)).isoformat(),
             "metrics_updated_at": now.isoformat(), "followers_count": 1000,
             "text": "a row with no view count, long enough to read back",
             "metrics": {"likes": 0}} for index in range(40)]
    rows.append({"post_id": "one", "post_type": "post",
                 "created_at": (now + timedelta(days=2)).isoformat(),
                 "metrics_updated_at": now.isoformat(), "followers_count": 1000,
                 "text": "the only measured post, long enough to read back",
                 "metrics": {"views": 500, "likes": 1}})
    post = (performance_insights(rows).get("by_format") or {}).get("post") or {}
    assert post["measured_views"] == 1, post
    assert post["evidence"] == "very limited", (
        f"40 unmeasured rows called the sample {post['evidence']!r}")
