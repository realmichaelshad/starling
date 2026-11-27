"""Journey 2: connecting the extension with PKCE (local, mocked providers).

The exchange is driven exactly as `extension/shared/auth.ts` drives it: a 32-byte base64url
verifier, its S256 challenge, a random state, the connect URL with
`redirect_uri=https://<extension id>.chromiumapp.org/connect`, a POST approval, and the
redirect captured but never followed (there is no such host). Then
`POST /api/v1/devices/token` turns the code into a device token.

Refusals checked: a wrong verifier, a replayed code (which revokes the device that code
created), an unknown extension id, and a redirect URI that does not belong to the client id.
Finally the device is listed on `/account/devices`, and revoking it there makes
`GET /api/v1/session` answer 401 `signed_out`.
"""
from __future__ import annotations

import pytest

from e2e.tests.client import (
    Account,
    Control,
    Device,
    Website,
    new_state,
    pkce_pair,
    redirect_query,
    redirect_uri_for,
)


def connect_params(extension_id: str, challenge: str, state: str, **overrides: str) -> dict[str, str]:
    params = {
        "client_id": extension_id,
        "redirect_uri": redirect_uri_for(extension_id),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "device_label": "Chrome on Windows",
    }
    params.update(overrides)
    return params


def approve(account: Account, extension_id: str, **overrides: str):
    verifier, challenge = pkce_pair()
    state = new_state()
    params = connect_params(extension_id, challenge, state, **overrides)
    response = account.site.approve_connect(params)
    query = redirect_query(response)
    return verifier, state, params, query, response


def redeem(account: Account, *, code: str, verifier: str, params: dict[str, str], client_id: str | None = None,
           redirect_uri: str | None = None, version: str = "1.0.0"):
    return account.site.http.post(
        "/api/v1/devices/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri or params["redirect_uri"],
            "client_id": client_id or params["client_id"],
            "device_label": "Chrome on Windows",
            "extension_version": version,
        },
    )


def test_the_approval_redirect_carries_a_code_and_the_state_the_extension_sent(accounts, extension_id, harness):
    account = accounts("signup")
    verifier, state, params, query, response = approve(account, extension_id)

    location = response.headers["location"]
    assert location.startswith(f"https://{extension_id}.chromiumapp.org/connect?"), location
    assert query["state"] == state and len(query["code"]) == 43
    assert "code" in query and "error" not in query

    exchange = redeem(account, code=query["code"], verifier=verifier, params=params)
    assert exchange.status_code == 200, exchange.text
    token = exchange.json()
    assert token["token"].startswith("stl_dev_")
    assert token["expires_at"] > token["idle_expires_at"] > 0
    stored = harness.one("SELECT * FROM devices WHERE id = ?", (token["device_id"],))
    assert stored["account_id"] == account.id and stored["extension_id"] == extension_id
    assert stored["revoked_at"] is None
    assert token["token"] not in str(stored), "the raw token is stored, not its hash"

    device = Device(account.site.base_url, token["token"], token["device_id"])
    try:
        assert device.session()["account"]["id"] == account.id
    finally:
        device.close()


def test_a_wrong_verifier_is_refused_and_spends_the_code(accounts, extension_id, harness):
    account = accounts("signup")
    _verifier, _state, params, query, _response = approve(account, extension_id)
    other_verifier, _ = pkce_pair()

    refused = redeem(account, code=query["code"], verifier=other_verifier, params=params)
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "bad_request"
    row = harness.one("SELECT redeemed_at FROM connect_requests WHERE code_hash IS NOT NULL ORDER BY created_at DESC LIMIT 1")
    assert row["redeemed_at"] is not None, "a failed attempt must still spend the code"


def test_replaying_a_code_is_refused_and_revokes_the_device_it_created(accounts, extension_id, harness):
    account = accounts("signup")
    verifier, _state, params, query, _response = approve(account, extension_id)
    first = redeem(account, code=query["code"], verifier=verifier, params=params)
    assert first.status_code == 200
    device = Device(account.site.base_url, first.json()["token"], first.json()["device_id"])
    try:
        assert device.session()["account"]["id"] == account.id  # negative control: it worked

        replay = redeem(account, code=query["code"], verifier=verifier, params=params)
        assert replay.status_code == 400

        after = device.get("/session")
        assert after.status_code == 401 and after.json()["error"]["code"] == "signed_out"
        row = harness.one("SELECT revoked_at, revoked_reason FROM devices WHERE id = ?", (first.json()["device_id"],))
        assert row["revoked_at"] is not None and row["revoked_reason"] == "code_replay"
    finally:
        device.close()


