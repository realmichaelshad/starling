"""World service: readable-text extraction, feed parsing, the news pool shape, and the fetch/image/summary routes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ssl
import time
from datetime import datetime, timezone

import httpx
import pytest

from starling_server.services.world import sources as src
from starling_server.services.world.extract import extract_page, parse_timestamp
from starling_server.services.world.news import (
    NewsPool,
    RawStory,
    SourceError,
    assemble_pool,
    normalize_item,
    parse_feed,
    parse_hn_host_hot,
    parse_hn_hits,
    parse_openrouter_models,
)
from starling_server.services.world.routes import FetchRateLimiter
from starling_server.services.world.safe_fetch import FetchResult

from . import fake_openai as ai
from .conftest import NOW, FakeClock
from .fakes_net import RecordingBackend, ScriptedResolver, http_response

# What normalize_item defines for one story on its own.
BASE_ITEM_KEYS = {"title", "url", "source", "summary", "published_at", "points", "lane"}
# T2 (2026-09-21): assemble_pool adds cluster_size, first_seen_at, points_per_hour and breaking to
# every pool item (additive: the engine reads them when present and ignores them otherwise).
ITEM_KEYS = BASE_ITEM_KEYS | {"cluster_size", "first_seen_at", "points_per_hour", "breaking"}
PUBLIC_V4 = "93.184.216.34"

ARTICLE_HTML = b"""<!doctype html><html><head>
<meta charset="utf-8"><title>Site | Fallback title</title>
<meta property="og:title" content="Example Labs &amp; friends ship a model">
<meta property="article:published_time" content="2026-09-14T08:30:00Z">
<style>body { color: red }</style><script>var tracking = "SCRIPT-TEXT";</script>
</head><body>
<nav><a href="/">Home</a> NAV-TEXT</nav>
<article><h1>Example Labs ships a model</h1>
<p>The model is available under an open licence and scores well on the lab's own coding benchmark.</p>
<p>Weights and an inference guide are published alongside the post, with notes on memory use and speed.</p>
<p>Pricing for the hosted version starts at a low rate per million tokens for early users.</p>
</article>
<footer>FOOTER-TEXT</footer><form>FORM-TEXT</form>
</body></html>"""


# ----------------------------------------------------------------------------- extraction


def test_extraction_keeps_the_article_as_lines_and_drops_page_chrome():
    page = extract_page(ARTICLE_HTML, "text/html; charset=utf-8")
    assert page.title == "Example Labs & friends ship a model"
    assert page.published_at == int(datetime(2026, 9, 14, 8, 30, tzinfo=timezone.utc).timestamp())
    lines = page.text.split("\n")
    assert lines[0] == "Example Labs ships a model"
    assert len(lines) == 4 and len(page.text) >= 100
    for chrome in ("SCRIPT-TEXT", "NAV-TEXT", "FOOTER-TEXT", "FORM-TEXT", "color: red"):
        assert chrome not in page.text


def test_extraction_date_fallbacks_and_missing_values():
    ld = b'<html><head><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-09-13T10:00:00+02:00"}</script></head><body><p>Short.</p></body></html>'
    assert extract_page(ld).published_at == int(datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc).timestamp())
    timed = b'<html><body><article><time datetime="2026-09-12" pubdate>Sept 12</time><p>Body</p></article></body></html>'
    assert extract_page(timed).published_at == int(datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp())
    bare = extract_page(b"<html><body><p>No title, no date.</p></body></html>")
    assert bare.title is None and bare.published_at == 0 and bare.text == "No title, no date."
    assert extract_page("<p>caf\xe9</p>".encode("latin-1"), "text/html; charset=iso-8859-1").text == "caf\xe9"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Mon, 14 Sep 2026 08:30:00 GMT", 1789374600),
        ("2026-09-14T08:30:00Z", 1789374600),
        ("2026-09-14T08:30:00", 1789374600),
        ("", 0),
        (None, 0),
        ("yesterday", 0),
    ],
)
def test_timestamps_are_whole_utc_seconds_or_zero(value, expected):
    assert parse_timestamp(value) == expected


# ----------------------------------------------------------------------------- parsers and shape

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Lab &amp; partner release &lt;b&gt;weights&lt;/b&gt;</title><link>https://example.com/a</link>
<description><![CDATA[<p>Jun 29, 2026 \xc2\xb7 The release includes <b>weights</b> and a guide for running the model locally on one GPU.</p>]]></description>
<pubDate>Mon, 14 Sep 2026 08:30:00 GMT</pubDate></item>
<item><title>No link</title></item>
<item><title>Undated</title><link>https://example.com/b</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title type="html">Atom entry</title><link rel="replies" href="https://example.org/a#comments"/>
<link rel="alternate" href="https://example.org/a"/><published>2026-09-13T10:00:00+00:00</published>
<summary type="html">A summary that is long enough to be kept by the parser for the pool.</summary></entry>
</feed>"""


