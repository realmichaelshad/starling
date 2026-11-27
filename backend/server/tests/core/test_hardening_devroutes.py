"""`/_dev/outbox` returns email bodies, and those carry live password-reset links.

Found by the security review: the route was gated on `STARLING_ENV` alone, and that
variable DEFAULTS to development. A deployment that set everything else correctly (https address,
real SMTP, live keys) and merely forgot `STARLING_ENV` started without a single warning and handed
any visitor the reset link of any account: `POST /forgot-password`, then
`GET /_dev/outbox?to=<victim>`. The route now needs three independent conditions, and a
deployment-shaped configuration outside production is reported at start-up.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.config import load_settings, validate

from ..conftest import PUBLIC_URL, base_env

# A made-up public host. Nothing here ever connects to it.
DEPLOYED_URL = "https://app.starling-hosting.com"


def outbox_status(env: dict[str, str], base_url: str) -> int:
    with TestClient(create_app(load_settings(env)), base_url=base_url) as client:
        return client.get("/_dev/outbox", params={"to": "victim@example.com"}).status_code


def test_a_deployment_that_forgot_starling_env_does_not_serve_the_outbox(tmp_path):
    env = dict(base_env(tmp_path / "data"), STARLING_PUBLIC_URL=DEPLOYED_URL, EMAIL_MODE="smtp",
               SMTP_HOST="smtp.starling-hosting.com", SMTP_FROM="Starling <hello@starling-hosting.com>")
    env.pop("STARLING_ENV")
    settings = load_settings(env)
    assert settings.env == "development", "the default this test exists for"
    assert settings.dev_tools_enabled is False
    assert outbox_status(env, DEPLOYED_URL) == 404


@pytest.mark.parametrize("override", [
    {"STARLING_PUBLIC_URL": DEPLOYED_URL},                       # a public address, outbox mode
    {"STARLING_PUBLIC_URL": "http://192.168.1.20:8765"},         # another machine on the network
    {"EMAIL_MODE": "smtp", "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "Starling <hello@example.com>"},
])
def test_each_condition_is_enough_on_its_own_to_close_the_route(tmp_path, override):
    env = dict(base_env(tmp_path / "data"), **override)
    assert outbox_status(env, env["STARLING_PUBLIC_URL"]) == 404


@pytest.mark.parametrize("environment", ["development", "e2e"])
def test_a_local_run_still_has_it(tmp_path, environment):
    """Negative control: the journeys and every test that reads an email depend on this."""
    env = dict(base_env(tmp_path / "data"), STARLING_ENV=environment)
    assert outbox_status(env, PUBLIC_URL) == 200
    with TestClient(create_app(load_settings(env)), base_url=PUBLIC_URL) as client:
        assert client.get("/_dev/outbox", params={"to": "nobody@example.com"}).json() == {"messages": []}


def test_development_mode_on_a_public_address_is_never_silent(tmp_path):
    env = dict(base_env(tmp_path / "data"), STARLING_PUBLIC_URL=DEPLOYED_URL)
    problems = validate(load_settings(env), "gateway")
    assert any("production checks are OFF" in problem and "STARLING_ENV=production" in problem for problem in problems), problems
    # ...and a local run is not nagged.
    assert not any("production checks are OFF" in p for p in validate(load_settings(base_env(tmp_path / "local")), "gateway"))


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_addresses_are_recognised(tmp_path, host):
    settings = load_settings(dict(base_env(tmp_path / "data"), STARLING_PUBLIC_URL=f"http://{host}:8765"))
    assert settings.public_url_is_loopback is True


@pytest.mark.parametrize("url", ["https://127.0.0.1.evil.example", "https://localhost.evil.example", "http://127.0.0.2:8765",
                                 "https://app.example.com", "http://0.0.0.0:8765"])
def test_lookalike_addresses_are_not_loopback(tmp_path, url):
    assert load_settings(dict(base_env(tmp_path / "data"), STARLING_PUBLIC_URL=url)).public_url_is_loopback is False