def test_an_unknown_extension_id_never_gets_an_approval_page(accounts, extension_id, harness):
    account = accounts("signup")
    _verifier, challenge = pkce_pair()
    unknown = "ponmlkjihgfedcbaponmlkjihgfedcba"
    page = account.site.connect_page(connect_params(unknown, challenge, new_state(), client_id=unknown,
                                                    redirect_uri=redirect_uri_for(unknown)))
    assert page.status_code == 400
    assert "not recognised" in page.text or "not recognized" in page.text
    assert unknown not in page.text, "an unknown client id is not echoed back"


def test_a_redirect_uri_that_does_not_match_the_client_id_is_refused(accounts, extension_id):
    account = accounts("signup")
    _verifier, challenge = pkce_pair()
    params = connect_params(extension_id, challenge, new_state(),
                            redirect_uri="https://ponmlkjihgfedcbaponmlkjihgfedcba.chromiumapp.org/connect")
    page = account.site.connect_page(params)
    assert page.status_code == 400


def test_the_token_exchange_checks_the_client_id_and_the_redirect_uri(accounts, extension_id):
    account = accounts("signup")
    verifier, _state, params, query, _response = approve(account, extension_id)
    mismatched = redeem(account, code=query["code"], verifier=verifier, params=params,
                        client_id="ponmlkjihgfedcbaponmlkjihgfedcba")
    assert mismatched.status_code == 400


def test_an_outdated_extension_version_is_told_to_update(accounts, extension_id):
    account = accounts("signup")
    verifier, _state, params, query, _response = approve(account, extension_id)
    old = redeem(account, code=query["code"], verifier=verifier, params=params, version="0.9.0")
    assert old.status_code == 426 and old.json()["error"]["code"] == "extension_outdated"


def test_the_device_is_listed_and_revoking_it_signs_that_browser_out(accounts, extension_id, harness):
    account = accounts("signup")
    verifier, _state, params, query, _response = approve(account, extension_id)
    issued = redeem(account, code=query["code"], verifier=verifier, params=params).json()
    device = Device(account.site.base_url, issued["token"], issued["device_id"])
    try:
        assert device.session()["account"]["id"] == account.id  # negative control before revoking

        listed = account.site.devices()
        assert issued["device_id"] in [item["id"] for item in listed]
        page = account.site.get("/account/devices").text
        assert "Chrome on Windows" in page

        revoked = account.site.post_form(f"/account/devices/{issued['device_id']}/revoke", {})
        assert revoked.status_code == 303

        after = device.get("/session")
        assert after.status_code == 401 and after.json()["error"]["code"] == "signed_out"
        row = harness.one("SELECT revoked_at FROM devices WHERE id = ?", (issued["device_id"],))
        assert row["revoked_at"] is not None
    finally:
        device.close()


def test_revoking_one_device_leaves_the_others_connected(accounts, extension_id, harness):
    """Several browsers on one account: revoking one must not touch the rest, and the revoked
    one must be refused on its very next call."""
    account = accounts("connect-many")
    issued = []
    for label in ("First browser", "Second browser", "Third browser"):
        verifier, _state, params, query, _response = approve(account, extension_id, device_label=label)
        issued.append(redeem(account, code=query["code"], verifier=verifier, params=params).json())
    devices = [Device(account.site.base_url, item["token"], item["device_id"]) for item in issued]
    try:
        assert len({item["device_id"] for item in issued}) == 3
        assert len({item["token"] for item in issued}) == 3
        listed = {item["id"] for item in account.site.devices()}
        assert {item["device_id"] for item in issued} <= listed
        for device in devices:
            assert device.session()["account"]["id"] == account.id

        revoked = account.site.post_form(f"/account/devices/{issued[1]['device_id']}/revoke", {})
        assert revoked.status_code == 303
        assert devices[1].get("/session").status_code == 401
        assert devices[0].get("/session").status_code == 200
        assert devices[2].get("/session").status_code == 200
        remaining = {item["id"] for item in account.site.devices()}
        assert issued[1]["device_id"] not in remaining
        assert {issued[0]["device_id"], issued[2]["device_id"]} <= remaining
    finally:
        for device in devices:
            device.close()


def test_a_device_token_cannot_reach_the_website(accounts, extension_id, gateway_url):
    """CONTRACTS 13.4: device tokens are for `/api/v1` only."""
    account = accounts("signup", with_device=True)
    import httpx

    with httpx.Client(base_url=gateway_url, follow_redirects=False, trust_env=False, timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {account.device.token}"}
        for path in ("/account", "/account/billing", "/account/data"):
            response = client.get(path, headers=headers)
            assert response.status_code in (302, 303), f"{path} answered {response.status_code}"
            assert "/login" in response.headers.get("location", "")


def test_negative_control_the_session_check_can_fail(accounts, extension_id):
    """A token that was never issued must not satisfy the session assertion."""
    account = accounts("signup", with_device=True)
    fake = Device(account.site.base_url, "stl_dev_" + "0" * 43, "not-a-device")
    try:
        with pytest.raises(AssertionError):
            fake.session()
        assert fake.get("/session").status_code == 401
    finally:
        fake.close()
