"""Adversarial cover for the extension connect flow and device tokens (CONTRACTS section 9)."""
from __future__ import annotations

import sys
import types
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest

from starling_server.account import deletion
from starling_server.devices import connect

from ..conftest import PUBLIC_URL, TEST_EXTENSION_ID
from .helpers import (
    account_row,
    api_headers,
    approve_connect,
    connect_device,
    connect_params,
    context_value,
    create_user,
    csrf_token,
    open_connect,
    pkce_pair,
    post_form,
    redeem,
    redirect_uri,
)

OTHER_EXTENSION = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def fake_billing(monkeypatch):
    cancelled: list[str] = []
    module = types.ModuleType("starling_server.billing.service")
    module.cancel_for_deletion = lambda db, settings, account_id: cancelled.append(account_id)
    monkeypatch.setitem(sys.modules, "starling_server.billing.service", module)
    return cancelled


def set_status(app, email: str, status: str) -> None:
    app.state.db.write(
        lambda conn: conn.execute("UPDATE accounts SET status = ? WHERE email_norm = ?", (status, email))
    )


def decision(request_id: str, state: str) -> dict[str, str]:
    return {"connect_request_id": request_id, "connect_state": state}


# --------------------------------------------------------------------------- the authorization request


@pytest.mark.parametrize(
    "uri",
    [
        f"http://{TEST_EXTENSION_ID}.chromiumapp.org/connect",  # not https
        f"https://{TEST_EXTENSION_ID}.chromiumapp.org/connect/",  # trailing slash
        f"https://{TEST_EXTENSION_ID}.chromiumapp.org/connect#x",
        f"https://{TEST_EXTENSION_ID}.chromiumapp.org.evil.example/connect",
        f"https://{TEST_EXTENSION_ID}.chromiumapp.org:443/connect",
        f"https://{TEST_EXTENSION_ID.upper()}.chromiumapp.org/connect",
        f"https://evil.example/?x=https://{TEST_EXTENSION_ID}.chromiumapp.org/connect",
        f"//{TEST_EXTENSION_ID}.chromiumapp.org/connect",
        "",
    ],
)
def test_the_return_address_must_match_the_extension_exactly(app, client, uri):
    create_user(client, "ana@example.com")
    _, challenge = pkce_pair()
    response = client.get("/connect", params=connect_params(challenge, redirect_uri=uri), follow_redirects=False)
    assert response.status_code == 400 and "location" not in response.headers
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM connect_requests").fetchone()[0] == 0


def test_an_unapproved_request_expires(app, client, fake_clock):
    create_user(client, "ana@example.com")
    _, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    fake_clock.advance(connect.REQUEST_TTL_S + 1)
    refused = post_form(client, "/connect/approve", decision(request_id, state), token=csrf_token(page))
    assert refused.status_code == 400 and "location" not in refused.headers
    with app.state.db.read() as conn:
        assert conn.execute("SELECT code_hash FROM connect_requests WHERE id = ?", (request_id,)).fetchone()[0] is None


def test_approving_twice_issues_only_one_code(app, client):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    first = post_form(client, "/connect/approve", decision(request_id, state), token=csrf_token(page))
    assert first.status_code == 303
    second = post_form(client, "/connect/approve", decision(request_id, state), token=csrf_token(page))
    assert second.status_code == 400 and "location" not in second.headers
    code = parse_qs(urlsplit(first.headers["location"]).query)["code"][0]
    assert redeem(client, code, verifier).status_code == 200


def test_the_code_never_leaves_the_server_in_a_referrer_or_a_cache(client):
    create_user(client, "ana@example.com")
    _, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    assert page.headers["referrer-policy"] == "no-referrer"
    approved = post_form(client, "/connect/approve", decision(request_id, state), token=csrf_token(page))
    assert approved.headers["referrer-policy"] == "no-referrer"
    assert approved.headers["cache-control"] == "no-store"


