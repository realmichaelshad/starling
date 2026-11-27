"""The topic audition: relevant, enough to say, interesting — or wait.

His diagnosis (2026-07-30): the trouble with posts is the topics. Drafts
with real material behind them held up; drafts on whatever the pool held did
not. These tests pin the formula that closes the gap:

  * a bare headline is not material: the floor needs something to say beyond
    the label itself;
  * a thin pick loses to a meatier candidate that exists NOW — the 404'd
    source that used to ship "honest headline-only";
  * a stranger's project triggers one forced look at the news before it may
    settle (the Supapool/UnUI presses of 2026-07-29);
  * the LAST usable topic still ships, flagged — a gate that silently kills
    every candidate is the one outcome he has ruled out;
  * a pool with nothing usable at all becomes a wait WITH REASONS.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import radar                                          # noqa: E402
from app.topic_audition import (MATERIAL_FLOOR, audit_topic,   # noqa: E402
                                has_prospect, material_beyond_label)

THEMES = ["model releases", "coding tools", "pricing", "performance",
          "benchmarks"]


def test_a_bare_headline_is_not_material():
    """The starvation case: a 404'd source or a synthetic trend label leaves
    the writer one sentence to have an opinion about."""
    title = "Claude Opus 5 Cost Efficiency"
    assert material_beyond_label(title, title) == 0
    for evidence in ("", title, f"{title}."):
        verdict = audit_topic(title, evidence)
        assert not verdict["substantial"]
        assert "thin_material" in verdict["weaknesses"]


def test_prospects_are_material_or_an_unread_source():
    thin = {"topic": "A", "evidence": "", "evidence_sources": []}
    sourced = {"topic": "B", "evidence": "", "evidence_sources": ["http://s"]}
    meaty = {"topic": "C", "evidence": "x" * (MATERIAL_FLOOR + 10)}
    assert not has_prospect([thin])
    assert has_prospect([thin, sourced])
    assert has_prospect([thin, meaty])


def _meaty(topic: str, source: str = "http://example.com/story") -> dict:
    return {"topic": topic, "angle": "a", "why": "b",
            "evidence": (f"{topic}. It ships today with a 40% price cut to "
                         "$10 per million tokens, a 256k context window, and "
                         "the team published latency numbers against the "
                         "previous release: p50 down from 900ms to 410ms."),
            "evidence_sources": [source]}


def _thin(topic: str, source: str | None = "http://example.com/x") -> dict:
    candidate = {"topic": topic, "angle": "a", "why": "b"}
    if source:
        candidate["evidence_sources"] = [source]
    return candidate


def _wire(monkeypatch, pool, memory=None, web=None, stale=None):
    memory = list(memory or [])
    monkeypatch.setattr(radar, "compute_radar",
                        lambda *a, **k: {"emerging": [dict(c) for c in pool],
                                         "trends": [], "web": {}})
    monkeypatch.setattr(radar, "topic_staleness",
                        stale or (lambda t, r=None, **k: {"stale": False,
                                                          "reason": ""}))
    monkeypatch.setattr(radar, "_account_themes", lambda: THEMES)
    monkeypatch.setattr(radar, "_web_candidates",
                        web or (lambda themes, limit=24, force=False: []))
    monkeypatch.setattr(radar, "recent_post_topics", lambda: list(memory))
    monkeypatch.setattr(radar, "record_post_topic",
                        lambda topic, lane=None: memory.insert(0, topic))
    trail: list[dict] = []
    monkeypatch.setattr(radar, "set_job_state",
                        lambda key, value: trail.append({key: value}))
    # The audition judges what the ARTICLE yields; in here the fetch yields
    # nothing, so a candidate's own evidence decides.
    import app.source_facts as source_facts
    monkeypatch.setattr(source_facts, "evidence_block",
                        lambda title, url=None: "")
    return trail


def test_a_thin_topic_loses_to_a_meaty_one(monkeypatch):
    """The 404'd-source case: the picked story's page yields nothing, and a
    candidate with real material is sitting right behind it. Before the
    audition this shipped headline-only; now the meatier topic is the post."""
    thin = _thin("Anthropic launches Cowork, a Claude Desktop agent")
    meaty = _meaty("Cursor cuts Tab pricing for teams")
    _wire(monkeypatch, [thin, meaty])

    grounding = radar.creation_grounding(object(), budget_seconds=0.1)
    assert grounding, "the selector went silent"
    assert "Cursor" in str(grounding["topic"]), grounding["topic"]
    assert grounding.get("thin_material") is False
    rejected = [r["topic"] for r in (grounding.get("audit") or {}).get(
        "rejected", [])]
    assert any("Cowork" in topic for topic in rejected), (
        "the thin pick was not recorded as auditioned away")


def test_the_last_thin_topic_ships_flagged_never_silence(monkeypatch):
    """No prospects left: the weak survivor ships WITH the weakness named,
    because a flagged draft he can judge beats an empty composer."""
    only = _thin("Anthropic launches Cowork, a Claude Desktop agent")
    _wire(monkeypatch, [only])

    grounding = radar.creation_grounding(object(), budget_seconds=0.1)
    assert grounding, "the audition silenced the last usable topic"
    assert "Cowork" in str(grounding["topic"])
    assert grounding.get("thin_material") is True
    assert "thin_material" in (grounding.get("audit") or {}).get(
        "weaknesses", [])


def test_a_stranger_triggers_one_refresh_for_real_news(monkeypatch):
    """2026-07-29, presses 2 and 3: Supapool and UnUI went out while the
    day's real news sat one forced refresh away. A pick naming nothing he
    would recognise now asks the web once for something notable with
    material before it may settle."""
    stranger = _thin("UnUI - Help coding agents make better design decisions",
                     source=None)
    fresh = _meaty("Anthropic ships Claude Code governance controls")

    def web(themes, limit=24, force=False):
        return [dict(fresh)] if force else []

    _wire(monkeypatch, [stranger], web=web)

    grounding = radar.creation_grounding(object(), budget_seconds=5.0)
    assert grounding, "the selector went silent"
    assert "Claude Code" in str(grounding["topic"]), (
        f"settled for the stranger instead of refreshing: "
        f"{grounding['topic']!r}")
    assert grounding.get("off_lane") is False
    assert grounding.get("thin_material") is False


# ------------------------------------------------------------- card paths
# A verdict nobody surfaces is a silent gate by another route, so both card
# paths are proven END TO END through /browser/assist, not by trusting the
# flag on the grounding dict.

class _FakeModel:
    text_model = "qwen3:14b"

    def status(self):
        return {"available": True, "provider": "starling-services",
                "text_model": "qwen3:14b"}


def _card_client(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, security
    client = TestClient(main.app)
    token = security.ensure_token()
    monkeypatch.setattr(main, "ModelClient", _FakeModel)
    return client, {"Authorization": f"Bearer {token}"}


# RETIRED (API-native pass, 2026-09-22): test_a_thin_material_draft_carries_the_wait_card: Create post no longer runs the radar audition; its stories are the account's own beat.


# RETIRED (API-native pass, 2026-09-22): test_the_no_signal_wait_names_what_the_audition_saw: same: with nothing found Create post writes from the beat and says so (no dead end).


def test_an_exhausted_pool_says_wait_with_reasons(monkeypatch):
    """Nothing usable at all -> None, and the trail says what was seen, so
    the composer's wait card can name it instead of shrugging."""
    pool = [_thin("Cursor pricing change"), _thin("Goose agent benchmarks")]
    trail = _wire(monkeypatch, pool,
                  stale=lambda t, r=None, **k: {"stale": True,
                                                "reason": "cold trend"})
    import app.release_radar as release_radar
    monkeypatch.setattr(release_radar, "recent_releases",
                        lambda *a, **k: [])

    grounding = radar.creation_grounding(object(), budget_seconds=0.1)
    assert grounding is None
    recorded = [entry["creation_audit_last"] for entry in trail
                if "creation_audit_last" in entry]
    assert recorded, "the audition left no trail for the wait card"
    last = recorded[-1]
    assert last["accepted"] is None
    assert last["pool_size"] == 2
    assert last["skipped"], "the wait card has nothing to say about the pool"
