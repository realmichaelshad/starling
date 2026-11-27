"""Bring-your-own-data: safe ingestion, honest fallbacks, grounded composition
through the normal gated pipeline, and chat URL extraction."""
from __future__ import annotations

from app import sources
from app.sources import compose_request, extract_urls, ingest_sources
from app.storage import delete_user_source, list_user_sources


class OfflineModel:
    router = None

    def status(self):
        return {"available": False}


def test_extract_urls_dedupes_and_bounds():
    text = ("read https://example.com/a and https://example.com/a plus "
            "https://example.com/b, https://example.com/c and https://example.com/d.")
    urls = extract_urls(text)
    assert urls == ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
    assert extract_urls("no links here") == []


def test_ingest_pasted_text_offline_is_honest():
    result = ingest_sources([], "My own notes about agent tooling: retries lose state, and state is the bottleneck.",
                            "my notes", OfflineModel())
    stored = result["stored"]
    assert len(stored) == 1
    assert "unavailable" in result["results"][0]["summary"]
    assert list_user_sources()[0]["title"] == "my notes"


def test_ingest_unfetchable_url_reports_unreadable(monkeypatch):
    monkeypatch.setenv("STARLING_RESEARCH", "on")
    monkeypatch.setattr(sources, "fetch_page_text", lambda url: None)
    result = ingest_sources(["https://example.com/blocked"], None, None, OfflineModel())
    assert result["results"][0]["status"] == "unreadable"
    assert result["stored"] == []


def test_ingest_url_respects_research_kill_switch(monkeypatch):
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    result = ingest_sources(["https://example.com/a"], None, None, OfflineModel())
    assert result["results"][0]["status"] == "blocked"
    assert "paste the text" in result["results"][0]["reason"]


def test_compose_request_grounds_in_source_material():
    ingest_sources([], "The new release cut inference cost forty percent while doubling context length for local models.",
                    "release notes", OfflineModel())
    source_id = list_user_sources()[0]["id"]
    request, error = compose_request([source_id], "make it punchy", "post", thread=False)
    assert error is None
    assert "inference cost" in request.context.text
    assert "untrusted reference text" in request.direction
    assert "make it punchy" in request.direction
    assert request.thread_requested is False
    missing, error = compose_request(["nope"], None)
    assert missing is None and "No matching sources" in error


def test_sources_roundtrip_without_the_routes():
    """The `/sources` routes are gone; ingest, list, compose and delete remain
    as library functions and still round-trip."""
    empty = ingest_sources([], None, None, OfflineModel())
    assert empty["results"] == [] and empty["stored"] == []
    stored = ingest_sources([], "Pasted research data about agents and their state handling failures.",
                            "notes", OfflineModel())
    assert stored["stored"]
    listing = list_user_sources()
    assert len(listing) == 1
    request, error = compose_request([listing[0]["id"]], "short and specific")
    assert error is None and request is not None
    assert "short and specific" in request.direction
    assert delete_user_source(listing[0]["id"]) is True
    assert delete_user_source(listing[0]["id"]) is False
    assert list_user_sources() == []
