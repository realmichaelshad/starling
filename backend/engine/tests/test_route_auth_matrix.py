"""Only the contract's routes exist, and none answers without the worker token.

CONTRACTS §3.2 and §3.3: every request carries `Authorization: Bearer
<worker_token>`, anything else gets the default-deny 404, and every route not in
the table returns 404 too. This walks the router itself, so a route added later
is covered the moment it is registered.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route, WebSocketRoute

from app import gate, main, security

CONTRACT_ROUTES = {
    ("GET", "/livez"),
    ("POST", "/browser/assist"),
    ("POST", "/browser/assist/stream"),
    ("POST", "/snapshots/profile"),
    ("POST", "/snapshots/metrics"),
    ("POST", "/outcomes/record"),
    ("POST", "/images/feedback"),
    ("POST", "/preferences/confirm"),
    ("POST", "/own-material/list"),
    ("POST", "/own-material/add"),
    ("POST", "/own-material/remove"),
    ("GET", "/profile/export"),
    ("POST", "/internal/requests/{request_id}/cancel"),
}


@pytest.fixture()
def client():
    with TestClient(main.app) as running:
        yield running


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {security.ensure_token()}"}


def _registered() -> set[tuple[str, str]]:
    found = set()
    for route in main.app.routes:
        if isinstance(route, WebSocketRoute):
            found.add(("WEBSOCKET", route.path))
        elif isinstance(route, Route):
            for method in route.methods or ():
                if method != "HEAD":
                    found.add((method, route.path))
    return found


def test_only_the_contract_routes_exist():
    assert _registered() == CONTRACT_ROUTES


def test_every_route_is_closed_without_a_token(client):
    exposed = []
    for method, path in sorted(CONTRACT_ROUTES):
        concrete = path.replace("{request_id}", "res_1")
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = client.request(method, concrete, headers=headers)
            if response.status_code != 404 or response.content != b"":
                exposed.append(f"{method} {concrete} -> {response.status_code}")
    assert not exposed, "these answer without the worker token:\n  " + "\n  ".join(exposed)


def test_the_public_allowlist_is_empty():
    assert gate.PUBLIC_PATHS == frozenset()


def test_a_real_route_and_a_missing_one_are_indistinguishable(client):
    real = client.get("/profile/export")
    missing = client.get("/no-such-route-at-all")
    wrong = client.get("/profile/export", headers={"Authorization": "Bearer wrong"})
    for response in (real, missing, wrong):
        assert response.status_code == 404
        assert response.content == b""
        assert "www-authenticate" not in response.headers


def test_the_worker_token_reaches_livez(client):
    response = client.get("/livez", headers=_auth())
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("method, path", [
    ("GET", "/health"), ("POST", "/bootstrap/status"), ("POST", "/update/status"),
    ("POST", "/model/provider/get"), ("POST", "/model/local/set"), ("POST", "/import"),
    ("GET", "/bridge/capabilities"), ("DELETE", "/data"), ("GET", "/generations"),
    ("POST", "/strategy/chat"), ("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/redoc"),
])
def test_removed_routes_are_gone_even_with_the_token(client, method, path):
    response = client.request(method, path, headers=_auth())
    assert response.status_code in (404, 405)
    assert response.status_code == 404 or method == "POST"


def test_there_is_no_cors_and_no_preflight_oracle(client):
    preflight = {"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
                 "Access-Control-Request-Method": "POST"}
    real = client.options("/browser/assist", headers=preflight)
    missing = client.options("/no-such-route-at-all", headers=preflight)
    assert (real.status_code, real.content) == (missing.status_code, missing.content) == (404, b"")
    ok = client.get("/livez", headers={**_auth(), "Origin": "https://example.com"})
    assert ok.status_code == 200
    assert "access-control-allow-origin" not in {key.lower() for key in ok.headers}


def test_the_framework_is_not_announced(client):
    assert "server" not in {key.lower() for key in client.get("/livez", headers=_auth()).headers}


def test_a_websocket_handshake_is_refused(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/livez"):
            pass
