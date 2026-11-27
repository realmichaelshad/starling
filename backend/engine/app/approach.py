"""Does the account still write like its measured baseline, or has it moved?

His requirement, 2026-07-31: "let's say my account took a different approach —
it wasn't just AI and it is something else — then we would hop on that
different approach." The archive import is a snapshot of who the account WAS;
the watcher keeps feeding who it IS. This module is the judge between them:
it splits the dated corpus into a recent window and a baseline and asks one
question — topically and stylistically, is the recent window the same
account?

Three states, each with one visible consequence:
- ``stable``:   nothing changes; the default 90-day half-life applies.
- ``watching``: named in the profile gaps, changes nothing else. Style-only
  movement can never go further than this — the product ask is about the
  account's DIRECTION, and style alone is weather.
- ``shifted``:  a confirmed topical move. Voice and pillar learning drop to
  the 30-day half-life (recency.FAST_HALF_LIFE_DAYS) so the new approach
  takes over in weeks, not quarters.

Calibration law (same as every gate here): the thresholds were probed
against his real archive (tools/approach_probe.py) and the current account
must read ``stable`` with margin. A threshold that misfires moves AWAY from
firing — never the fixture toward passing.

No scheduler: results are cached like personal_topic_profile (TTL + corpus
version) and computed lazily wherever the profile is already being built.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from .account_model import _style_metrics, _tokens
from .recency import DEFAULT_HALF_LIFE_DAYS, FAST_HALF_LIFE_DAYS, row_when

STABLE = "stable"
WATCHING = "watching"
SHIFTED = "shifted"

# The recent window: everything inside 60 days, widened to the newest 20
# posts when the account posts sparsely — whichever set is larger.
RECENT_WINDOW_POSTS = 20
RECENT_WINDOW_DAYS = 60
# Below these the honest answer is "not enough evidence", never a verdict.
MIN_RECENT_POSTS = 8
MIN_BASELINE_POSTS = 12

# Topical thresholds, MEASURED before frozen — never guessed. Four cases,
# 2026-07-31 (tools/approach_probe.py prints the live row on demand):
#
#                          jaccard   cosine
#   his live archive         0.108    0.847   must read stable
#   his fixture archive      0.091    0.780   must read stable
#   style-only torture       0.074    0.692   may watch, must NOT shift
#   real pivot (AI->food)    0.021    0.629   must shift
#
# Two lessons are frozen in these numbers. First, the hashed 96-dim embedder
# keeps even disjoint topics near 0.63, so a cosine line belongs at 0.70 —
# an intuition like 0.45 would never fire. Second, cosine ALONE cannot carry
# the decision: the style-only case sits at 0.692, inside any cosine line low
# enough to catch a real pivot. The shared vocabulary emptying is what
# separates them (0.021 vs 0.074, a 3.5x gap), so a confirmed shift needs
# BOTH signals and jaccard is the discriminating one.
#
# These moved once already: expanding account_model.STOPWORDS (dates and
# hedges, see subject.builder_fit) thinned every token set and dropped every
# jaccard by ~25%. If tokenization changes again, RE-MEASURE this table
# rather than nudging a constant.
CENTROID_COSINE_SHIFT = 0.70
CENTROID_COSINE_WATCH = 0.75
TOKEN_JACCARD_SHIFT = 0.05
# Fallback-only watch line, used when no embedder is available. Below his
# quietest stable reading (0.091) so a healthy account never reads nervous.
TOKEN_JACCARD_WATCH = 0.065

# A style delta outside its band is one vote; two votes make the style
# signal. Ratio for length (his medians run 18-30, absolute bands would
# misfire), absolute deltas for the habit rates. Bands sit clear of the
# deltas his own stable halves measure (words_ratio 0.467, multiline 0.479,
# lowercase 0.304 — the draft-size drift already being fixed elsewhere),
# because a band his own archive trips is a false positive by law.
STYLE_DELTA_BANDS = {
    "median_words_ratio": 0.60,
    "multiline_rate": 0.55,
    "emoji_rate": 0.30,
    "lowercase_start_rate": 0.45,
    "question_rate": 0.35,
}

_TRANSITIONS_KEY = "approach_transitions"
_CACHE_TTL_S = 900.0
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"key": None, "state": None, "built_at": 0.0}


def _embed(texts: list[str]) -> np.ndarray | None:
    try:
        from .embeddings import LocalOnnxEmbedder
        embedder = LocalOnnxEmbedder()
        if embedder.available:
            return embedder.encode(texts)
    except Exception:                                    # noqa: BLE001
        pass
    return None


def _centroid(matrix: np.ndarray) -> np.ndarray:
    mean = matrix.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    return mean / norm if norm > 0 else mean


def _window_tokens(texts: list[str]) -> set[str]:
    tokens: set[str] = set()
    for text in texts:
        tokens.update(_tokens(text))
    return tokens


def _style_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [str(row.get("text") or "") for row in rows]
    return _style_metrics(rows, texts, [len(t.split()) for t in texts], len(texts))


def _style_deltas(recent: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float]:
    deltas: dict[str, float] = {}
    base_words = float(baseline.get("median_words") or 0)
    recent_words = float(recent.get("median_words") or 0)
    deltas["median_words_ratio"] = round(abs(recent_words - base_words) / max(1.0, base_words), 3)
    for key in ("multiline_rate", "emoji_rate", "lowercase_start_rate", "question_rate"):
        deltas[key] = round(abs(float(recent.get(key) or 0) - float(baseline.get(key) or 0)), 3)
    return deltas


def _split_windows(rows: list[dict[str, Any]], now: datetime) -> tuple[list[dict], list[dict]]:
    dated = [(when, row) for row in rows if (when := row_when(row)) is not None]
    dated.sort(key=lambda pair: pair[0], reverse=True)
    cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)
    in_window = [pair for pair in dated if pair[0] >= cutoff]
    if len(in_window) >= RECENT_WINDOW_POSTS:
        # A dense account can have its ENTIRE corpus inside 60 days (the live
        # archive does: 46/46 on 2026-07-31). The recent window must never
        # swallow the baseline it is judged against, so it is capped at the
        # newest half — for a dense account "before" is simply its older half.
        cap = max(RECENT_WINDOW_POSTS, len(dated) // 2)
        recent = in_window[:cap]
    elif len(in_window) >= MIN_RECENT_POSTS:
        # Enough genuinely recent activity to judge on its own. Padding this
        # window to 20 with older posts would dilute a real two-week pivot
        # back below its own thresholds — the recent window must be RECENT.
        recent = in_window
    else:
        recent = dated[:RECENT_WINDOW_POSTS]
    baseline = dated[len(recent):]
    return [row for _, row in recent], [row for _, row in baseline]


def approach_state(rows: list[dict[str, Any]] | None = None, *,
                   now: datetime | None = None) -> dict[str, Any]:
    """The account's current approach verdict, with every signal named.

    Explicit ``rows`` (tests, probes) bypass the cache and never persist
    transition breadcrumbs — the live no-args path does both.
    """
    live = rows is None
    now = now or datetime.now(timezone.utc)
    if live:
        from .storage import corpus_version, performance_records
        # Check the cheap thing BEFORE the expensive one. `len(rows)` used to be
        # in the key, which meant the corpus-wide load ran even on a hit.
        # `corpus_version()` alone is sufficient: every writer that can change
        # the row count bumps it (`remove_posts_from_archive_sources` was the
        # one exception and now does too).
        cache_key = (corpus_version(),)
        monotonic = time.monotonic()
        with _CACHE_LOCK:
            if (_CACHE["state"] is not None and _CACHE["key"] == cache_key
                    and monotonic - _CACHE["built_at"] < _CACHE_TTL_S):
                return _CACHE["state"]
        rows = performance_records()

    state = _evaluate(rows, now)
    if live:
        _record_transition(state, now)
        with _CACHE_LOCK:
            _CACHE.update(key=cache_key, state=state, built_at=time.monotonic())
    return state


def _evaluate(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    recent_rows, baseline_rows = _split_windows(rows, now)
    signals: dict[str, Any] = {"centroid_cosine": None, "token_jaccard": None,
                               "style_deltas": {}, "style_shifted": False,
                               "topical_shifted": False, "topical_watch": False}
    base = {"recent_posts": len(recent_rows), "baseline_posts": len(baseline_rows),
            "signals": signals, "since": None,
            "half_life_days": DEFAULT_HALF_LIFE_DAYS}
    if len(recent_rows) < MIN_RECENT_POSTS:
        return {**base, "state": STABLE,
                "note": (f"Only {len(recent_rows)} dated recent posts — a shift needs "
                         f"{MIN_RECENT_POSTS} to be judged, so the measured baseline stands.")}
    if len(baseline_rows) < MIN_BASELINE_POSTS:
        return {**base, "state": STABLE,
                "note": ("The measured history is still too small to have a baseline "
                         "to drift from; everything recent IS the baseline.")}

    recent_texts = [str(row.get("text") or "") for row in recent_rows]
    baseline_texts = [str(row.get("text") or "") for row in baseline_rows]

    recent_tokens = _window_tokens(recent_texts)
    baseline_tokens = _window_tokens(baseline_texts)
    union = recent_tokens | baseline_tokens
    jaccard = round(len(recent_tokens & baseline_tokens) / max(1, len(union)), 3)
    signals["token_jaccard"] = jaccard

    cosine = None
    embeddings = _embed(recent_texts + baseline_texts)
    if embeddings is not None:
        recent_centroid = _centroid(embeddings[:len(recent_texts)])
        baseline_centroid = _centroid(embeddings[len(recent_texts):])
        cosine = round(float(recent_centroid @ baseline_centroid), 3)
        signals["centroid_cosine"] = cosine

    if cosine is not None:
        # Jaccard corroborates: a real topical pivot empties the shared
        # vocabulary too. Requiring both keeps one noisy signal from firing,
        # and jaccard never solos the watch state (see threshold comment).
        topical_shifted = cosine < CENTROID_COSINE_SHIFT and jaccard < TOKEN_JACCARD_SHIFT
        topical_watch = cosine < CENTROID_COSINE_WATCH
    else:
        topical_shifted = jaccard < TOKEN_JACCARD_SHIFT
        topical_watch = jaccard < TOKEN_JACCARD_WATCH
    signals["topical_shifted"] = topical_shifted
    signals["topical_watch"] = topical_watch

    deltas = _style_deltas(_style_of(recent_rows), _style_of(baseline_rows))
    signals["style_deltas"] = deltas
    out_of_band = sum(1 for key, band in STYLE_DELTA_BANDS.items()
                      if float(deltas.get(key) or 0) > band)
    signals["style_shifted"] = out_of_band >= 2

    if topical_shifted:
        oldest_recent = min((when for row in recent_rows
                             if (when := row_when(row)) is not None), default=None)
        return {**base, "state": SHIFTED,
                "half_life_days": FAST_HALF_LIFE_DAYS,
                "since": oldest_recent.isoformat() if oldest_recent else None,
                "note": ("The recent posts have moved off the measured baseline "
                         "topically — voice and topic learning now follow the new "
                         f"direction (half-life {FAST_HALF_LIFE_DAYS:.0f}d instead of "
                         f"{DEFAULT_HALF_LIFE_DAYS:.0f}d).")}
    if signals["style_shifted"] or topical_watch:
        moved = [key for key, band in STYLE_DELTA_BANDS.items()
                 if float(deltas.get(key) or 0) > band]
        what = ("topic overlap is thinning" if topical_watch else
                "style moved: " + ", ".join(moved))
        return {**base, "state": WATCHING,
                "note": (f"Recent posts are moving ({what}) but not confirmed as a "
                         "new approach; nothing changes until the topical shift is real.")}
    return {**base, "state": STABLE,
            "note": "Recent posts match the measured baseline."}


def _record_transition(state: dict[str, Any], now: datetime) -> None:
    try:
        from .storage import get_job_state, set_job_state
        stored = get_job_state(_TRANSITIONS_KEY)
        history = list(stored.get("transitions", []))
        last = history[-1]["to"] if history else STABLE
        if last != state["state"]:
            history.append({"at": now.isoformat(), "from": last, "to": state["state"],
                            "since": state.get("since")})
            set_job_state(_TRANSITIONS_KEY, {"transitions": history[-5:]})
    except Exception:                                    # noqa: BLE001
        pass


def transitions() -> list[dict[str, Any]]:
    try:
        from .storage import get_job_state
        return list(get_job_state(_TRANSITIONS_KEY).get("transitions", []))
    except Exception:                                    # noqa: BLE001
        return []


def current_half_life(state: dict[str, Any] | None = None) -> float:
    """The half-life the voice/pillar learners should use right now."""
    try:
        state = state if state is not None else approach_state()
        return float(state.get("half_life_days") or DEFAULT_HALF_LIFE_DAYS)
    except Exception:                                    # noqa: BLE001
        return DEFAULT_HALF_LIFE_DAYS


def reset_approach_cache() -> None:
    """Test hook, mirrors reset_topic_profile_cache."""
    with _CACHE_LOCK:
        _CACHE.update(key=None, state=None, built_at=0.0)
