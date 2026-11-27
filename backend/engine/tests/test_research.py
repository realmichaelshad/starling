"""Web material through the world service: the off switch, no general search,
page reads through `/v1/fetch`, and summaries for authored queries only."""
from __future__ import annotations

import pytest

from app import cloud, research
from app.research import research as run_research


@pytest.fixture(autouse=True)
def proxy_mode(monkeypatch):
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    yield


def test_the_off_switch_stops_every_request(monkeypatch):
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    monkeypatch.setattr(cloud, "fetch", lambda url: pytest.fail("a page was fetched with research off"))
    monkeypatch.setattr(cloud, "summary", lambda query_id: pytest.fail("a summary was requested with research off"))
    assert research.research_enabled() is False
    assert research.fetch_page_text("https://example.com/story") is None
    result = run_research("AI model release")
    assert result["status"] == "unavailable"
    assert "off" in result["answer"].lower()


@pytest.mark.parametrize("value, enabled", [
    ("proxy", True), ("on", True), ("", True), ("off", False), ("0", False), ("false", False), ("OFF", False),
])
def test_only_an_explicit_off_disables_research(monkeypatch, value, enabled):
    monkeypatch.setenv("STARLING_RESEARCH", value)
    assert research.research_enabled() is enabled


def test_general_web_search_is_not_available():
    assert research.search_web("latest model release") == []


def test_private_targets_are_refused_before_the_service_is_asked(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(cloud, "fetch", lambda url: asked.append(url) or {"text": "x " * 200})
    for url in ("http://localhost/x", "http://127.0.0.1/x", "http://10.0.0.5/x",
                "http://169.254.1.1/x", "http://192.168.1.1/x", "ftp://example.com/x",
                "http://user:pass@example.com/x", "http://service.local/x"):
        assert research.fetch_page_text(url) is None, url
    assert asked == []


def test_a_page_is_read_through_the_world_service(monkeypatch):
    body = "Useful public story with enough words to be returned by the engine. " * 4
    asked: list[str] = []

    def fake_fetch(url):
        asked.append(url)
        return {"final_url": url, "title": "The story", "text": body, "published_at": 0}

    monkeypatch.setattr(cloud, "fetch", fake_fetch)
    assert research.fetch_page_text("https://example.com/story") == body.strip()
    preview = research.fetch_page_preview("https://example.com/story")
    assert preview == {"text": body.strip(), "image": None, "title": "The story", "description": None}
    assert research.fetch_page_text_and_image("https://example.com/story") == (body.strip(), None)
    # S4 (2026-09-22): a page read is cached briefly, so three reads of one page are one request.
    assert asked == ["https://example.com/story"]


def test_thin_or_missing_pages_are_unreadable(monkeypatch):
    monkeypatch.setattr(cloud, "fetch", lambda url: None)
    assert research.fetch_page_text("https://example.com/a") is None
    monkeypatch.setattr(cloud, "fetch", lambda url: {"text": "too short"})
    assert research.fetch_page_text("https://example.com/b") is None


def test_a_long_page_is_cut_at_a_block_boundary(monkeypatch):
    block = "A paragraph of real article text that keeps going for a while. " * 3
    body = "\n".join(block.strip() for _ in range(80))
    monkeypatch.setattr(cloud, "fetch", lambda url: {"text": body})
    text = research.fetch_page_text("https://example.com/long")
    assert text is not None
    assert len(text) <= research.PAGE_TEXT_LIMIT
    assert text.endswith("while.")


def test_an_authored_query_is_summarised_once_and_then_cached(monkeypatch):
    calls: list[str] = []

    def fake_summary(query_id):
        calls.append(query_id)
        return {"answer": "Two labs shipped models this week [1].",
                "sources": [{"title": "Release notes", "url": "https://example.com/a",
                             "published_at": 1757790000, "snippet": "Notes"},
                            {"title": "Not a link", "url": "javascript:alert(1)"}]}

    monkeypatch.setattr(cloud, "summary", fake_summary)
    first = run_research("AI model release")
    assert first["status"] == "used"
    assert first["answer"] == "Two labs shipped models this week [1]."
    assert [source["url"] for source in first["sources"]] == ["https://example.com/a"]
    assert calls == ["ai-model-releases"]

    second = run_research("  ai MODEL release ")
    assert second["status"] == "cached"
    assert calls == ["ai-model-releases"], "a cached summary must not ask the service again"


def test_free_text_never_reaches_the_service(monkeypatch):
    monkeypatch.setattr(cloud, "summary", lambda query_id: pytest.fail("free text was sent"))
    result = run_research("what did my competitor post about their pricing yesterday")
    assert result["status"] == "unavailable"
    assert "unverified" in result["answer"].lower()


def test_an_unavailable_summary_degrades_honestly(monkeypatch):
    monkeypatch.setattr(cloud, "summary", lambda query_id: None)
    result = run_research("coding agent")
    assert result["status"] == "unavailable"
    assert "unverified" in result["answer"].lower()


def test_the_authored_queries_are_craft_not_a_person():
    """The ids are shared with the world service; the texts must stay authored."""
    assert research.AUTHORED_QUERY_TEXTS == tuple(research.AUTHORED_QUERIES.values())
    for query_id, text in research.AUTHORED_QUERIES.items():
        assert query_id == query_id.lower() and " " not in query_id
        assert "@" not in text
        assert text.strip() == text and 2 <= len(text.split()) <= 4, text
        assert research.authored_query_id(text) == query_id
        assert research.authored_query_id(query_id) == query_id
