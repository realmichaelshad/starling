"""T1 + T2 on the engine side.

T1: a story the shared pool picked up is a candidate within minutes. The engine used to keep
the pool for 15 minutes and its candidate list for 30, on top of the pool's own 15-minute
refresh, so a new story took 15-45 minutes to become something a customer could post about.

T2: the pool now says how big and how fast a story is (cluster_size, first_seen_at,
points_per_hour, breaking); the engine carries those fields to its candidates and ranks a
breaking story right after "has a source".
"""
from __future__ import annotations

import time

import httpx
import pytest

from app import cloud, news
from app import radar as R
from app.storage import get_job_state, set_job_state

TOKEN = "n" * 43
NOW = int(time.time())


def _item(title, **extra):
    return {"title": title, "url": f"https://example.com/{abs(hash(title))}", "source": "Example Feed",
            "summary": "A summary with enough words to be real evidence for a post.",
            "published_at": NOW - 600, "points": 0, **extra}


@pytest.fixture()
def world(monkeypatch):
    """A fake world service whose pool the test can move on."""
    state = {"generated_at": NOW, "items": []}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"generated_at": state["generated_at"], "items": list(state["items"])})

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    cloud.set_services_token(TOKEN)
    monkeypatch.setattr(cloud, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://services"))
    return state, calls


def _age(key: str, seconds: float) -> None:
    stored = get_job_state(key)
    set_job_state(key, {**stored, "at": float(stored["at"]) - seconds})


@pytest.fixture()
def permissive_beat(monkeypatch):
    """The beat filters have their own tests; these are about freshness and rank."""
    import app.intelligence as intelligence
    import app.release_radar as release_radar
    monkeypatch.setattr(R, "builder_fit", lambda topic: True)
    monkeypatch.setattr(R, "interest_fit", lambda topic, themes: 1.0)
    monkeypatch.setattr(R, "event_subject", lambda title: title)
    monkeypatch.setattr(intelligence, "is_roundup_topic", lambda topic: False)
    monkeypatch.setattr(release_radar, "recent_releases", lambda **kwargs: [])


def test_the_engine_keeps_the_pool_minutes_not_a_quarter_hour():
    assert news._CACHE_TTL_S <= 180, "T1: the pool's fast tick is 2.5 minutes"
    assert R._WEB_CANDIDATES_TTL_S <= 300, "T1: candidates were cached for 30 minutes"
    assert news._FORCE_REFRESH_FLOOR_S <= 60


def test_the_size_and_speed_fields_reach_the_engine(world):
    state, _ = world
    state["items"] = [
        _item("Anthropic releases Claude Opus 5", cluster_size=3, first_seen_at=NOW - 900,
              points_per_hour=120.0, breaking=True),
        _item("A quiet story"),
        _item("Junk fields", cluster_size="many", first_seen_at=-4, points_per_hour=-5.0,
              breaking="yes"),
    ]
    stories = {story["title"]: story for story in news.headlines(limit=5, force=True)}
    loud = stories["Anthropic releases Claude Opus 5"]
    assert (loud["cluster_size"], loud["first_seen_at"], loud["points_per_hour"], loud["breaking"]) == (
        3, NOW - 900, 120.0, True)
    for title in ("A quiet story", "Junk fields"):
        assert not {"cluster_size", "first_seen_at", "points_per_hour", "breaking"} & set(stories[title]), (
            "an older pool, or junk, must not invent a size or a speed")


def test_an_unchanged_pool_is_not_processed_again(world, monkeypatch):
    state, calls = world
    state["items"] = [_item("Story one")]
    news.headlines(limit=5, force=True)
    _age("news_headlines", news._CACHE_TTL_S + 10)
    processed: list[dict] = []
    real_story = news._story
    monkeypatch.setattr(news, "_story", lambda raw: processed.append(raw) or real_story(raw))
    assert [story["title"] for story in news.headlines(limit=5)] == ["Story one"]
    assert len(calls) == 2 and processed == [], "same generated_at: nothing to process"
    assert time.time() - float(get_job_state("news_headlines")["at"]) < 5, "the copy counts as fresh again"
    state["generated_at"] += 150
    state["items"] = [_item("Story two")]
    _age("news_headlines", news._CACHE_TTL_S + 10)
    assert [story["title"] for story in news.headlines(limit=5)] == ["Story two"]
    assert news.pool_stamp() == state["generated_at"]


def test_a_story_the_pool_picked_up_is_a_candidate_on_the_next_press(world, permissive_beat):
    state, _ = world
    state["items"] = [_item("Example Labs ships Model One")]
    assert [c["topic"] for c in R._web_candidates(["model releases"])] == ["Example Labs ships Model One"]
    built_at = get_job_state("radar_web_candidates")["at"]

    # Same pool, a minute later: the cached list is served as it is.
    assert [c["topic"] for c in R._web_candidates(["model releases"])] == ["Example Labs ships Model One"]
    assert get_job_state("radar_web_candidates")["at"] == built_at

    # The pool moved on. The candidate list is only three minutes old, and still gives way.
    state["generated_at"] += 150
    state["items"] = [_item("Example Labs ships Model Two", breaking=True, cluster_size=2),
                      _item("Example Labs ships Model One")]
    _age("news_headlines", news._CACHE_TTL_S + 10)
    _age("radar_web_candidates", 180)
    by_topic = {c["topic"]: c for c in R._web_candidates(["model releases"])}
    assert set(by_topic) == {"Example Labs ships Model One", "Example Labs ships Model Two"}
    assert by_topic["Example Labs ships Model Two"]["breaking"] is True
    assert by_topic["Example Labs ships Model Two"]["cluster_size"] == 2
    assert by_topic["Example Labs ships Model One"]["breaking"] is False
    assert by_topic["Example Labs ships Model One"]["cluster_size"] == 1
