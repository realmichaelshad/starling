"""Two ceilings nobody should ever meet by accident: sign-ups per hour for the whole site, and
connected browsers per account. Both come from the environment and 0 switches either one off."""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from starling_server.config import ConfigError, load_settings

from ..conftest import PUBLIC_URL, base_env
from .helpers import (
    connect_device,
    connect_params,
    context_value,
    create_user,
    open_connect,
    pkce_pair,
    post_form,
    signup,
)


def with_settings(app, **changes):
    app.state.settings = dataclasses.replace(app.state.settings, **changes)
    return app.state.settings


# --------------------------------------------------------------------------- sign-ups, site wide


def network(app, address: str) -> TestClient:
    """A visitor on their own network: the per-network rule keys on the /24 of this address."""
    return TestClient(app, base_url=PUBLIC_URL, client=(address, 5000))


def emails(app) -> list[str]:
    with app.state.db.read() as conn:
        return [row["email"] for row in conn.execute("SELECT email FROM accounts")]


def test_the_site_wide_sign_up_ceiling_stops_a_flood_spread_over_many_networks(app):
    with_settings(app, signup_global_per_hour=3)
    for index in range(3):
        # Every attempt from a different network: the per-network rule never sees two of them.
        assert signup(network(app, f"198.51.{index}.7"), f"flood{index}@example.com").status_code == 303

    refused = signup(network(app, "198.51.99.7"), "flood3@example.com")
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
    assert "busy right now" in refused.text
    assert "flood3@example.com" not in emails(app)


def test_zero_switches_the_site_wide_ceiling_off(app):
    with_settings(app, signup_global_per_hour=0)
    for index in range(6):
        assert signup(network(app, f"203.0.{index}.9"), f"open{index}@example.com").status_code == 303


def test_one_noisy_network_cannot_spend_everybody_elses_sign_ups(app):
    """The per-network rule answers first, so its refusals never reach the site-wide counter."""
    with_settings(app, signup_global_per_hour=12)
    noisy = network(app, "192.0.2.44")
    for index in range(10):
        assert signup(noisy, f"noisy{index}@example.com").status_code == 303
    for index in range(5):
        assert signup(noisy, f"noisier{index}@example.com").status_code == 429  # its own network's limit
    # Ten of the twelve are used; two other networks still get in, and a third is the first refused.
    assert signup(network(app, "198.51.100.1"), "elsewhere1@example.com").status_code == 303
    assert signup(network(app, "198.51.101.1"), "elsewhere2@example.com").status_code == 303
    assert signup(network(app, "198.51.102.1"), "elsewhere3@example.com").status_code == 429


# --------------------------------------------------------------------------- browsers per account


def test_an_account_at_its_device_limit_is_told_what_to_do_and_loses_nothing(app, client, fake_state):
    with_settings(app, max_devices_per_account=3)
    create_user(client, "ana@example.com")
    devices = [connect_device(client) for _ in range(3)]

    verifier, challenge = pkce_pair()
    params = connect_params(challenge)
    _page, request_id, state = open_connect(client, params)
    assert state == params["state"]
    refused = post_form(client, "/connect/approve", {"connect_request_id": request_id, "connect_state": params["state"]})
    assert refused.status_code in (200, 400, 409)
    assert "already has 3 connected browsers" in refused.text
    assert "Devices" in refused.text
    assert len(context_value(client.get("/account/devices"), "devices")) == 3  # nothing was added or removed

    # Disconnect one, press Approve on the SAME request: it goes through.
    assert post_form(client, f"/account/devices/{devices[0]['device_id']}/revoke", {}).status_code == 303
    approved = post_form(client, "/connect/approve", {"connect_request_id": request_id, "connect_state": params["state"]})
    assert approved.status_code == 303
    assert approved.headers["location"].startswith("https://")


def test_zero_switches_the_device_limit_off(app, client, fake_state):
    with_settings(app, max_devices_per_account=0)
    create_user(client, "ana@example.com")
    assert len({connect_device(client)["device_id"] for _ in range(5)}) == 5


def test_a_disconnected_browser_does_not_count(app, client, fake_state):
    with_settings(app, max_devices_per_account=2)
    create_user(client, "ana@example.com")
    first = connect_device(client)
    connect_device(client)
    assert post_form(client, f"/account/devices/{first['device_id']}/revoke", {}).status_code == 303
    assert connect_device(client)["device_id"]  # the freed place can be used again


# --------------------------------------------------------------------------- configuration


def test_the_ceilings_have_defaults_and_refuse_nonsense(tmp_path):
    env = base_env(tmp_path)
    settings = load_settings(env)
    assert (settings.signup_global_per_hour, settings.max_devices_per_account) == (500, 20)
    for name in ("SIGNUP_GLOBAL_PER_HOUR", "MAX_DEVICES_PER_ACCOUNT"):
        with pytest.raises(ConfigError):
            load_settings({**env, name: "-1"})
        with pytest.raises(ConfigError):
            load_settings({**env, name: "plenty"})
