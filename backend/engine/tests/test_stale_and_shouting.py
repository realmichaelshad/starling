"""The three faults in one live draft (2026-07-30 03:41):

    claude opus 5 cost efficiency is HALF PRICE for FABLE performance.

He named all three: too short, words shouted for no reason, and old news
about the one subject this account has always over-posted. The third is the
one he called out by name, as the kind of mistake that had already cost the
account badly and must not happen again, and it was real: the topic was not a
story at all but a label a model invented from trend data ("Claude Opus 5 Cost
Efficiency"), its evidence was a search snippet stamped "6 days ago", and the
watchlist tier added hours earlier had promoted it above every real headline
in the pool.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import radar                                     # noqa: E402
from app.intelligence import normalise_shouting           # noqa: E402


def test_shouted_words_are_restored_to_the_evidence_casing():
    evidence = ("Claude Opus 5 provides Fable-level performance at half the "
                "price of Opus 4.8.")
    draft = "claude opus 5 cost efficiency is HALF PRICE for FABLE performance."
    fixed = normalise_shouting(draft, evidence)
    assert "HALF PRICE" not in fixed
    assert "FABLE" not in fixed
    assert "Fable" in fixed, "the evidence spells the model name Fable"


def test_real_initialisms_are_never_touched():
    for keep in ("The API is free now.", "GPU memory is the wall.",
                 "AI agents ship code.", "MCP cuts payloads by 37%.",
                 "SWE-bench scores moved."):
        assert normalise_shouting(keep, "") == keep, keep


def test_evidence_that_states_its_own_age_is_stale():
    assert radar._evidence_is_old(
        {"topic": "Claude Opus 5 Cost Efficiency",
         "evidence": "Claude Opus 5 Cost Efficiency 6 days ago · Performance "
                     "and cost-effectiveness"})
    assert radar._evidence_is_old({"evidence": "3 weeks ago · the launch"})
    # Today's news is not old, and a number that is not an age is not a date.
    assert not radar._evidence_is_old({"evidence": "2 days ago · shipped"})
    assert not radar._evidence_is_old({"evidence": "Scores 6 points higher"})
    assert not radar._evidence_is_old({"evidence": "Runs on 14B parameters"})


def test_a_real_story_outranks_an_invented_label(monkeypatch):
    """An invented trend label about the archive's biggest name used to beat
    a sourced headline, because it has no date for staleness to judge."""
    invented = {"topic": "Claude Opus 5 Cost Efficiency", "angle": "a",
                "why": "trending"}                        # no source, no date
    real = {"topic": "Anthropic launches Cowork, a Claude Desktop agent",
            "angle": "a", "why": "Published today.",
            "evidence": "Anthropic launched Cowork today.",
            "evidence_sources": ["https://example.com/cowork"]}
    monkeypatch.setattr(radar, "compute_radar",
                        lambda *a, **k: {"emerging": [invented, real],
                                         "trends": [], "web": {}})
    monkeypatch.setattr(radar, "topic_staleness", lambda t, r: {"stale": False, "reason": ""})
    monkeypatch.setattr(radar, "_account_themes",
                        lambda: ["model releases", "coding tools", "pricing"])
    monkeypatch.setattr(radar, "_web_candidates", lambda themes, limit=12: [])
    monkeypatch.setattr(radar, "recent_post_topics", lambda: [])
    monkeypatch.setattr(radar, "record_post_topic", lambda topic, lane=None: None)

    grounding = radar.creation_grounding(object(), budget_seconds=0.1)
    assert grounding
    assert "Cowork" in str(grounding["topic"]), (
        f"an invented label beat a sourced story: {grounding['topic']!r}")
