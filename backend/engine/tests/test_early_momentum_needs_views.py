"""An unmeasured post is not a failing post.

`early_checkpoint` guarded only on the metrics dict being EMPTY, so a post
carrying likes but no view count reached `momentum_verdict` as zero views per
hour. Against a real baseline that reads as a post that is dying:

    status 'quiet', velocity_ratio 0.0,
    "Early velocity is below your median pace... note the hook and timing"

Advice about the hook, from a view count nobody had observed. Measured on his
archive: 3 of the 53 post rows with a metrics dict carry no usable view count.

The baseline builder in the same module already had this right
("if metrics.get('views') is not None"); only this call site did not.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import learning_loop
from app.learning_loop import early_checkpoint, momentum_verdict

BASELINE = {"views_per_hour_median": 40.0, "replies_per_hour_median": 0.5, "sample": 30}


def test_zero_views_would_have_read_as_a_dying_post() -> None:
    """The harm this guards, stated directly: 0 velocity is a real verdict."""
    verdict = momentum_verdict(0.0, 0.0, BASELINE)
    assert verdict is not None
    assert verdict["velocity_ratio"] == 0.0
    assert verdict["status"] == "quiet"


def _published(monkeypatch, metrics: dict) -> dict:
    """One generation posted 30 minutes ago, with the given metrics.

    `early_checkpoint` reads its generations through `_posted_generations(conn)`
    and its baseline through `account_velocity_baseline`, so those are what have
    to be stubbed - not the names I guessed at first.
    """
    now = datetime.now(timezone.utc)
    posted = (now - timedelta(minutes=30)).isoformat()
    monkeypatch.setattr(learning_loop, "_posted_generations",
                        lambda conn: [{"published_post_id": "p1", "posted_at": posted}])
    monkeypatch.setattr(learning_loop, "get_post",
                        lambda post_id: {"post_id": "p1", "metrics": metrics})
    monkeypatch.setattr(learning_loop, "account_velocity_baseline",
                        lambda *a, **k: BASELINE)
    return early_checkpoint(now=now)


def test_a_post_with_likes_but_no_view_count_is_not_judged(monkeypatch) -> None:
    result = _published(monkeypatch, {"likes": 2, "replies": 0})
    checks = result.get("checks") or []
    assert checks, "the published post was not checked at all"
    assert checks[0]["status"] == "no_metrics_yet", checks[0]
    assert "velocity_ratio" not in checks[0], checks[0]


def test_a_post_with_a_real_view_count_is_still_judged(monkeypatch) -> None:
    result = _published(monkeypatch, {"views": 300, "likes": 2, "replies": 1})
    checks = result.get("checks") or []
    assert checks
    assert checks[0]["status"] != "no_metrics_yet", checks[0]
    assert "velocity_ratio" in checks[0], checks[0]


def test_zero_views_reported_explicitly_is_still_a_measurement(monkeypatch) -> None:
    """A real observed 0 must NOT be discarded - that is a measured fact."""
    result = _published(monkeypatch, {"views": 0, "likes": 0})
    checks = result.get("checks") or []
    assert checks
    assert checks[0]["status"] != "no_metrics_yet", checks[0]
