"""S4: post-creation grounding reads in parallel.

`compute_radar` asked the five authored summaries one after another inside a 9 s budget, and
the topic audition read up to three articles one after another (a pick, then up to two
replacements), so a cold Create press paid for every miss in sequence. Both now run side by
side, the first real answer wins, and a short page cache means nothing is read twice.
"""
from __future__ import annotations

import threading
import time

import pytest

from app import cloud, radar, research

REAL = {"status": "used", "answer": "Example Labs shipped Model X today.", "sources": []}
APOLOGY = {"status": "unavailable", "answer": "Web search returned nothing usable right now.", "sources": []}


@pytest.fixture()
def quiet_radar(monkeypatch):
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(radar, "_rising_for_account", lambda *a, **k: [])
    monkeypatch.setattr(radar, "_pillars", lambda: ["model releases"])


def _research(answers: dict[str, dict], delay: float, seen: list):
    def run(query, llm=None, budget_seconds=12.0):
        seen.append((query, time.monotonic()))
        time.sleep(delay)
        return answers.get(query, APOLOGY)
    return run


def test_the_authored_summaries_are_asked_for_side_by_side(monkeypatch, quiet_radar):
    seen: list = []
    third = research.AUTHORED_QUERY_TEXTS[2]
    monkeypatch.setattr(research, "research", _research({third: REAL}, 0.3, seen))
    started = time.monotonic()
    result = radar.compute_radar(None, force=True, budget_seconds=5.0)
    elapsed = time.monotonic() - started
    assert result["web"]["answer"] == REAL["answer"]
    assert {query for query, _ in seen} <= set(research.AUTHORED_QUERY_TEXTS), "authored queries only"
    assert elapsed < 0.6, f"one after another would be 0.9 s or more; took {elapsed:.2f} s"
    starts = sorted(at for _, at in seen)
    assert starts[-1] - starts[0] < 0.2, "every summary was asked for at once"


def test_a_cached_radar_picks_what_the_old_loop_picked(monkeypatch, quiet_radar):
    seen: list = []
    first, third = research.AUTHORED_QUERY_TEXTS[0], research.AUTHORED_QUERY_TEXTS[2]
    other = {**REAL, "answer": "Another answer."}
    monkeypatch.setattr(research, "research", _research({first: REAL, third: other}, 0.0, seen))
    result = radar.compute_radar(None, force=True, budget_seconds=5.0)
    assert result["web"]["answer"] == REAL["answer"], "the earliest authored query still wins"


def _first_late_by(delay: float):
    first, third = research.AUTHORED_QUERY_TEXTS[0], research.AUTHORED_QUERY_TEXTS[2]
    other = {**REAL, "answer": "Another answer."}

    def run(query, llm=None, budget_seconds=12.0):
        if query == first:
            time.sleep(delay)
            return REAL
        return other if query == third else APOLOGY
    return run


def test_a_later_answer_that_comes_in_first_still_loses_to_the_earlier_query(monkeypatch, quiet_radar):
    """The race behind a flaky run on a busy machine: the third query answered before the first."""
    monkeypatch.setattr(research, "research", _first_late_by(0.05))
    result = radar.compute_radar(None, force=True, budget_seconds=5.0)
    assert result["web"]["answer"] == REAL["answer"]


def test_a_slow_earlier_query_costs_only_the_short_wait(monkeypatch, quiet_radar):
    monkeypatch.setattr(research, "research", _first_late_by(1.5))
    started = time.monotonic()
    result = radar.compute_radar(None, force=True, budget_seconds=5.0)
    elapsed = time.monotonic() - started
    assert result["web"]["answer"] == "Another answer."  # the first was too slow to wait for
    assert elapsed < 1.0, f"waited for the slow query: {elapsed:.2f} s"


def test_a_blind_day_carries_no_apology_as_signal(monkeypatch, quiet_radar):
    seen: list = []
    monkeypatch.setattr(research, "research", _research({}, 0.0, seen))
    result = radar.compute_radar(None, force=True, budget_seconds=2.0)
    assert not str((result.get("web") or {}).get("answer") or "")
    assert result.get("no_signal") is True


# --------------------------------------------------------------------------- pages