def test_rss_and_atom_feeds_parse_into_stories():
    rss = parse_feed(RSS, "Example Feed")
    assert [story.url for story in rss] == ["https://example.com/a", "https://example.com/b"]
    assert rss[0].title == "Lab & partner release weights"
    assert rss[0].published_at == 1789374600 and rss[1].published_at == 0
    assert rss[0].summary.startswith("The release includes weights")  # date furniture removed
    [atom] = parse_feed(ATOM, "Example Blog")
    assert atom.url == "https://example.org/a" and atom.published_at == int(datetime(2026, 9, 13, 10, tzinfo=timezone.utc).timestamp())


def test_feeds_declaring_entities_are_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><rss><channel><item><title>&lol2;</title><link>https://e.com/</link></item></channel></rss>'
    with pytest.raises(SourceError):
        parse_feed(bomb, "Bad")
    with pytest.raises(SourceError):
        parse_feed(b"not xml at all", "Bad")


def test_normalized_items_match_the_pool_shape_exactly():
    item = normalize_item(RawStory(title="  <i>Big</i>   news &amp; more " + "x" * 400, url="https://example.com/n", source="S" * 100,
                                   summary="<p>Summary</p> " + "y" * 900, published_at=-5, points=-3, lane=""))
    assert set(item) == BASE_ITEM_KEYS
    assert item["title"].startswith("Big news & more") and len(item["title"]) == 300
    assert len(item["source"]) == 80 and len(item["summary"]) <= 700
    assert (item["published_at"], item["points"], item["lane"]) == (0, 0, None)
    assert all(isinstance(item[key], str) for key in ("title", "url", "source", "summary"))
    assert isinstance(item["published_at"], int) and isinstance(item["points"], int)
    assert normalize_item(RawStory(title="", url="https://example.com", source="S")) is None
    assert normalize_item(RawStory(title="T", url="javascript:alert(1)", source="S")) is None
    assert normalize_item(RawStory(title="T", url="/relative", source="S")) is None


def test_pool_is_deduplicated_and_interleaved_by_source_name():
    stories = [
        RawStory("Zeta story one", "https://z.example/1", "Zeta"),
        RawStory("Zeta story two", "https://z.example/2", "Zeta"),
        RawStory("ALPHA story one", "https://a.example/1", "Alpha"),
        RawStory("alpha STORY one", "https://a.example/dupe", "Alpha"),
        RawStory("Middle story", "https://m.example/1", "Middle"),
    ]
    pool = assemble_pool(stories)
    assert [item["url"] for item in pool] == ["https://a.example/1", "https://m.example/1", "https://z.example/1", "https://z.example/2"]
    many = [RawStory(f"Story {i}", f"https://e.example/{i}", f"Source {i % 3}") for i in range(400)]
    assert len(assemble_pool(many)) == 150


