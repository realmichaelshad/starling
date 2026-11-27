"""Personal topic profile: themes from the user's own history joined with
honest per-theme performance, and the matching helper the radar/scorer use."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.account_model import personal_topic_profile, topic_performance_for


def _row(post_id: str, text: str, views: int, days_ago: int) -> dict:
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "post_id": post_id,
        "text": text,
        "post_type": "post",
        "created_at": created.isoformat(),
        "metrics": {"views": views, "likes": views // 50, "replies": views // 200},
        "metrics_updated_at": (created + timedelta(days=1)).isoformat(),
        "followers_count": 1000,
        "media_count": 0,
    }


def _fixture_rows() -> list[dict]:
    rows = []
    # Strong theme: model benchmark comparisons — consistently high views.
    for index in range(6):
        rows.append(_row(f"bench-{index}",
                         f"Benchmark comparison run {index}: the new model tops coding benchmarks but the "
                         "benchmark gap versus real production coding work keeps growing wider.",
                         40_000 + index * 1000, 30 - index))
    # Weak theme: career advice — consistently low views.
    for index in range(6):
        rows.append(_row(f"career-{index}",
                         f"Career advice note {index}: junior engineers should focus career growth on "
                         "fundamentals, mentorship and career patience over titles.",
                         300 + index * 10, 28 - index))
    return rows


def test_profile_separates_strong_and_weak_themes():
    profile = personal_topic_profile(_fixture_rows())
    clusters = profile["clusters"]
    assert len(clusters) >= 2
    labels = " ".join(cluster["label"] for cluster in clusters)
    assert "benchmark" in labels
    assert "career" in labels
    benchmark = next(c for c in clusters if "benchmark" in c["label"])
    career = next(c for c in clusters if "career" in c["label"])
    assert benchmark["median_percentile"] > career["median_percentile"]
    assert benchmark["evidence"] in ("limited", "solid")
    assert benchmark["size"] >= 3


def test_profile_is_honest_with_tiny_history():
    profile = personal_topic_profile([_row("only", "One single post about benchmarks.", 100, 3)])
    assert profile["clusters"] == []
    assert "Not enough" in profile["note"]


def test_topic_performance_matches_theme():
    profile = personal_topic_profile(_fixture_rows())
    match = topic_performance_for("coding benchmarks", profile)
    assert match is not None
    assert "benchmark" in match["label"]
    assert match["median_percentile"] > 0.5
    weak = topic_performance_for("career advice for junior engineers", profile)
    assert weak is not None
    assert weak["median_percentile"] < 0.5
    assert topic_performance_for("completely unrelated gardening topic", profile) is None