@pytest.fixture()
def pages(monkeypatch):
    """`/v1/fetch` as the world service answers it, slowly, recording every read."""
    reads: list[tuple[str, float]] = []
    lock = threading.Lock()
    bodies: dict[str, str] = {}

    def fetch(url, *, timeout_s=cloud.WORLD_TIMEOUT_S):
        with lock:
            reads.append((url, time.monotonic()))
        time.sleep(0.3)
        text = bodies.get(url, "")
        return {"final_url": url, "title": "Page", "text": text, "published_at": 0} if text else None

    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "_safe_to_fetch", lambda url: True)
    monkeypatch.setattr(cloud, "fetch", fetch)
    return reads, bodies


def test_a_page_read_twice_is_fetched_once(pages):
    reads, bodies = pages
    bodies["https://example.com/a"] = "Example Labs released Model X today with a 400k context window. " * 4
    first = research.fetch_page_text("https://example.com/a")
    second = research.fetch_page_text("https://example.com/a")
    assert first and first == second
    assert [url for url, _ in reads] == ["https://example.com/a"]


def test_concurrent_reads_of_one_page_share_a_fetch(pages):
    reads, bodies = pages
    bodies["https://example.com/b"] = "Cursor added background agents that open pull requests on their own. " * 3
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(research.fetch_page_text("https://example.com/b")))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(reads) == 1 and len(results) == 3 and all(results)


def test_a_failed_read_is_retried_after_a_short_while(pages, monkeypatch):
    reads, bodies = pages
    assert research.fetch_page_text("https://example.com/missing") is None
    assert research.fetch_page_text("https://example.com/missing") is None
    assert len(reads) == 1, "a failure is remembered briefly"
    monkeypatch.setattr(research, "PAGE_FAILURE_TTL_S", 0.0)
    research.fetch_page_text("https://example.com/missing")
    assert len(reads) == 2, "and retried once that has passed"


def test_the_audition_reads_its_pages_together(monkeypatch, pages):
    reads, bodies = pages
    # Two thin picks the selector tries first (breaking, so they sort ahead), then a meaty one.
    thin = [{"topic": title, "angle": "a", "why": "b", "breaking": True,
             "evidence_sources": [f"https://example.com/story-{n}"], "published_at": int(time.time()) - 600}
            for n, title in enumerate(("Anthropic launches Claude Code agent teams",
                                       "OpenAI ships Codex cloud tasks for teams"), start=1)]
    meaty = {"topic": "Cursor cuts Tab pricing for teams", "angle": "a", "why": "b",
             "evidence_sources": ["https://example.com/story-3"], "published_at": int(time.time()) - 3 * 3600}
    bodies["https://example.com/story-3"] = (
        "Cursor cut Tab pricing for teams by 40% to $10 per seat. The change ships today for every "
        "Business plan. Latency fell from 900ms to 410ms at p50 against the previous release. ") * 2
    monkeypatch.setattr(radar, "compute_radar", lambda *a, **k: {
        "emerging": [dict(c) for c in [*thin, meaty]], "trends": [], "web": {}})
    monkeypatch.setattr(radar, "topic_staleness", lambda t, r=None, **k: {"stale": False, "reason": ""})
    monkeypatch.setattr(radar, "_account_themes", lambda: ["model releases", "coding tools", "pricing"])
    monkeypatch.setattr(radar, "_web_candidates", lambda themes, limit=24, force=False: [])
    monkeypatch.setattr(radar, "recent_post_topics", lambda: [])
    monkeypatch.setattr(radar, "record_post_topic", lambda topic, lane=None: None)
    monkeypatch.setattr(radar, "set_job_state", lambda key, value: None)
    started = time.monotonic()
    grounding = radar.creation_grounding(object(), budget_seconds=5.0)
    elapsed = time.monotonic() - started
    urls = [url for url, _ in reads]
    assert sorted(urls) == sorted(set(urls)), "no page was read twice"
    assert len(urls) == 3
    starts = sorted(at for _, at in reads)
    assert starts[-1] - starts[0] < 0.2, "the three pages were asked for together"
    assert elapsed < 0.8, f"one after another would be 0.9 s or more; took {elapsed:.2f} s"
    assert grounding and "Cursor" in str(grounding["topic"])