def test_hacker_news_and_openrouter_records():
    hits = {"hits": [
        {"title": "Show HN: a tool", "url": "", "objectID": "123", "points": 40, "created_at_i": NOW - 60, "story_text": "<p>Details &amp; more</p>"},
        {"title": "", "url": "https://skip.example"},
        {"title": "Linked", "url": "https://news.example/x", "points": "7", "created_at_i": NOW - 120},
    ]}
    first, second = parse_hn_hits(hits)
    assert first.url == "https://news.ycombinator.com/item?id=123" and first.summary == "Details & more" and first.points == 40
    assert second.points == 7 and second.source == "Hacker News"
    hot = parse_hn_host_hot({"hits": [
        {"title": "Lab post", "url": "https://www.openai.com/index/post", "points": 120, "num_comments": 30, "created_at_i": NOW - 3600},
        {"title": "Off lane", "url": "https://elsewhere.example/post", "points": 500, "created_at_i": NOW - 3600},
        {"title": "Too quiet", "url": "https://huggingface.co/blog/x", "points": 10, "created_at_i": NOW - 3600},
        {"title": "Too old", "url": "https://huggingface.co/blog/y", "points": 90, "created_at_i": NOW - 3 * 86_400},
    ]}, NOW)
    assert [story.title for story in hot] == ["Lab post"] and hot[0].summary.startswith("On Hacker News: 120 points")
    models = parse_openrouter_models({"data": [
        {"id": "example/model-x", "name": "Model X", "created": NOW - 3600, "description": "d" * 150},
        {"id": "example/old", "name": "Old", "created": NOW - 5 * 86_400, "description": "d" * 150},
        {"id": "example/thin", "name": "Thin", "created": NOW - 3600, "description": "short"},
    ]}, NOW)
    [model] = models
    assert (model.title, model.url, model.lane) == ("Model X, a newly listed model", "https://openrouter.ai/example/model-x", "frontier models")
    assert model.summary.startswith("Listed on OpenRouter as example/model-x.")


# ----------------------------------------------------------------------------- pool refresh


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, *, max_bytes, timeout_s, max_redirects=3, accept="*/*", truncate=False, headers=None):
        self.calls.append((url, dict(headers or {})))
        for prefix, handler in self.routes.items():
            if url.startswith(prefix):
                return handler(url, headers or {}) if callable(handler) else handler
        return FetchResult(url, 404, httpx.Headers(), b"")


def ok(body, headers=None):
    return FetchResult("https://source.example/", 200, httpx.Headers(headers or {}), body)


def hn_hits(title="Example Labs releases a new model", url="https://news.example/model"):
    return ok(json.dumps({"hits": [{"title": title, "url": url, "points": 42, "created_at_i": NOW - 600, "objectID": "1"}]}).encode())


def feeds_and_queries():
    feeds = (src.FeedSource("Beta Blog", "https://beta.example/feed"), src.FeedSource("Alpha Blog", "https://alpha.example/feed"))
    return feeds, (src.QUERIES_BY_ID["ai-model-releases"],)


def standard_routes():
    return {
        "https://beta.example/feed": ok(RSS, {"ETag": '"v1"'}),
        "https://alpha.example/feed": ok(ATOM, {"Last-Modified": "Mon, 14 Sep 2026 08:00:00 GMT"}),
        src.HN_SEARCH_BY_DATE_URL: lambda url, headers: (
            hn_hits() if ("AI+model+release" in url or "AI%20model%20release" in url) else ok(json.dumps({"hits": []}).encode())
        ),
        src.HN_SEARCH_URL: ok(json.dumps({"hits": []}).encode()),
        src.OPENROUTER_MODELS_URL: ok(json.dumps({"data": [{"id": "example/model-x", "name": "Model X", "created": NOW - 60, "description": "d" * 150}]}).encode()),
    }


def test_refresh_builds_a_pool_in_the_documented_shape():
    clock = FakeClock(NOW)
    feeds, queries = feeds_and_queries()
    http = FakeHttp(standard_routes())
    pool = NewsPool(http, clock=clock, monotonic=clock, sleep=clock.sleep, feeds=feeds, queries=queries)
    assert asyncio.run(pool.refresh()) == 5
    response = pool.headlines(150)
    assert response["generated_at"] == int(clock()) and isinstance(response["generated_at"], int)
    assert all(set(item) == ITEM_KEYS for item in response["items"])
    assert [item["source"] for item in response["items"]] == ["Alpha Blog", "Beta Blog", "Hacker News", "OpenRouter", "Beta Blog"]
    assert [item["lane"] for item in response["items"] if item["source"] == "OpenRouter"] == ["frontier models"]
    assert pool.headlines(2)["items"] == response["items"][:2]
    urls = [url for url, _ in http.calls]
    assert any(url.startswith(src.HN_SEARCH_BY_DATE_URL) and "query=AI+model+release" in url for url in urls)
    assert pool.freshness()["sources"]["Hacker News"]["items"] == 1


