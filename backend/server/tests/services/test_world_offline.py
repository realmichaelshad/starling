"""`STARLING_WORLD_OFFLINE` (CONTRACTS 4.2, 12 and 13.11).

With the switch on, the services process makes no outbound connection at all: the news pool's
refresh task never starts, and `/v1/fetch`, `/v1/image` and `/v1/summary` fail at once with the
world service's own `fetch_failed` code. Nothing else changes: the AI proxy and the headline
route keep working, and the switch is refused in production.
"""
from __future__ import annotations

import asyncio
import ssl

import pytest

from starling_server.config import load_settings, validate
from starling_server.services.world.safe_fetch import FetchError, SafeFetcher

from .fakes_net import RecordingBackend, ScriptedResolver, http_response

PUBLIC_V4 = "93.184.216.34"
PAGE = b"<!doctype html><html><head><title>Example</title></head><body><article><p>" + b"word " * 40 + b"</p></article></body></html>"


def offline_services(make_services, **app_kwargs):
    """A services app with the switch on, plus the resolver and backend it must never use."""
    resolver = ScriptedResolver({"news.example": [[PUBLIC_V4]]})
    backend = RecordingBackend(lambda a, p, r: http_response(200, PAGE, {"Content-Type": "text/html"}))
    services = make_services(
        overrides={"STARLING_WORLD_OFFLINE": "true"},
        resolver=resolver,
        network_backend=backend,
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        **app_kwargs,
    )
    return services, resolver, backend


def test_fetch_and_image_fail_fast_without_resolving_or_connecting(make_services):
    services, resolver, backend = offline_services(make_services)
    headers = services.auth(services.add_account())
    for path in ("/v1/fetch", "/v1/image"):
        response = services.client.post(path, json={"url": "https://news.example/story"}, headers=headers)
        assert response.status_code == 502, path
        assert response.json()["error"]["code"] == "fetch_failed"
    assert resolver.calls == []
    assert backend.connections == []


def test_summary_is_refused_and_spends_no_ai_call(make_services):
    services, _, _ = offline_services(make_services)
    headers = services.auth(services.add_account())
    response = services.client.post("/v1/summary", json={"query_id": "coding-agents"}, headers=headers)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "fetch_failed"
    assert services.fake.requests == []
    assert services.rows("SELECT id FROM ai_calls") == []


def test_an_unknown_query_id_is_still_a_bad_request(make_services):
    services, _, _ = offline_services(make_services)
    headers = services.auth(services.add_account())
    response = services.client.post("/v1/summary", json={"query_id": "not-an-authored-id"}, headers=headers)
    assert response.status_code == 400 and response.json()["error"]["code"] == "bad_request"


def test_the_news_refresh_task_never_starts_and_headlines_stay_empty(make_services):
    services, resolver, backend = offline_services(make_services, start_background=True)
    assert services.app.state.world_offline is True
    assert services.app.state.news_task is None
    headers = services.auth(services.add_account())
    body = services.client.get("/v1/news/headlines", headers=headers).json()
    assert body == {"generated_at": 0, "items": []}
    assert resolver.calls == [] and backend.connections == []


def test_the_ai_proxy_is_untouched_by_the_switch(make_services):
    services, _, _ = offline_services(make_services)
    account = services.add_account()
    response = services.press(account)
    assert response.status_code == 200
    assert services.one("SELECT status FROM ai_calls")["status"] == "ok"


def test_without_the_switch_the_refresh_task_runs(make_services):
    resolver = ScriptedResolver({"news.example": [[PUBLIC_V4]]})
    backend = RecordingBackend(lambda a, p, r: http_response(200, PAGE, {"Content-Type": "text/html"}))
    services = make_services(
        resolver=resolver,
        network_backend=backend,
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        start_background=True,
    )
    assert services.app.state.world_offline is False
    assert services.app.state.news_task is not None


@pytest.mark.parametrize("role", ["gateway", "services"])
def test_production_refuses_the_switch(env, role):
    values = dict(env)
    values.update({"STARLING_ENV": "production", "STARLING_WORLD_OFFLINE": "true"})
    problems = validate(load_settings(values), role)
    assert any("STARLING_WORLD_OFFLINE" in problem for problem in problems)


def test_the_fetcher_refuses_before_it_validates_or_resolves_the_url():
    """The refusal comes first, so even a well-formed public URL costs no lookup."""
    resolver = ScriptedResolver({"news.example": [[PUBLIC_V4]]})
    backend = RecordingBackend(lambda a, p, r: http_response(200, PAGE, {"Content-Type": "text/html"}))
    fetcher = SafeFetcher(resolver=resolver, network_backend=backend, offline=True)
    with pytest.raises(FetchError) as refused:
        asyncio.run(fetcher.get("https://news.example/story", max_bytes=1024, timeout_s=5))
    assert refused.value.code == "fetch_failed" and refused.value.http_status == 502
    assert resolver.calls == [] and backend.connections == []
