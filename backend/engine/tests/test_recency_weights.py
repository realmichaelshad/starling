"""The recency-weight law: nothing moves unless the account itself moved.

Pins the three guarantees the online-learning work stands on:
- uniform or absent dates produce today's exact numbers (weighted paths are
  reached only when rows genuinely differ in age);
- decay follows the performance.recency_weights precedent (half-life 90d,
  floor 0.25, undated rows weigh 1.0);
- Starling-written posts count half for VOICE, and because every consumer
  normalizes by the weight sum, an all-Starling corpus measures identically
  to an unmarked one — self-training collapse is structurally impossible,
  not just discouraged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.account_model import _pctile, _style_metrics
from app.recency import (
    DEFAULT_HALF_LIFE_DAYS,
    WEIGHT_FLOOR,
    recency_weights,
    topic_weights,
    voice_weights,
)

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _row(text: str, *, days_old: float | None = None, origin: str | None = None) -> dict:
    row = {"text": text, "post_type": "post"}
    if days_old is not None:
        row["created_at"] = (NOW - timedelta(days=days_old)).isoformat()
    if origin:
        row["origin"] = origin
    return row


def _style(rows: list[dict], weights=None) -> dict:
    texts = [r["text"] for r in rows]
    counts = [len(t.split()) for t in texts]
    return _style_metrics(rows, texts, counts, len(texts), weights=weights)


SAMPLE_TEXTS = [
    "Benchmarks without the harness are marketing.",
    "shipped a tiny eval tonight\n\nit already caught two regressions",
    "Local models keep getting cheaper. That's the story!",
    "who else is running these on a 4090?",
    "Pricing is the product now.",
    "The context window is not the bottleneck, retrieval is.",
]


def test_undated_rows_weigh_one():
    rows = [_row(t) for t in SAMPLE_TEXTS]
    assert recency_weights(rows) == [1.0] * len(rows)
    assert topic_weights(rows) == [1.0] * len(rows)


def test_undated_row_inside_dated_set_weighs_one():
    rows = [_row("a", days_old=0), _row("b"), _row("c", days_old=90)]
    weights = recency_weights(rows)
    assert weights[1] == 1.0


def test_decay_matches_the_performance_precedent():
    rows = [_row("newest", days_old=0), _row("half", days_old=DEFAULT_HALF_LIFE_DAYS),
            _row("floor", days_old=400)]
    weights = recency_weights(rows)
    assert weights[0] == 1.0
    assert abs(weights[1] - 0.5) < 1e-9
    assert weights[2] == WEIGHT_FLOOR


def test_first_observed_at_dates_watcher_only_rows():
    rows = [{"text": "seen", "first_observed_at": NOW.isoformat()},
            {"text": "older", "first_observed_at": (NOW - timedelta(days=DEFAULT_HALF_LIFE_DAYS)).isoformat()}]
    weights = recency_weights(rows)
    assert abs(weights[1] - 0.5) < 1e-9


def test_equal_weights_reproduce_unweighted_style_exactly():
    rows = [_row(t) for t in SAMPLE_TEXTS]
    unweighted = _style(rows)
    weighted = _style(rows, weights=[1.0] * len(rows))
    for key, value in unweighted.items():
        if key in ("weighted", "effective_sample"):
            continue
        assert weighted[key] == value, key
    assert weighted["weighted"] is False


def test_weighted_median_is_continuous_in_the_weights():
    """The estimator must not JUMP as weights approach equal.

    Found on the live archive: the crossing-point rule returns the first
    value past the halfway mark while statistics.median averages the two
    middle values, so weights a hair off equal moved his reply length target
    three words with no new information in the corpus. Midpoint
    interpolation removes the discontinuity and reproduces statistics.median
    exactly at equal weights, for odd and even samples alike.
    """
    import random
    from statistics import median

    from app.account_model import _wmedian

    random.seed(11)
    for _ in range(300):
        count = random.randint(1, 12)
        values = [float(random.randint(1, 40)) for _ in range(count)]
        assert _wmedian(values, [1.0] * count) == median(values)
        assert _wmedian(values, [3.7] * count) == median(values)
        nudged = [1.0 + random.uniform(-1e-9, 1e-9) for _ in range(count)]
        assert abs(_wmedian(values, nudged) - median(values)) < 1e-6


def test_weighted_median_still_follows_a_real_weight_difference():
    heavy_short = _wmedian_probe([10, 10, 10, 40, 40, 40], [1.0, 1.0, 1.0, 0.01, 0.01, 0.01])
    heavy_long = _wmedian_probe([10, 10, 10, 40, 40, 40], [0.01, 0.01, 0.01, 1.0, 1.0, 1.0])
    assert heavy_short == 10.0
    assert heavy_long == 40.0


def _wmedian_probe(values, weights):
    from app.account_model import _wmedian
    return _wmedian([float(v) for v in values], weights)


def test_pctile_equal_weights_match_unweighted():
    for values in ([3.0, 1.0, 4.0, 1.0, 5.0], [9.0, 2.0, 6.0, 5.0]):
        for q in (0.25, 0.5, 0.75):
            assert _pctile(values, q, [1.0] * len(values)) == _pctile(values, q)


def test_starling_verified_counts_half_for_voice_only():
    rows = [_row("mine", days_old=0), _row("machine", days_old=0, origin="starling_verified")]
    assert voice_weights(rows) == [1.0, 0.5]
    assert topic_weights(rows) == [1.0, 1.0]


def test_all_starling_corpus_measures_identically():
    """Normalization makes a uniformly discounted corpus indistinguishable —
    the anti-collapse pin: Starling can never drift a pure-Starling account's
    measured voice away from what is actually published."""
    plain = [_row(t, days_old=index) for index, t in enumerate(SAMPLE_TEXTS)]
    marked = [_row(t, days_old=index, origin="starling_verified")
              for index, t in enumerate(SAMPLE_TEXTS)]
    style_plain = _style(plain, weights=voice_weights(plain))
    style_marked = _style(marked, weights=voice_weights(marked))
    for key, value in style_plain.items():
        if key == "effective_sample":
            continue
        assert style_marked[key] == value, key


def test_unequal_weights_actually_move_a_rate():
    """Sanity that the weighted path is live: an old emoji habit fades when
    recent posts dropped it."""
    rows = [_row("old post \U0001F525", days_old=400) for _ in range(3)]
    rows += [_row(f"new plain post {i}", days_old=1) for i in range(3)]
    weights = recency_weights(rows)
    weighted = _style(rows, weights=weights)
    unweighted = _style(rows)
    assert weighted["emoji_rate"] < unweighted["emoji_rate"]
    assert weighted["weighted"] is True
