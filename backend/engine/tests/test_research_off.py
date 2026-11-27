"""The shared news pool (CONTRACTS §4.2, docs/architecture/news-pool-shape.md).

What these pin: the advertised off switch really stops the request; nothing
derived from the account is ever sent with it; and the pool's fields are read
the way the shape document promises (`published_at` -> `created_at`, 0 = unknown).
"""
from __future__ import annotations

import time

import httpx
import pytest

from app import cloud, news
from app.storage import connect, get_job_state, set_job_state

TOKEN = "n" * 43


def _item(title, source="Example Feed", published_at=1757796400, **extra):
    return {"title": title, "url": f"https://example.com/{abs(hash(title))}", "source": source,
            "summary": "A summary with enough words to be real evidence for a post.",
            "published_at": published_at, "points": 0, **extra}


@pytest.fixture()
def pool(monkeypatch):
    """A fake world service. `calls` records every request that reached it."""
    calls: list[httpx.Request] = []
    items: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"generated_at": int(time.time()), "items": list(items)})

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    cloud.set_services_token(TOKEN)
    monkeypatch.setattr(cloud, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://services"))
    return calls, items


def test_research_off_means_no_news_request(monkeypatch, pool):
    calls, items = pool
    items.append(_item("Example Labs releases a coding model"))
    monkeypatch.setenv("STARLING_RESEARCH", "off")
    assert news.headlines(["model releases"], limit=5, force=True) == []
    assert calls == [], "the off switch was ignored"


def test_proxy_mode_reads_the_shared_pool(monkeypatch, pool):
    calls, items = pool
    items.append(_item("Example Labs releases a coding model"))
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    stories = news.headlines(None, limit=5, force=True)
    assert [story["title"] for story in stories] == ["Example Labs releases a coding model"]
    assert len(calls) == 1
    assert calls[0].url.path == "/v1/news/headlines"
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert calls[0].headers["x-starling-bucket"] == "background"


def test_the_request_never_carries_the_accounts_own_words(monkeypatch, pool):
    """Themes are two words distilled from the account's own posts. They decide
    what is KEPT after the pool arrives; they must never be sent."""
    calls, items = pool
    items.append(_item("Example Labs releases a coding model"))
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    news.headlines(["opus pricing", "tracker side project", "first time"], limit=5, force=True)
    assert len(calls) == 1
    sent = str(calls[0].url) + calls[0].content.decode("utf-8", errors="replace")
    for theme in ("opus", "tracker", "first time", "pricing"):
        assert theme not in sent.lower(), f"{theme!r} reached the wire: {sent}"
    assert dict(calls[0].url.params) == {"limit": "150"}


def test_published_at_is_read_as_created_at_and_unknown_is_zero(monkeypatch, pool):
    _calls, items = pool
    items.extend([
        _item("Integer stamp", published_at=1757796400),
        _item("Zero stamp", published_at=0),
        _item("Null stamp", published_at=None),
        _item("String stamp", published_at="2026-09-13T20:00:00Z"),
        _item("Negative stamp", published_at=-5),
        _item("Float stamp", published_at=1757796400.9),
        _item("Boolean stamp", published_at=True),
    ])
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    by_title = {story["title"]: story["created_at"] for story in news.headlines(limit=50, force=True)}
    assert by_title == {"Integer stamp": 1757796400, "Zero stamp": 0, "Null stamp": 0,
                        "String stamp": 0, "Negative stamp": 0, "Float stamp": 1757796400,
                        "Boolean stamp": 0}


def test_unusable_items_are_dropped_and_text_is_cleaned(monkeypatch, pool):
    _calls, items = pool
    items.extend([
        {"title": "", "url": "https://example.com/a", "source": "Feed"},
        {"title": "No link", "url": "", "source": "Feed"},
        {"title": "Bad scheme", "url": "javascript:alert(1)", "source": "Feed"},
        "not an object",
        {"title": "<b>Tagged</b> &amp; escaped", "url": "https://example.com/ok", "source": "Feed",
         "summary": "Jun 29, 2026 · The model ships with a 1M token context window.",
         "published_at": 1757796400, "lane": "frontier models"},
    ])
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    stories = news.headlines(limit=50, force=True)
    assert len(stories) == 1
    story = stories[0]
    assert story["title"] == "Tagged & escaped"
    assert story["summary"] == "The model ships with a 1M token context window."
    assert story["lane"] == "frontier models"
    assert story["points"] == 0


def test_duplicates_collapse_and_no_single_source_floods_the_pool(monkeypatch, pool):
    _calls, items = pool
    items.extend([
        _item("A one", source="Alpha"), _item("A two", source="Alpha"),
        _item("A three", source="Alpha"), _item("a ONE", source="Beta"),
        _item("B one", source="Beta"),
    ])
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    titles = [story["title"] for story in news.headlines(limit=50, force=True)]
    assert titles == ["A one", "B one", "A two", "A three"]


def test_a_failed_refresh_serves_the_stale_pool_and_says_so(monkeypatch):
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    cloud.set_services_token(TOKEN)
    monkeypatch.setattr(cloud, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={
            "error": {"code": "ai_unavailable", "message": "down"}})),
        base_url="http://services"))
    stale = [{"title": "Yesterday's story", "url": "https://example.com/y", "source": "Feed",
              "points": 0, "created_at": 1, "summary": ""}]
    set_job_state("news_headlines", {"at": time.time() - 3600, "items": stale})
    assert news.headlines(limit=5, force=True) == stale
    with connect() as conn:
        rows = conn.execute("SELECT reasons_json FROM safety_events WHERE category='news'").fetchall()
    assert any("news_refresh_empty" in str(row[0]) for row in rows)


def test_a_pool_fetched_seconds_ago_is_not_fetched_again(monkeypatch, pool):
    calls, items = pool
    items.append(_item("Fresh story"))
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    news.headlines(limit=5, force=True)
    news.headlines(limit=5, force=True)
    news.headlines(limit=5)
    assert len(calls) == 1
    assert get_job_state("news_headlines")["items"][0]["title"] == "Fresh story"
