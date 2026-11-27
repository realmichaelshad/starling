"""T1 + T2: the news pool notices a story in minutes and says how big and how fast it is.

It used to refresh everything every 15 minutes, which with the engine's own caches made a new
story 15-45 minutes old before a customer could post about it, and it ranked nothing by how
many sources carried a story or how fast it was rising.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from starling_server.services.world import sources as src
from starling_server.services.world.news import (BREAKING_POINTS_PER_HOUR, NewsPool, RawStory,
                                                  assemble_pool)
from starling_server.services.world.safe_fetch import FetchResult

from .conftest import NOW, FakeClock
from .test_world import FakeHttp, feeds_and_queries, standard_routes


def test_a_fast_tick_reads_hacker_news_and_openrouter_and_keeps_the_feeds():
    clock = FakeClock(NOW)
    feeds, queries = feeds_and_queries()
    http = FakeHttp(standard_routes())
    pool = NewsPool(http, clock=clock, monotonic=clock, sleep=clock.sleep, feeds=feeds, queries=queries)
    assert asyncio.run(pool.refresh(slow=None)) == 5          # the first tick reads everything
    first = {item["source"] for item in pool.headlines()["items"]}
    http.calls.clear()
    clock.advance(pool.refresh_interval_s)
    asyncio.run(pool.refresh(slow=None))
    reread = [url for url, _ in http.calls]
    assert any(url.startswith(src.OPENROUTER_MODELS_URL) for url in reread), reread
    assert reread and all(url.startswith(("https://hn.algolia.com/", src.OPENROUTER_MODELS_URL))
                          for url in reread), reread
    assert {item["source"] for item in pool.headlines()["items"]} == first, "the feeds' stories are kept"
    http.calls.clear()
    clock.advance(pool.slow_refresh_interval_s)
    asyncio.run(pool.refresh(slow=None))
    assert any(url.startswith("https://beta.example/") for url, _ in http.calls), "the slow tick comes round"
    assert pool.refresh_interval_s <= 180 and pool.slow_refresh_interval_s <= 300


def test_openrouter_is_read_with_a_conditional_request():
    clock = FakeClock(NOW)
    feeds, queries = feeds_and_queries()
    routes = standard_routes()
    listing = json.dumps({"data": [{"id": "example/model-x", "name": "Model X", "created": NOW - 60,
                                    "description": "d" * 150}]}).encode()
    routes[src.OPENROUTER_MODELS_URL] = lambda url, headers: (
        FetchResult(url, 304, httpx.Headers(), b"") if headers.get("If-None-Match") == '"m1"'
        else FetchResult(url, 200, httpx.Headers({"ETag": '"m1"'}), listing))
    pool = NewsPool(FakeHttp(routes), clock=clock, monotonic=clock, sleep=clock.sleep, feeds=feeds, queries=queries)
    asyncio.run(pool.refresh())
    clock.advance(pool.refresh_interval_s)
    asyncio.run(pool.refresh(slow=None))
    assert [item for item in pool.headlines()["items"] if item["source"] == "OpenRouter"], "304 reuses the listing"


def _story(title, source, *, published=NOW - 600, points=0, url=None):
    return RawStory(title=title, url=url or f"https://{source.lower().replace(' ', '')}.example/{abs(hash(title))}",
                    source=source, summary="", published_at=published, points=points)


def test_one_story_from_several_sources_is_one_cluster_and_breaking():
    items = assemble_pool([
        _story("Anthropic releases Claude Opus 5", "The Decoder"),
        _story("Claude Opus 5 is here with a longer context window", "TechCrunch AI"),
        _story("A bakery opens downtown", "Local News"),
    ], now=NOW)
    by_title = {item["title"]: item for item in items}
    opus = by_title["Anthropic releases Claude Opus 5"]
    assert opus["cluster_size"] == 2 and opus["breaking"] is True
    assert by_title["A bakery opens downtown"]["cluster_size"] == 1
    assert by_title["A bakery opens downtown"]["breaking"] is False


def test_an_old_multi_source_story_is_not_breaking():
    items = assemble_pool([
        _story("Anthropic releases Claude Opus 5", "The Decoder", published=NOW - 30 * 3600),
        _story("Claude Opus 5 is here", "TechCrunch AI", published=NOW - 29 * 3600),
    ], now=NOW)
    assert all(item["cluster_size"] == 2 and not item["breaking"] for item in items)


def test_points_velocity_is_measured_since_the_last_refresh():
    url = "https://example.com/hn-story"
    story = _story("Show HN: a tiny local firewall for coding agents", "Hacker News",
                   points=300, url=url, published=NOW - 5 * 3600)
    items = assemble_pool([story], now=NOW, previous_points={url: (200, NOW - 3600)},
                          first_seen={url: NOW - 4 * 3600})
    assert items[0]["points_per_hour"] == 100.0 and items[0]["breaking"] is True
    assert items[0]["first_seen_at"] == NOW - 4 * 3600
    slow = assemble_pool([story], now=NOW, previous_points={url: (295, NOW - 3600)})
    assert slow[0]["points_per_hour"] < BREAKING_POINTS_PER_HOUR and not slow[0]["breaking"]


def test_a_new_listing_from_a_watched_lab_is_breaking():
    listing = RawStory(title="GPT-6 Mini, a newly listed model", url="https://openrouter.ai/openai/gpt-6-mini",
                       source=src.OPENROUTER_SOURCE, summary="Listed on OpenRouter.", published_at=NOW - 1800,
                       lane=src.LANE_FRONTIER)
    unknown = RawStory(title="Somemodel, a newly listed model", url="https://openrouter.ai/someone/somemodel",
                       source=src.OPENROUTER_SOURCE, summary="Listed on OpenRouter.", published_at=NOW - 1800,
                       lane=src.LANE_FRONTIER)
    items = {item["url"]: item for item in assemble_pool([listing, unknown], now=NOW)}
    assert items[listing.url]["breaking"] is True
    assert items[unknown.url]["breaking"] is False


def test_a_restart_does_not_make_undated_stories_breaking():
    undated = [_story("Anthropic releases Claude Opus 5", "The Decoder", published=0),
               _story("Claude Opus 5 is here", "TechCrunch AI", published=0)]
    after_restart = assemble_pool(undated, now=NOW)          # no memory: when they appeared is unknown
    assert all(item["cluster_size"] == 2 and not item["breaking"] for item in after_restart)
    remembered = {item["url"]: NOW - 300 for item in after_restart}
    later = assemble_pool(undated, now=NOW, first_seen=remembered)
    assert all(item["breaking"] for item in later), "first seen five minutes ago by a running pool"


def test_an_unchanged_openrouter_list_still_ages_out_of_the_window():
    clock = FakeClock(NOW)
    feeds, queries = feeds_and_queries()
    routes = standard_routes()
    listing = json.dumps({"data": [{"id": "example/model-x", "name": "Model X", "created": NOW - 47 * 3600,
                                    "description": "d" * 150}]}).encode()
    routes[src.OPENROUTER_MODELS_URL] = lambda url, headers: (
        FetchResult(url, 304, httpx.Headers(), b"") if headers.get("If-None-Match") == '"m1"'
        else FetchResult(url, 200, httpx.Headers({"ETag": '"m1"'}), listing))
    pool = NewsPool(FakeHttp(routes), clock=clock, monotonic=clock, sleep=clock.sleep, feeds=feeds, queries=queries)
    asyncio.run(pool.refresh())
    assert [item for item in pool.headlines()["items"] if item["source"] == "OpenRouter"]
    clock.advance(2 * 3600)
    asyncio.run(pool.refresh())
    assert not [item for item in pool.headlines()["items"] if item["source"] == "OpenRouter"]
