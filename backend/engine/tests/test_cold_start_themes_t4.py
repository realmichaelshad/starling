"""T4: a new customer gets topics from what they told Starling, before they have posts.

`radar._account_themes` read only the account's own posts. With none, it returned no themes,
`_web_candidates` returned nothing, and a new customer pressing Create got "no signal" on a
day full of news in their lane.
"""
from __future__ import annotations

import time

import httpx
import pytest

from app import cloud, storage
from app import own_material as om
from app import radar as R
from app import subject

VOCABULARY = set(subject._THEMES)


@pytest.fixture()
def no_posts(monkeypatch):
    monkeypatch.setattr(storage, "performance_records", lambda: [])


def test_nothing_said_and_nothing_posted_is_still_no_themes(no_posts):
    assert R._account_themes() == []


def test_the_bio_seeds_the_themes_of_a_new_account(no_posts):
    storage.set_account_profile({"bio": "I build coding agents and write about model releases and API pricing.",
                                 "audience": "developers", "content_pillars": []})
    themes = R._account_themes()
    assert {"coding tools", "model releases", "pricing"} <= set(themes)
    assert set(themes) <= VOCABULARY, "a theme is a name from the fixed vocabulary, never his words"


def test_topic_choices_and_background_notes_count_for_selection(no_posts):
    storage.remember_topic_focus("running models locally on a GPU", source_text="let's do local models")
    om.add(om.KIND_BACKGROUND, "I only care about self-hosted inference and open weights.")
    assert "running models locally" in R._account_themes()


def test_the_latest_captured_profile_page_counts(no_posts):
    storage.save_profile_snapshot({"handle": "newcomer", "bio": "Benchmarks, evals and leaderboards, daily.",
                                   "source": "extension"})
    assert "benchmarks" in R._account_themes()


def test_a_food_blogger_does_not_inherit_the_coder_vocabulary(no_posts):
    storage.set_account_profile({"bio": "I bake sourdough and share bread recipes from my kitchen.",
                                 "audience": "home bakers", "content_pillars": []})
    assert R._account_themes() == []


def test_an_account_with_its_own_posts_is_read_from_its_posts(monkeypatch):
    posts = [{"text": f"The new pricing tier costs less than the old subscription, take {n}"} for n in range(12)]
    monkeypatch.setattr(storage, "performance_records", lambda: posts)
    storage.set_account_profile({"bio": "Benchmarks, evals and leaderboards.", "audience": "", "content_pillars": []})
    themes = R._account_themes()
    assert "pricing" in themes and "benchmarks" not in themes, "twelve posts outrank a bio"


def test_a_new_account_with_a_bio_gets_a_candidate_from_the_pool(no_posts, monkeypatch):
    """End to end through the real beat filters: bio -> themes -> a candidate."""
    storage.set_account_profile({"bio": "I build with coding agents and write about the tools.",
                                 "audience": "developers", "content_pillars": []})
    story = {"title": "Anthropic releases Claude Code 2.0 with background agents",
             "url": "https://example.com/claude-code-2", "source": "Example Feed",
             "summary": "The coding agent can now run tasks in the background while you work.",
             "published_at": int(time.time()) - 900, "points": 0}
    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("STARLING_RESEARCH", "proxy")
    cloud.set_services_token("n" * 43)
    monkeypatch.setattr(cloud, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"generated_at": int(time.time()), "items": [story]})),
        base_url="http://services"))
    import app.release_radar as release_radar
    monkeypatch.setattr(release_radar, "recent_releases", lambda **kwargs: [])
    themes = R._account_themes()
    assert themes
    topics = [candidate["topic"] for candidate in R._web_candidates(themes)]
    assert any("Claude Code" in topic for topic in topics), topics