def test_feeds_use_conditional_requests_and_reuse_stories_on_304():
    clock = FakeClock(NOW)
    feeds, queries = feeds_and_queries()
    routes = standard_routes()
    http = FakeHttp(routes)
    pool = NewsPool(http, clock=clock, monotonic=clock, sleep=clock.sleep, feeds=feeds, queries=queries)
    asyncio.run(pool.refresh())
    routes["https://beta.example/feed"] = lambda url, headers: (
        FetchResult(url, 304, httpx.Headers(), b"") if headers.get("If-None-Match") == '"v1"' else ok(b"<rss/>"))
    routes["https://alpha.example/feed"] = lambda url, headers: (
        FetchResult(url, 304, httpx.Headers(), b"") if headers.get("If-Modified-Since") else ok(b"<rss/>"))
    clock.advance(900)
    asyncio.run(pool.refresh())
    sources = [item["source"] for item in pool.headlines()["items"]]
    assert sources.count("Beta Blog") == 2 and sources.count("Alpha Blog") == 1


def test_requests_to_one_host_are_spaced():
    clock = FakeClock(NOW)
    feeds = (src.FeedSource("One", "https://same.example/a"), src.FeedSource("Two", "https://same.example/b"))
    http = FakeHttp({"https://same.example/": ok(RSS)})
    pool = NewsPool(http, clock=clock, monotonic=clock, sleep=clock.sleep, feeds=feeds, queries=())
    asyncio.run(pool.refresh())
    assert 1.0 in clock.sleeps


def test_a_refresh_that_finds_nothing_keeps_the_previous_pool():
    clock = FakeClock(NOW)
    feeds, queries = feeds_and_queries()
    routes = standard_routes()
    pool = NewsPool(FakeHttp(routes), clock=clock, monotonic=clock, sleep=clock.sleep, feeds=feeds, queries=queries)
    asyncio.run(pool.refresh())
    before = pool.headlines()
    refreshed_at = pool.generated_at
    clock.advance(900)
    broken = FakeHttp({prefix: FetchResult(prefix, 500, httpx.Headers(), b"") for prefix in routes})
    pool._http = broken
    assert asyncio.run(pool.refresh()) == 0
    assert pool.headlines() == before and pool.generated_at == refreshed_at
    assert pool.freshness()["sources"]["Alpha Blog"]["last_error"] == "http_500"


def test_the_refresh_loop_runs_until_cancelled():
    clock = FakeClock(NOW)
    feeds, queries = feeds_and_queries()
    calls = []

    async def stop(seconds):
        calls.append(seconds)
        raise asyncio.CancelledError

    pool = NewsPool(FakeHttp(standard_routes()), clock=clock, monotonic=clock, sleep=stop, feeds=feeds, queries=queries, host_spacing_s=0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pool.run())
    # T1: the loop ticks every 2.5 minutes (it was one refresh of everything every 15).
    assert pool.generated_at == NOW and calls == [pool.refresh_interval_s] and pool.refresh_interval_s == 150


def test_the_app_refreshes_news_in_the_background_only_when_asked(make_services):
    routes = standard_routes()
    idle = make_services(world_http=FakeHttp(routes))
    assert idle.app.state.news_task is None
    http = FakeHttp(routes)
    running = make_services(world_http=http, start_background=True, sleep=asyncio.sleep, monotonic=time.monotonic)
    deadline = time.monotonic() + 10
    while running.app.state.world.pool.generated_at == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert running.app.state.world.pool.generated_at > 0 and http.calls


# ----------------------------------------------------------------------------- routes


def test_headlines_route_validates_limit_and_needs_a_token(make_services):
    services = make_services(world_http=FakeHttp(standard_routes()))
    asyncio.run(services.app.state.world.pool.refresh())
    account = services.add_account()
    response = services.client.get("/v1/news/headlines?limit=2", headers=services.auth(account))
    assert response.status_code == 200 and len(response.json()["items"]) == 2
    full = services.client.get("/v1/news/headlines", headers=services.auth(account)).json()
    assert full == services.app.state.world.pool.headlines(150) and len(full["items"]) >= 2
    for bad in ("0", "151", "abc", "-1"):
        assert services.client.get(f"/v1/news/headlines?limit={bad}", headers=services.auth(account)).status_code == 400
    assert services.client.get("/v1/news/headlines").status_code == 401


