"""R-SORT-1 - the selector key, and the trap it is only safe beside.

The three off-lane topics in his last twelve - vLLM's funding round, AMD acquiring Taalas,
Google's search-box redesign - ALL pass `names_notable`. That is why notability could never do
the lane's job, and why this key demotes it to position 6.

The key is only correct WHOLE, so it is tested as a whole: each test fixes every term but one.
"""
from __future__ import annotations

import time

import pytest

from app import radar as R


def _c(topic, *, lane=None, age_h=None, source=True, **extra):
    published = 0 if age_h is None else int(time.time() - age_h * 3600)
    row = {"topic": topic, "lane": lane, "published_at": published,
           "evidence": "", "why": "", "evidence_sources": ["http://x"] if source else []}
    row.update(extra)
    return row


def _order(rows, monkeypatch, recent_lanes=()):
    """Sorts with THE PRODUCT'S OWN KEY - `radar._selector_key` - never a copy of it.

    An earlier draft of this file rebuilt the key inline, which would have tested a program
    nobody runs and passed happily while the real sort did something else.
    """
    monkeypatch.setattr(R, "recent_post_lanes", lambda: list(recent_lanes))
    return [c["topic"] for c in sorted(rows, key=R._selector_key)]


def test_age_tiers():
    assert R._age_tier(_c("x", age_h=1)) == 0
    assert R._age_tier(_c("x", age_h=12)) == 1
    assert R._age_tier(_c("x", age_h=48)) == 2
    assert R._age_tier(_c("x")) == 3, "unknown sits between <72h and older"
    assert R._age_tier(_c("x", age_h=200)) == 4


def test_unknown_age_is_neither_fresh_nor_stale():
    """UNKNOWN RANKS BELOW WHAT WE KNOW IS RECENT AND ABOVE WHAT WE KNOW IS OLD.

    Ranking it 0 would claim freshness we never observed; ranking it last would treat a
    missing field as evidence of staleness. Both are claims about an absence.
    """
    assert R._age_tier(_c("x", age_h=48)) < R._age_tier(_c("x")) < R._age_tier(_c("x", age_h=200))


def test_a_published_story_outranks_an_invented_label(monkeypatch):
    """TERM 1 STAYS FIRST. A lane-first key promotes a synthetic label over a real headline
    because both are frontier - reproducing the 2026-07-30 defect exactly."""
    invented = _c("Claude Opus 5 Value Proposition", lane="frontier models",
                  source=False, synthetic=True, age_h=1)
    real = _c("Qwen3.5 tops SWE-bench", lane="frontier models", age_h=40)
    assert _order([invented, real], monkeypatch)[0] == "Qwen3.5 tops SWE-bench"


def test_lane_outranks_notability(monkeypatch):
    """THE FIX, in one assertion. All three of his off-lane picks pass `names_notable`."""
    off_lane = _c("vLLM: all aboard open models", lane=None, age_h=2)
    on_lane = _c("Qwen3.5 tops SWE-bench", lane="frontier models", age_h=2)
    assert _order([off_lane, on_lane], monkeypatch)[0] == "Qwen3.5 tops SWE-bench"


def test_age_tier_outranks_notability_within_a_lane(monkeypatch):
    fresh = _c("Gemini 3.7 Flash ships", lane="frontier models", age_h=1)
    older = _c("Opus 5 benchmarks land", lane="frontier models", age_h=40)
    assert _order([older, fresh], monkeypatch)[0] == "Gemini 3.7 Flash ships"


def test_rotation_prefers_the_lane_he_did_not_just_post_in(monkeypatch):
    """A SORT TERM, NOT A FILTER. When a practice item exists it wins after a frontier post."""
    frontier = _c("Opus 5 benchmarks land", lane="frontier models", age_h=2)
    practice = _c("Cursor keeps crashing when I run two sessions", lane="vibe coding", age_h=2)
    order = _order([frontier, practice], monkeypatch, recent_lanes=["frontier models"])
    assert order[0].startswith("Cursor")


