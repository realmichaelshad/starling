"""GET /api/v1/session prewarms the caller's engine: fire and forget, it never delays or fails the
session response and never evicts another customer's engine (real session route and account state)."""
from __future__ import annotations

import time

import httpx

from starling_server.api import session_routes

from .support import LiveServer, build_app, device_for, insert_account, make_pool, wait_until


def get(server: LiveServer, path: str, token: str) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    with httpx.Client(timeout=60, trust_env=False) as client:
        response = client.get(server.base_url + path, headers={"Authorization": f"Bearer {token}", "X-Starling-Extension": "1.0.0"})
    return response, time.monotonic() - started


def app_for(settings, pool, devices):
    return build_app(settings, pool, devices, routers=(session_routes.router,))


def test_session_answers_at_once_and_starts_the_engine_in_the_background(settings, database):
    account = insert_account(database, settings, 1)
    pool = make_pool(settings, database, fake_args=("--ready-delay", "2.0"))
    with LiveServer(app_for(settings, pool, {"device-a": device_for(account, 1)})) as server:
        response, elapsed = get(server, "/api/v1/session", "device-a")
        live_at_response = pool.is_live(account)
        assert response.status_code == 200 and response.json()["account"]["id"] == account
        assert elapsed < 1.5 and live_at_response is False  # not waiting for a 2-second engine start
        assert wait_until(lambda: pool.is_live(account), timeout=20)  # control: the prewarm did start it
    assert pool.spawn_count == 1


def test_session_prewarm_never_evicts_another_customers_engine(settings, database):
    a = insert_account(database, settings, 1)
    b = insert_account(database, settings, 2)
    pool = make_pool(settings, database, max_live=1)
    devices = {"device-a": device_for(a, 1), "device-b": device_for(b, 2)}
    with LiveServer(app_for(settings, pool, devices)) as server:
        own, _ = get(server, "/api/v1/own-material", "device-b")
        assert own.status_code == 200 and pool.is_live(b)
        response, _ = get(server, "/api/v1/session", "device-a")
        assert response.status_code == 200
        time.sleep(1.0)
        assert pool.is_live(b) and not pool.is_live(a) and pool.spawn_count == 1


def test_session_answers_when_the_prewarm_fails_or_raises(settings, database, monkeypatch):
    account = insert_account(database, settings, 1)
    pool = make_pool(settings, database, fake_args=("--exit-before-ready", "3"))
    with LiveServer(app_for(settings, pool, {"device-a": device_for(account, 1)})) as server:
        response, _ = get(server, "/api/v1/session", "device-a")
        assert response.status_code == 200
        assert wait_until(lambda: pool.spawn_count == 1, timeout=10)
        assert not pool.is_live(account)

        def broken(*args, **kwargs):
            raise RuntimeError("prewarm is broken")

        monkeypatch.setattr(pool, "prewarm", broken)
        again, _ = get(server, "/api/v1/session", "device-a")
        assert again.status_code == 200


def test_session_does_not_prewarm_without_a_current_disclosure_consent(settings, database):
    account = insert_account(database, settings, 1, consent=False)
    pool = make_pool(settings, database)
    with LiveServer(app_for(settings, pool, {"device-a": device_for(account, 1)})) as server:
        response, _ = get(server, "/api/v1/session", "device-a")
        assert response.status_code == 200 and response.json()["consent"]["accepted_version"] is None
        time.sleep(0.8)
        assert pool.spawn_count == 0


def test_session_does_not_prewarm_an_unconfirmed_account():
    """It is refused on every draft, so a warm engine would only hold a slot someone else needs."""
    state = {
        "account": {"status": "active", "email_verified": False},
        "consent": {"accepted_version": "v1", "required_version": "v1"},
        "service": {"status": "ok"},
    }
    assert session_routes._worth_prewarming(state) is False
    state["account"]["email_verified"] = True
    assert session_routes._worth_prewarming(state) is True


def test_a_session_call_keeps_a_running_engine_from_idling_out(settings, database):
    """Someone reading X with the extension open is about to draft: their engine counts as used."""
    account = insert_account(database, settings, 1)
    pool = make_pool(settings, database)
    with LiveServer(app_for(settings, pool, {"device-a": device_for(account, 1)})) as server:
        get(server, "/api/v1/session", "device-a")
        assert wait_until(lambda: pool.is_live(account), timeout=20)
        entry = pool._engines[account]
        entry.last_used -= 1000  # as if it had sat idle for a while
        idle_since = entry.last_used
        response, _ = get(server, "/api/v1/session", "device-a")
        assert response.status_code == 200
        assert pool._engines[account].last_used > idle_since + 900
    assert pool.spawn_count == 1
