"""Findings of the security review in the services process and its launcher."""
from __future__ import annotations

import asyncio

import pytest

from starling_server import supervisor
from starling_server.engines import spawner
from starling_server.services.world.safe_fetch import FetchError, SafeFetcher


def test_the_services_process_never_receives_a_gateway_secret():
    """`STARLING_SECRET_KEY_PREVIOUS` (live for the 30 days of a rotation) matched neither the two
    excluded names nor the two prefixes, so the process that talks to the internet inherited the
    key that signs sessions and encrypts the owner's two-factor secret."""
    inherited = {
        "STARLING_SECRET_KEY": "current", "STARLING_SECRET_KEY_PREVIOUS": "previous", "BACKUP_PASSPHRASE": "passphrase",
        "STRIPE_SECRET_KEY": "sk", "STRIPE_WEBHOOK_SECRET": "whsec", "STRIPE_WEBHOOK_SECRET_PREVIOUS": "whsec-old",
        "SMTP_PASSWORD": "smtp", "SOME_FUTURE_SECRET": "unknown today", "OWNER_DB_PASSWORD": "also unknown",
        # what it does need
        "AI_API_KEY": "ai", "AI_API_KEY_SECONDARY": "ai-2", "STARLING_SERVICES_TOKEN_KEY": "token-key", "STARLING_ENV": "production",
        "PATH": "/usr/bin",
    }
    passed = supervisor.services_environment(inherited)
    assert set(passed) == {"AI_API_KEY", "AI_API_KEY_SECONDARY", "STARLING_SERVICES_TOKEN_KEY", "STARLING_ENV", "PATH"}


def test_the_previous_secret_key_is_a_secret_to_engines_too():
    assert spawner.is_secret_env_name("STARLING_SECRET_KEY_PREVIOUS")
    parent = {"STARLING_SECRET_KEY_PREVIOUS": "p" * 44}
    with pytest.raises(Exception):
        spawner.assert_engine_environment_clean({"HARMLESS": "prefix-" + "p" * 44}, parent)


@pytest.mark.parametrize("url", [
    "http://%31%32%37%2e%30%2e%30%2e%31/",          # 127.0.0.1, percent-encoded
    "https://%6c%6f%63%61%6c%68%6f%73%74/admin",    # localhost
    "http://exa%6dple.com/",
])
def test_a_percent_encoded_host_never_reaches_the_resolver(url):
    """Neither an IP literal nor a numeric label to the checks, so where it led depended on the resolver."""
    resolved: list[str] = []

    async def resolver(host: str, port: int):
        resolved.append(host)
        return ["93.184.216.34"]

    with pytest.raises(FetchError):
        asyncio.run(SafeFetcher(resolver=resolver).validate(url))
    assert resolved == []


def test_an_ordinary_host_still_resolves():
    async def resolver(host: str, port: int):
        return ["93.184.216.34"]

    target = asyncio.run(SafeFetcher(resolver=resolver).validate("https://example.com/page"))
    assert target is not None


@pytest.mark.parametrize("status", ["suspended", "deleting"])
def test_a_token_stops_working_on_the_fetcher_when_its_account_does(services, status):
    """A services token lives for a day and cannot be revoked, so the ACCOUNT is checked. The AI proxy
    always did; the page and image fetcher did not, so a token taken from a compromised engine kept
    fetching from the owner's address after the owner had suspended the customer."""
    from starling_server.services import auth as services_auth

    account = services.add_account()
    headers = services.auth(account)
    assert services.client.get("/v1/news/headlines?limit=1", headers=headers).status_code == 200  # control

    services.db.write(lambda conn: conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account.id)))
    services.app.state.active_accounts.clear()  # past the 30 seconds an "active" answer is reused
    for method, path, body in (("GET", "/v1/news/headlines", None), ("POST", "/v1/fetch", {"url": "http://news.example/"}),
                               ("POST", "/v1/image", {"url": "http://news.example/a.png"}), ("POST", "/v1/summary", {"query_id": "x"})):
        response = services.client.request(method, path, json=body, headers=headers)
        assert response.status_code == 401, (path, response.status_code)
    assert services_auth.ACTIVE_CACHE_S <= 60, "a suspension must bite within a minute"


def test_an_account_that_never_existed_is_refused_and_the_operator_token_is_not_checked(services):
    ghost = services.auth(type("Ghost", (), {"id": "00000000-0000-4000-8000-000000000000"})())
    assert services.client.get("/v1/news/headlines?limit=1", headers=ghost).status_code == 401
    assert services.client.get("/v1/news/headlines?limit=1", headers=services.auth(None)).status_code == 200