def test_a_connect_request_is_not_readable_from_its_own_row(app, client):
    """The state and the code are bearer values; only their digests may be stored."""
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    approved = post_form(client, "/connect/approve", decision(request_id, state), token=csrf_token(page))
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    with app.state.db.read() as conn:
        row = conn.execute("SELECT * FROM connect_requests WHERE id = ?", (request_id,)).fetchone()
    stored = " ".join(str(value) for value in tuple(row))
    assert state not in stored and code not in stored and verifier not in stored
    assert len(row["state_hash"]) == 64 and len(row["code_hash"]) == 64


# --------------------------------------------------------------------------- PKCE


@pytest.mark.parametrize(
    "verifier",
    [
        "short",
        "x" * 42,  # one under the RFC minimum
        "x" * 129,  # one over the maximum
        "",
        "not base64url! with spaces and /+=",
        "ünicode" * 10,
    ],
)
def test_a_verifier_outside_the_pkce_rules_is_refused(client, verifier):
    create_user(client, "ana@example.com")
    code, _ = approve_connect(client, connect_params(pkce_pair()[1]))
    assert redeem(client, code, verifier).status_code == 400


def test_the_challenge_must_be_the_hash_of_the_verifier_not_the_verifier(client):
    """A `plain` style exchange (challenge == verifier) must never be accepted."""
    create_user(client, "ana@example.com")
    verifier = "x" * 43
    code, _ = approve_connect(client, connect_params(verifier))  # the "challenge" is the verifier itself
    assert redeem(client, code, verifier).status_code == 400


def test_two_parallel_redemptions_of_one_code_issue_one_device(app, client, fake_state):
    """A racing extension (or an attacker with the code) must not get a second device token."""
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))

    def submit(_index: int):
        return redeem(client, code, verifier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(submit, 0), pool.submit(submit, 1)]]
    assert sorted(response.status_code for response in results) == [200, 400]
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
        # The replay revoked the device the first redemption created (RFC 6749 section 4.1.2).
        assert conn.execute("SELECT revoked_reason FROM devices").fetchone()[0] == "code_replay"


def test_a_verifier_for_another_request_is_refused(client):
    create_user(client, "ana@example.com")
    first_verifier, first_challenge = pkce_pair()
    second_verifier, second_challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(first_challenge))
    approve_connect(client, connect_params(second_challenge))
    assert redeem(client, code, second_verifier).status_code == 400
    assert redeem(client, code, first_verifier).status_code == 400  # the attempt spent the code


# --------------------------------------------------------------------------- account status


def test_an_unverified_account_may_still_connect(app, client, fake_state):
    """Contract section 9.6: connecting does not need a verified address."""
    create_user(client, "ana@example.com", verified=False)
    device = connect_device(client)
    assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 200


def test_a_suspended_account_cannot_connect_a_new_device(app, client, fake_state):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    set_status(app, "ana@example.com", "suspended")
    refused = post_form(client, "/connect/approve", decision(request_id, state), token=csrf_token(page))
    assert refused.status_code == 400
    # And the page itself stops offering the approval.
    _, challenge2 = pkce_pair()
    assert client.get("/connect", params=connect_params(challenge2), follow_redirects=False).status_code == 400


def test_a_code_cannot_be_redeemed_after_the_account_is_suspended(app, client, fake_state):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))
    set_status(app, "ana@example.com", "suspended")
    refused = redeem(client, code, verifier)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "account_suspended"


def test_a_code_cannot_be_redeemed_after_the_account_is_deleted(app, client, settings, fake_state, fake_pool, fake_billing):
    create_user(client, "ana@example.com")
    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))
    anyio.run(deletion.delete_account, app.state.db, settings, fake_pool, account_row(app, "ana@example.com")["id"])
    refused = redeem(client, code, verifier)
    assert refused.status_code == 400
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0