def test_rotation_never_empties_the_pool(monkeypatch):
    """When no practice item exists, frontier wins again and nothing breaks - the reason this
    is a sort term and not an alternation."""
    a = _c("Opus 5 benchmarks land", lane="frontier models", age_h=2)
    b = _c("Gemini 3.7 Flash ships", lane="frontier models", age_h=5)
    order = _order([a, b], monkeypatch, recent_lanes=["frontier models"])
    assert len(order) == 2 and order[0] == "Opus 5 benchmarks land"


def test_the_freshness_gate_catches_the_trap_the_key_would_otherwise_promote():
    """THE HARD DEPENDENCY, asserted rather than trusted.

    R-SORT-1: "do not ship key 2 above key 3 without the freshness GATE - lane-first without
    it is precisely how a 214-day-old 'Claude Code costs up to $200 a month' reaches the top
    of his pile: it is maximally on-lane."

    The key alone WOULD rank it highly. `_select`'s first pass calls `_evidence_is_old`, which
    condemns by the `published_at` field, so it never survives selection. If this assertion
    ever fails, the sort key has become a trap and must be reverted with it.
    """
    trap = _c("Claude Code costs up to $200 a month. Goose does the same for free",
              lane="vibe coding", age_h=214 * 24)
    assert R._evidence_is_old(trap) is True
    assert R._evidence_is_old(_c("Qwen3.5 tops SWE-bench", lane="frontier models",
                                 age_h=1)) is False


def test_recent_topics_reads_both_shapes(monkeypatch):
    """Twelve BARE STRINGS are in `job_state` right now; reading only the new dict shape
    would silently empty the rotation memory on upgrade."""
    stored = {}
    monkeypatch.setattr(R, "get_job_state", lambda key: stored.get(key, {}))
    monkeypatch.setattr(R, "set_job_state", lambda key, value: stored.__setitem__(key, value))

    stored[R._RECENT_TOPICS_KEY] = {"topics": ["an old bare string",
                                               {"topic": "a new one", "lane": "vibe coding"}]}
    assert R.recent_post_topics() == ["an old bare string", "a new one"]
    assert R.recent_post_lanes() == [None, "vibe coding"]

    R.record_post_topic("newest", "frontier models")
    assert R.recent_post_topics()[0] == "newest"
    assert R.recent_post_lanes()[0] == "frontier models"
    assert "an old bare string" in R.recent_post_topics(), "old entries must survive"


# T2 (2026-09-21): "breaking" ranks right after "has a source".

def test_a_breaking_story_outranks_a_fresher_on_lane_one(monkeypatch):
    """Several sources at once, or points rising fast: the moment to post is now."""
    breaking = _c("Kimi K3 lands on OpenRouter", lane=None, age_h=3, breaking=True, cluster_size=3)
    quiet = _c("Qwen3.5 tops SWE-bench", lane="frontier models", age_h=1)
    assert _order([quiet, breaking], monkeypatch)[0] == "Kimi K3 lands on OpenRouter"


def test_breaking_never_lifts_an_invented_label_over_a_real_story(monkeypatch):
    invented = _c("Claude Opus 5 Value Proposition", source=False, synthetic=True, age_h=1, breaking=True)
    real = _c("Qwen3.5 tops SWE-bench", lane="frontier models", age_h=40)
    assert _order([invented, real], monkeypatch)[0] == "Qwen3.5 tops SWE-bench"


def test_a_stale_story_is_not_breaking_however_fast_it_rises(monkeypatch):
    stale = _c("Claude Code costs up to $200 a month", lane="vibe coding", age_h=24 * 9, breaking=True)
    assert not R._is_breaking(stale)
    fresh = _c("Gemini 3.7 Flash ships", lane="frontier models", age_h=2)
    assert _order([stale, fresh], monkeypatch)[0] == "Gemini 3.7 Flash ships"
