"""STARLING_WORLD_FIXTURES (e2e only): listed pages and news stories served from a file, offline."""
from __future__ import annotations

import json

import pytest

from starling_server.config import load_settings, validate


@pytest.fixture
def fixture_file(tmp_path):
    path = tmp_path / "world.json"
    path.write_text(json.dumps({
        "pages": {"https://research.example.org/study": {
            "title": "A study of 400 pricing pages", "published": "2026-09-16",
            "text": "The study reviewed 400 pricing pages.\n\n61% offered a free trial."}},
        "news": [{"title": "Bootstrapped founders raised prices without churn rising",
                  "url": "https://news.example.com/pricing", "source": "Example SaaS Journal",
                  "summary": "Most who raised prices saw no rise in churn.", "published_ago_s": 3600, "points": 40}],
    }), encoding="utf-8")
    return path


def _fetch(services, account, url):
    return services.client.post("/v1/fetch", json={"url": url},
                                headers={"Authorization": f"Bearer {services.token(account.id)}"})


def test_an_offline_world_serves_the_fixture_pages_and_news(make_services, fixture_file):
    services = make_services(overrides={"STARLING_WORLD_OFFLINE": "true", "STARLING_WORLD_FIXTURES": str(fixture_file)})
    account = services.add_account("free")
    page = _fetch(services, account, "https://research.example.org/study")
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["title"] == "A study of 400 pricing pages" and "61% offered a free trial." in body["text"]
    assert _fetch(services, account, "https://research.example.org/elsewhere").status_code == 502, \
        "any other page fails as offline fetches do"
    news = services.client.get("/v1/news/headlines",
                               headers={"Authorization": f"Bearer {services.token(account.id)}"}).json()
    assert [item["url"] for item in news["items"]] == ["https://news.example.com/pricing"]
    assert news["generated_at"] > 0 and news["items"][0]["published_at"] > 0


def test_fixtures_are_ignored_by_a_process_that_is_online(make_services, fixture_file):
    services = make_services(overrides={"STARLING_WORLD_OFFLINE": "false", "STARLING_WORLD_FIXTURES": str(fixture_file)})
    assert services.app.state.world.pool.headlines()["items"] == []


def test_production_refuses_the_fixture_switch(env, fixture_file):
    settings = load_settings({**env, "STARLING_ENV": "production", "STARLING_WORLD_FIXTURES": str(fixture_file)})
    assert "STARLING_WORLD_FIXTURES is only for development and e2e" in validate(settings, "services")
