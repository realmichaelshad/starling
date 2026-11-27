"""The watcher on the accounts this user orbits (2026-07-29).

His words: "an account that I mainly post on just had this post and it's a
big one — what will the AI do? It should have a watcher on these big
accounts, the ones I'm typically going to post on, and these should be the
official ones, like Anthropic."

Derived, never declared: which accounts matter is already written in the
archive. A different user's archive yields a different watchlist with no code
change — the standing rule for everything here that looks like a preference.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import storage                                   # noqa: E402
from app import subject as subject_mod                    # noqa: E402
from app import watchlist                                 # noqa: E402


@pytest.fixture(autouse=True)
def _clean_caches():
    watchlist.reset_watchlist_cache()
    subject_mod._LANE_CACHE = None
    yield
    watchlist.reset_watchlist_cache()
    subject_mod._LANE_CACHE = None


def _seed(texts):
    with storage.connect() as conn:
        for index, text in enumerate(texts):
            conn.execute("insert into posts (post_id, text) values (?, ?)",
                         (f"watch-{index}", text))
        conn.commit()
    watchlist.reset_watchlist_cache()


def test_the_watchlist_comes_from_the_archive():
    _seed([
        "Anthropic shipped Opus 5 and the pricing is wild.",
        "Anthropic's limits are still the annoying part.",
        "OpenAI shipped Codex updates today.",
        "OpenAI pricing moved again.",
        "Ate a good sandwich.",                      # no subject at all
    ])
    watched = watchlist.watched_subjects()
    assert "anthropic" in watched and "openai" in watched
    assert watchlist.is_watched("Anthropic launches Cowork, a Claude agent")
    assert watchlist.watched_handle(
        "Anthropic launches Cowork, a Claude agent") == "@AnthropicAI"


def test_one_mention_is_not_a_beat():
    """A passing joke must not put an account on the watchlist."""
    _seed([
        "Anthropic shipped Opus 5.",
        "Anthropic pricing again.",
        "Watched the Avengers last night.",           # marvel, once
    ])
    watched = watchlist.watched_subjects()
    assert "anthropic" in watched
    assert not watchlist.is_watched("Avengers Doomsday trailer drops")


def test_another_users_archive_gives_another_watchlist():
    """NOT HARDCODED. A web-dev account watches Vercel and Next.js, and the
    labs in the presentational handle map never make it a watcher of theirs."""
    _seed([
        "Vercel's pricing change hits every hobby deployment.",
        "Vercel previews are still the best part of the workflow.",
        "Next.js 16 makes hydration errors readable.",
        "Next.js middleware finally does what you expect.",
    ])
    watched = set(watchlist.watched_subjects())
    assert watched, "no watchlist derived at all"
    assert not watchlist.is_watched("Anthropic launches Cowork"), (
        "a lab this account never mentions became a watched account")
    assert watchlist.is_watched("Vercel raises deployment pricing again")


def test_an_empty_archive_watches_nobody():
    assert watchlist.watched_subjects() == {}
    assert not watchlist.is_watched("Anthropic launches Cowork")
    assert watchlist.watched_handle("Anthropic launches Cowork") == ""


def test_a_watched_account_outranks_a_notable_stranger(monkeypatch):
    """The whole point: when a watched account moves, that beats general
    notability — and must never lose to a stranger's Show HN launch."""
    from app import radar
    _seed([
        "Anthropic shipped Opus 5 and the pricing is wild.",
        "Anthropic's rate limits are still the annoying part.",
    ])
    pool = ["UnUI - Help coding agents make better design decisions",
            "Perplexity ships a Windows agent",          # notable, not watched
            "Anthropic launches Cowork, a Claude Desktop agent"]
    monkeypatch.setattr(radar, "compute_radar",
                        lambda *a, **k: {"emerging": [{"topic": t, "angle": "a",
                                                       "why": "b"} for t in pool],
                                         "trends": [], "web": {}})
    monkeypatch.setattr(radar, "topic_staleness", lambda t, r: {"stale": False, "reason": ""})
    monkeypatch.setattr(radar, "_account_themes",
                        lambda: ["model releases", "coding tools", "pricing"])
    monkeypatch.setattr(radar, "_web_candidates", lambda themes, limit=12: [])
    monkeypatch.setattr(radar, "recent_post_topics", lambda: [])
    monkeypatch.setattr(radar, "record_post_topic", lambda topic, lane=None: None)

    grounding = radar.creation_grounding(object(), budget_seconds=0.1)
    assert grounding, "the selector went silent"
    assert "Cowork" in str(grounding["topic"]), (
        f"a watched account lost to {grounding['topic']!r}")
    assert grounding.get("watched_handle") == "@AnthropicAI"