def test_a_device_token_dies_the_moment_the_account_starts_deleting(app, client, fake_state):
    create_user(client, "ana@example.com")
    device = connect_device(client)
    assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 200
    set_status(app, "ana@example.com", "deleting")
    refused = client.get("/api/v1/session", headers=api_headers(device["token"]))
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "signed_out"


# --------------------------------------------------------------------------- many devices


def test_many_devices_stay_independent_and_are_revoked_one_at_a_time(app, client, fake_state):
    create_user(client, "ana@example.com")
    devices = [connect_device(client) for _ in range(8)]
    assert len({device["device_id"] for device in devices}) == 8
    assert len({device["token"] for device in devices}) == 8
    page = client.get("/account/devices")
    assert len(context_value(page, "devices")) == 8
    for device in devices:
        assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 200

    victim = devices[3]
    assert post_form(client, f"/account/devices/{victim['device_id']}/revoke", {}).status_code == 303
    assert client.get("/api/v1/session", headers=api_headers(victim["token"])).status_code == 401
    for device in devices:
        if device is victim:
            continue
        assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 200
    assert len(context_value(client.get("/account/devices"), "devices")) == 7


def test_a_device_of_another_account_cannot_be_revoked(app, client, second_client, fake_state):
    create_user(client, "ana@example.com")
    create_user(second_client, "bob@example.com")
    victim = connect_device(second_client)
    response = post_form(client, f"/account/devices/{victim['device_id']}/revoke", {})
    assert response.status_code == 303  # the page never says whether the id existed
    assert second_client.get("/api/v1/session", headers=api_headers(victim["token"])).status_code == 200


def test_device_tokens_are_never_stored_in_the_clear(app, client, fake_state):
    create_user(client, "ana@example.com")
    device = connect_device(client)
    with app.state.db.read() as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        found = []
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):
                if device["token"] in " ".join(str(value) for value in tuple(row)):
                    found.append(table)
    assert found == []


# --------------------------------------------------------------------------- the token endpoint


def test_the_token_endpoint_answers_shaped_errors_for_bad_requests(client):
    create_user(client, "ana@example.com")
    cases = [
        client.post("/api/v1/devices/token", content=b"not json", headers={"Content-Type": "application/json"}),
        client.post("/api/v1/devices/token", content=b"[1,2,3]", headers={"Content-Type": "application/json"}),
        client.post("/api/v1/devices/token", content=b"", headers={"Content-Type": "application/json"}),
        client.post("/api/v1/devices/token", data={"grant_type": "authorization_code"}),
        client.post("/api/v1/devices/token", json={"grant_type": "password"}),
        client.post("/api/v1/devices/token", json={"grant_type": "authorization_code", "extension_version": "1.0.0"}),
    ]
    for response in cases:
        assert response.status_code in (400, 426), response.text
        assert set(response.json()["error"]) >= {"code", "message"}
        assert "Traceback" not in response.text


def test_the_token_endpoint_refuses_an_oversized_body(client):
    create_user(client, "ana@example.com")
    body = {"grant_type": "authorization_code", "code": "x" * 43, "code_verifier": "y" * 100_000,
            "redirect_uri": redirect_uri(), "client_id": TEST_EXTENSION_ID, "extension_version": "1.0.0"}
    response = client.post("/api/v1/devices/token", json=body)
    assert response.status_code == 413 and response.json()["error"]["code"] == "payload_too_large"


def test_the_token_endpoint_ignores_website_cookies(app, client, fake_state):
    """Contract section 13.3: a signed-in browser session must not be able to mint a device token."""
    create_user(client, "ana@example.com")
    response = client.post("/api/v1/devices/token", json={"grant_type": "authorization_code", "code": "x" * 43,
                                                          "code_verifier": "y" * 43, "redirect_uri": redirect_uri(),
                                                          "client_id": TEST_EXTENSION_ID, "extension_version": "1.0.0"})
    assert response.status_code == 400
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