def fetch_services(make_services, responder, answers=None):
    resolver = ScriptedResolver(answers or {"news.example": [[PUBLIC_V4]], "internal.example": [["10.0.0.9"]]})
    backend = RecordingBackend(responder)
    services = make_services(resolver=resolver, network_backend=backend, ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    return services, backend


def test_fetch_route_returns_readable_page_fields(make_services, caplog):
    caplog.set_level(logging.DEBUG)
    services, backend = fetch_services(make_services, lambda a, p, r: http_response(200, ARTICLE_HTML, {"Content-Type": "text/html"}))
    account = services.add_account()
    url = "https://news.example/URL-CANARY-5d1e/article"
    response = services.client.post("/v1/fetch", json={"url": url}, headers=services.auth(account))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"final_url", "title", "text", "published_at"}
    assert body["final_url"] == url and body["title"] == "Example Labs & friends ship a model"
    assert len(body["text"]) >= 100 and body["published_at"] > 0
    assert backend.connections == [(PUBLIC_V4, 443)]
    assert "URL-CANARY" not in caplog.text


def test_fetch_route_refuses_private_destinations_bad_bodies_and_non_pages(make_services):
    services, backend = fetch_services(make_services, lambda a, p, r: http_response(200, b"%PDF-1.7", {"Content-Type": "application/pdf"}))
    account = services.add_account()
    headers = services.auth(account)
    blocked = services.client.post("/v1/fetch", json={"url": "http://internal.example/admin"}, headers=headers)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "destination_blocked"
    assert services.client.post("/v1/fetch", json={"url": "http://news.example/", "extra": 1}, headers=headers).status_code == 400
    assert services.client.post("/v1/fetch", json={"url": 5}, headers=headers).status_code == 400
    assert services.client.post("/v1/fetch", json={"url": "http://news.example/"}).status_code == 401
    pdf = services.client.post("/v1/fetch", json={"url": "http://news.example/file.pdf"}, headers=headers)
    assert pdf.status_code == 415 and pdf.json()["error"]["code"] == "unsupported_content"
    assert backend.connections == [(PUBLIC_V4, 80)]


def test_fetches_are_rate_limited_per_caller(make_services):
    services, _ = fetch_services(make_services, lambda a, p, r: http_response(200, ARTICLE_HTML, {"Content-Type": "text/html"}))
    services.app.state.world.limiter = FetchRateLimiter(per_minute=2, monotonic=services.clock)
    first, second = services.add_account(), services.add_account()
    for _ in range(2):
        assert services.client.post("/v1/fetch", json={"url": "http://news.example/"}, headers=services.auth(first)).status_code == 200
    limited = services.client.post("/v1/fetch", json={"url": "http://news.example/"}, headers=services.auth(first))
    assert limited.status_code == 429 and limited.headers["retry-after"]
    assert services.client.post("/v1/fetch", json={"url": "http://news.example/"}, headers=services.auth(second)).status_code == 200
    services.clock.advance(61)
    assert services.client.post("/v1/fetch", json={"url": "http://news.example/"}, headers=services.auth(first)).status_code == 200


GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 20


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        (GIF, "image/gif", 200),
        (b"<html>not an image</html>", "image/png", 415),
        (GIF, "text/html", 415),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", 415),
    ],
)
def test_image_route_checks_type_and_magic_bytes(make_services, body, content_type, status):
    services, _ = fetch_services(make_services, lambda a, p, r: http_response(200, body, {"Content-Type": content_type}))
    account = services.add_account()
    response = services.client.post("/v1/image", json={"url": "https://news.example/pic"}, headers=services.auth(account))
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"data_url": "data:image/gif;base64," + __import__("base64").b64encode(GIF).decode(), "mime": "image/gif"}


def test_image_route_refuses_oversized_images_and_private_redirects(make_services):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024)

    def responder(address, port, raw):
        if b"/redirect" in raw.split(b"\r\n")[0]:
            return http_response(302, b"", {"Location": "http://internal.example/x.png"})
        return http_response(200, big, {"Content-Type": "image/png"})

    services, backend = fetch_services(make_services, responder)
    account = services.add_account()
    too_big = services.client.post("/v1/image", json={"url": "https://news.example/big.png"}, headers=services.auth(account))
    assert too_big.status_code == 413
    redirected = services.client.post("/v1/image", json={"url": "https://news.example/redirect"}, headers=services.auth(account))
    assert redirected.status_code == 403
    assert all(address == PUBLIC_V4 for address, _ in backend.connections)


