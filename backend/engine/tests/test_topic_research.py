"""Topic research in the account's own beat (API-native pass, 2026-09-22).

Background work only when the account is active and something is due, metered as background
(`cloud.search(..., background=True)`), with the engine's own interval, daily cap and backoff;
candidates ranked freshest first with their pages; queries built only from beat labels.
"""
from __future__ import annotations

import time

from app import cloud, niche, own_material, topic_research


def _niche(*texts: str, note: str = "") -> None:
    for text in texts:
        own_material.add("voice", text)
    if note:
        own_material.add("background", note)
    from app import storage
    storage.mark_corpus_dirty()
    niche.refresh()


def _answer(beat: str = "strength training") -> dict:
    return {"answer": "...", "searches": 1, "queries": [beat], "sources": [
        {"title": "New protein guidance for beginners", "url": "https://news.example.com/protein",
         "snippet": "A new review this week says most beginners need less protein timing and more total protein."},
        {"title": "Gyms report a January surge", "url": "https://daily.example.com/gyms",
         "snippet": "Example Daily reports gyms saw a record number of new members this week."},
    ]}


def test_research_waits_until_the_account_is_active_and_something_is_due(monkeypatch):
    _niche("Protein at breakfast is the easiest win.", "Beginners: three full-body days a week.",
           note="I coach strength training and nutrition for beginners.")
    calls = []
    monkeypatch.setattr(cloud, "search", lambda query, **kw: (calls.append((query, kw)) or (_answer(), "ok")))
    assert topic_research.run_if_due() == {"ran": False, "why": "inactive"}
    topic_research.mark_active()
    first = topic_research.run_if_due()
    assert first["ran"] and first["status"] == "ok" and first["found"] == 2
    [(query, kwargs)] = calls
    assert kwargs == {"purpose": "topics", "background": True}, "metered as background topic research"
    assert "breakfast" not in query and "three full-body" not in query, "only beat labels leave, never text"
    assert topic_research.run_if_due() == {"ran": False, "why": "recent"}


def test_the_daily_cap_and_a_refusal_back_off(monkeypatch):
    _niche("Protein at breakfast is the easiest win.", note="I coach strength training for beginners.")
    monkeypatch.setattr(cloud, "search", lambda query, **kw: (None, "capped"))
    topic_research.mark_active()
    assert topic_research.run_if_due()["status"] == "capped"
    now = time.time() + topic_research.RESEARCH_INTERVAL_S + 5
    assert topic_research.due(now)[2] == "backoff", "the service said the share is used up"
    state = topic_research._state()
    state.pop("backoff_until")
    state["runs"] = {topic_research._utc_day(now): topic_research.DAILY_RUNS}
    topic_research._save_state(state)
    assert topic_research.due(now)[2] == "daily_cap"


def test_candidates_are_freshest_first_with_their_pages_and_used_ones_skipped(monkeypatch):
    _niche("Protein at breakfast is the easiest win.", note="I coach strength training and nutrition.")
    now = time.time()
    topic_research._store(topic_research.findings_from(_answer()), "strength training", "background", now - 7200)
    fresh = {"sources": [{"title": "Protein study out today", "url": "https://news.example.com/creatine",
                          "snippet": "A study published today finds total protein matters more than timing for beginners."}]}
    topic_research._store(topic_research.findings_from(fresh), "strength training", "background", now - 60)
    monkeypatch.setattr(topic_research, "_pool_stories", lambda: [])
    options = topic_research.candidates(3, now=now)
    assert [item["url"] for item in options][0] == "https://news.example.com/creatine"
    assert all(item["url"].startswith("https://") and item["fact"] for item in options)
    topic_research.mark_used("https://news.example.com/creatine", now)
    assert "https://news.example.com/creatine" not in [item["url"] for item in topic_research.candidates(3, now=now)]
    text = topic_research.options_text(options)
    assert text.startswith("[1] ") and "https://" not in text, "links stay on the card, not in the post"


def test_the_shared_pool_counts_only_where_it_touches_the_beat(monkeypatch):
    _niche("Protein at breakfast is the easiest win.", "Deadlifts aren't bad for your back.",
           note="I coach strength training and nutrition for beginners.")
    stories = [
        {"title": "Open-weight coding model released", "url": "https://example.com/model",
         "summary": "A lab released weights for a coding model.", "created_at": int(time.time()) - 600},
        {"title": "New strength training guidance for beginners", "url": "https://example.com/strength",
         "summary": "Coaches recommend protein and full-body training.", "created_at": int(time.time()) - 900},
    ]
    monkeypatch.setattr(topic_research, "_pool_stories", lambda: stories)
    urls = [item["url"] for item in topic_research.candidates(3)]
    assert urls == ["https://example.com/strength"]


def test_a_press_search_is_a_press_search(monkeypatch):
    _niche("Protein at breakfast is the easiest win.", note="I coach strength training for beginners.")
    calls = []
    monkeypatch.setattr(cloud, "search", lambda query, **kw: (calls.append(kw) or (_answer(), "ok")))
    found, status = topic_research.press_search()
    assert status == "ok" and len(found) == 2
    assert calls == [{"purpose": "topics", "background": False}]
