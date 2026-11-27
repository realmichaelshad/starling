"""LEAK-1 — nothing derived from the account may reach an outbound research request.

The radar used to build an outbound search query out of the account's own content
pillars. With no declared pillars, `account_model.py` falls back to
`frequent_pillars` — the most frequent tokens of the account's OWN POSTS — so the
account's own words were joined into a live outbound request of the form

    latest <frequent tokens of the account's posts> news today what is trending

The hosted engine forwards only authored queries to the world service. This file is
the proof that the radar never hands anything else to the research transport.

WHY THE ASSERTION USES AN INJECTED SENTINEL RATHER THAN A PINNED STRING
    Pinning an observed string would rot. `frequent_pillars` is recomputed from the
    posts, so the top tokens drift, and a pinned assertion would go green while the
    leak continued with different words. The invariant is not "that sentence is
    absent" but "nothing DERIVED FROM THE ACCOUNT is present", so we inject a pillar
    no corpus could produce and assert it never appears.

WHY THE PATCH TARGET IS `app.research` AND NOT `app.radar`
    `radar.py` imports research INSIDE the function:
        from .research import research as run_research, research_enabled
    A function-local import resolves against the source module at call time, so
    patching `radar.run_research` does NOTHING and the test would pass without
    exercising anything. On this control a false green is worse than no control.
"""
from __future__ import annotations

import pytest

from app import radar as radar_mod
from app import research as research_mod

# Cannot arise from any archive, from _THEMES, or from any authored query.
SENTINEL = "zzsecretcodename"


@pytest.fixture()
def captured_wire(monkeypatch):
    """Every query string that reaches the research transport, in order."""
    seen: list[str] = []

    def _fake_research(query, llm=None, budget_seconds=12.0):
        seen.append(str(query))
        return {"answer": "", "sources": [], "status": "unavailable"}

    # The conftest sets STARLING_RESEARCH=off so tests never reach the web. The leak path is
    # behind `research_enabled()`, so it must be switched ON deliberately here or this test
    # would pass by never running the code it is about.
    monkeypatch.setattr(research_mod, "research", _fake_research)
    monkeypatch.setattr(research_mod, "research_enabled", lambda: True)
    return seen


def _force_pillars(monkeypatch, pillars: list[str]) -> None:
    """Drive `_pillars()` the way the product does — through the account model."""
    monkeypatch.setattr(
        radar_mod, "infer_account_model",
        lambda *a, **k: {"content_pillars": pillars, "audience_interest_signals": []})


def test_account_pillars_never_reach_the_search_wire(monkeypatch, captured_wire):
    """The defect, as a test."""
    _force_pillars(monkeypatch, [SENTINEL, "second secret", "third secret"])

    radar_mod.compute_radar(None, force=True, budget_seconds=1.0)

    leaked = [q for q in captured_wire if SENTINEL in q.lower()]
    assert not leaked, (
        "ACCOUNT-DERIVED TEXT REACHED THE SEARCH WIRE.\n"
        f"  outbound strings observed : {captured_wire!r}\n"
        f"  carrying the sentinel     : {leaked!r}\n"
        "  source: radar.py builds the query from _pillars()."
    )


def test_every_outbound_query_is_authored(monkeypatch, captured_wire):
    """Absence of the sentinel is necessary, not sufficient.

    A query could avoid the sentinel and still be assembled from the account's words.
    The real guarantee is MEMBERSHIP: every string on the wire is one this repo
    authored (`research.AUTHORED_QUERY_TEXTS`), which is also the only kind of query
    the research module forwards to the world service.
    """
    _force_pillars(monkeypatch, [SENTINEL, "second secret"])
    radar_mod.compute_radar(None, force=True, budget_seconds=1.0)

    assert captured_wire, "the radar never asked for research, so this proves nothing"
    permitted = set(research_mod.AUTHORED_QUERY_TEXTS)
    unauthored = [q for q in captured_wire if q not in permitted]
    assert not unauthored, (
        "AN OUTBOUND QUERY WAS NOT ONE OF OURS.\n"
        f"  unauthored : {unauthored!r}\n"
        f"  permitted  : {sorted(permitted)!r}"
    )


def test_the_detector_can_fire(captured_wire):
    """NEGATIVE CONTROL ON THE CONTROL.

    A guard nobody has watched fail is decoration. Hand the capture fixture the exact shape
    of the old leak and assert the detector sees it — so a refactor that stops calling
    research at all cannot turn this file green while proving nothing.
    """
    research_mod.research(f"latest {SENTINEL}, first time news today what is trending", None)

    assert any(SENTINEL in q.lower() for q in captured_wire), \
        "the capture fixture is broken — it did not see a string handed straight to it"