def test_models_route_lists_the_three_lanes(services):
    account = services.add_account()
    response = services.client.get("/v1/models", headers=services.auth(account))
    assert [model["id"] for model in response.json()["data"]] == ["starling-quality", "starling-fast", "starling-vision"]
    assert services.client.get("/v1/models").status_code == 401


# ----------------------------------------------------------------------------- summaries


def summary_services(make_services):
    services = make_services(world_http=FakeHttp(standard_routes()))
    asyncio.run(services.app.state.world.pool.refresh())
    return services


def post_summary(services, account, payload):
    return services.client.post("/v1/summary", json=payload, headers=services.auth(account))


def test_summary_is_generated_from_the_pool_through_the_system_bucket_and_cached(make_services):
    services = summary_services(make_services)
    account = services.add_account()
    services.fake.queue(ai.ok_json(content="Example Labs released a new model [1]."))
    now = int(services.clock())
    response = post_summary(services, account, {"query_id": "ai-model-releases"})
    assert response.status_code == 200
    body = response.json()
    assert body["query_id"] == "ai-model-releases" and body["generated_at"] == now
    assert body["answer"] == body["summary"] == "Example Labs released a new model [1]."
    assert 1 <= len(body["sources"]) <= 6
    assert all(set(source) == {"title", "url", "published_at", "snippet"} for source in body["sources"])
    [sent] = services.fake.requests
    assert sent.body["model"] == services.settings.ai_model_fast
    assert "Topic: AI model release" in sent.body["messages"][1]["content"] and "[1]" in sent.body["messages"][1]["content"]
    assert sent.body["safety_identifier"] == hashlib.sha256(b"starling-world-summary").hexdigest()[:32]
    [row] = services.rows("SELECT bucket, account_id, reservation_id, lane, status, cost_micros FROM ai_calls")
    assert (row["bucket"], row["account_id"], row["reservation_id"], row["lane"], row["status"]) == ("system", None, None, "starling-fast", "ok")
    assert services.rows("SELECT * FROM usage_counters") == []
    assert services.one("SELECT cost_micros FROM budget_counters WHERE scope = 'global_day'")["cost_micros"] == row["cost_micros"]
    assert post_summary(services, account, {"query_id": "ai-model-releases"}).json() == body
    assert len(services.fake.requests) == 1  # cached
    services.clock.advance(3601)
    assert post_summary(services, account, {"query_id": "ai-model-releases"}).status_code == 200
    assert len(services.fake.requests) == 2  # regenerated after 60 minutes


def test_summary_accepts_only_authored_query_ids(make_services):
    services = summary_services(make_services)
    account = services.add_account()
    for payload in ({"query_id": "write about my private notes"}, {"query_id": 3}, {"query": "free text"}, {"query_id": "coding-agents", "text": "x"}):
        response = post_summary(services, account, payload)
        assert response.status_code == 400 and response.json()["error"]["code"] == "bad_request"
    assert services.fake.requests == []
    assert services.client.post("/v1/summary", json={"query_id": "coding-agents"}).status_code == 401


def test_summary_without_sources_or_with_a_none_answer_is_empty(make_services):
    services = summary_services(make_services)
    account = services.add_account()
    empty = post_summary(services, account, {"query_id": "llm-pricing"}).json()
    assert (empty["answer"], empty["sources"]) == ("", []) and services.fake.requests == []
    services.fake.queue(ai.ok_json(content="NONE"))
    none = post_summary(services, account, {"query_id": "ai-model-releases"}).json()
    assert none["answer"] == "" and none["sources"]


def test_summary_respects_owner_pause_but_serves_a_stale_digest(make_services):
    services = summary_services(make_services)
    account = services.add_account()
    services.set_service_state("ai_paused", "true")
    paused = post_summary(services, account, {"query_id": "ai-model-releases"})
    assert paused.status_code == 503 and paused.json()["error"]["code"] == "budget_exhausted"
    services.set_service_state("ai_paused", None)
    fresh = post_summary(services, account, {"query_id": "ai-model-releases"}).json()
    services.set_service_state("ai_paused", "true")
    services.clock.advance(3601)
    stale = post_summary(services, account, {"query_id": "ai-model-releases"})
    assert stale.status_code == 200 and stale.json()["generated_at"] == fresh["generated_at"]
