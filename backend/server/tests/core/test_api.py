"""/api/v1 session routes and device authentication (CONTRACTS 6 and 13.3, 13.4)."""
from __future__ import annotations

from starling_server.devices import tokens as device_tokens

from ..conftest import PUBLIC_URL
from .helpers import (
    PASSWORD,
    account_row,
    api_headers,
    connect_device,
    context_value,
    create_user,
    post_form,
    session_cookie,
)


def test_plans_are_public_and_carry_no_internal_fields(client):
    response = client.get("/api/v1/plans")
    assert response.status_code == 200
    body = response.json()
    assert body["unit"] == {"name": "draft", "label": "drafts"}
    assert [plan["code"] for plan in body["plans"]] == ["free", "starter", "pro"]
    starter = body["plans"][1]
    assert (starter["amount_cents"], starter["price_label"], starter["monthly_credits"]) == (900, "$9", 250)
    assert "stripe" not in response.text and "ceiling" not in response.text


def test_session_cookie_never_authenticates_the_api(client, settings, fake_state):
    create_user(client, "ana@example.com")
    assert session_cookie(client, settings)
    refused = client.get("/api/v1/session", headers={"X-Starling-Extension": "1.0.0"})
    assert refused.status_code == 401
    assert refused.json() == {"error": {"code": "signed_out", "message": "Sign in again to continue."}}
    assert client.post("/api/v1/consent", json={"version": settings.consent_version, "passive_learning": False}).status_code == 401
    device = connect_device(client)
    allowed = client.get("/api/v1/session", headers=api_headers(device["token"]))
    assert allowed.status_code == 200 and allowed.json()["account"]["email_verified"] is True


def test_device_token_cannot_reach_website_account_routes(app, client, second_client, fake_state, fake_pool):
    create_user(client, "ana@example.com")
    device = connect_device(client)
    headers = api_headers(device["token"])
    for path in ("/account", "/account/billing", "/account/devices", "/account/security", "/account/data"):
        response = second_client.get(path, headers=headers, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login?next=")
    for path in ("/account/delete", "/account/security/password", "/account/export", "/account/security/email"):
        response = second_client.post(path, headers={**headers, "Origin": PUBLIC_URL},
                                      data={"delete_password": PASSWORD, "delete_confirmation": "DELETE", "email_password": PASSWORD}, follow_redirects=False)
        assert response.status_code in (303, 403)
    assert account_row(app, "ana@example.com")["status"] == "active"
    assert fake_pool.deleted == [] and fake_pool.requests == []
    assert client.get("/account", follow_redirects=False).status_code == 200  # negative control: the session works


def test_revoked_device_is_signed_out(client, fake_state):
    create_user(client, "ana@example.com")
    device = connect_device(client)
    headers = api_headers(device["token"])
    revoked = client.post("/api/v1/devices/current/revoke", headers=headers)
    assert revoked.status_code == 200 and revoked.json() == {"ok": True}
    after = client.get("/api/v1/session", headers=headers)
    assert after.status_code == 401 and after.json()["error"]["code"] == "signed_out"
    assert after.headers["www-authenticate"].startswith("Bearer")


def test_revoking_from_the_devices_page(client, fake_state):
    create_user(client, "ana@example.com")
    first = connect_device(client)
    second = connect_device(client)
    page = client.get("/account/devices")
    assert {d["id"] for d in context_value(page, "devices")} == {first["device_id"], second["device_id"]}
    assert post_form(client, f"/account/devices/{first['device_id']}/revoke", {}).status_code == 303
    assert client.get("/api/v1/session", headers=api_headers(first["token"])).status_code == 401
    assert client.get("/api/v1/session", headers=api_headers(second["token"])).status_code == 200


def test_device_idle_expiry_slides_with_use(client, fake_clock, fake_state):
    create_user(client, "ana@example.com")
    headers = api_headers(connect_device(client)["token"])
    fake_clock.advance(device_tokens.IDLE_S - 60)
    assert client.get("/api/v1/session", headers=headers).status_code == 200
    fake_clock.advance(device_tokens.IDLE_S - 60)
    assert client.get("/api/v1/session", headers=headers).status_code == 200
    fake_clock.advance(device_tokens.IDLE_S + 1)
    assert client.get("/api/v1/session", headers=headers).status_code == 401


def test_device_absolute_expiry(client, fake_clock, fake_state):
    create_user(client, "ana@example.com")
    headers = api_headers(connect_device(client)["token"])
    for _ in range(6):
        fake_clock.advance(59 * 86400)
        assert client.get("/api/v1/session", headers=headers).status_code == 200
    fake_clock.advance(12 * 86400)
    assert client.get("/api/v1/session", headers=headers).status_code == 401


def test_last_used_is_written_at_most_every_ten_minutes(app, client, fake_clock, fake_state):
    create_user(client, "ana@example.com")
    device = connect_device(client)
    headers = api_headers(device["token"])

    def last_used():
        with app.state.db.read() as conn:
            return conn.execute("SELECT last_used_at FROM devices WHERE id = ?", (device["device_id"],)).fetchone()[0]

    client.get("/api/v1/session", headers=headers)
    first = last_used()
    assert first == fake_clock.value
    fake_clock.advance(300)
    client.get("/api/v1/session", headers=headers)
    assert last_used() == first
    fake_clock.advance(301)
    client.get("/api/v1/session", headers=headers)
    assert last_used() == fake_clock.value


def test_outdated_or_missing_extension_version_gets_426(client, settings, fake_state):
    create_user(client, "ana@example.com")
    token = connect_device(client)["token"]
    for version in ("0.9.0", None):
        headers = {"Authorization": f"Bearer {token}"}
        if version:
            headers["X-Starling-Extension"] = version
        response = client.get("/api/v1/session", headers=headers)
        assert response.status_code == 426 and response.json()["error"]["code"] == "extension_outdated"
    assert client.get("/api/v1/session", headers=api_headers(token, settings.extension_min_version)).status_code == 200


def test_suspended_account_is_refused_with_403(app, client, fake_state):
    create_user(client, "ana@example.com")
    headers = api_headers(connect_device(client)["token"])
    app.state.db.write(lambda c: c.execute("UPDATE accounts SET status = 'suspended' WHERE email_norm = 'ana@example.com'"))
    response = client.get("/api/v1/session", headers=headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "account_suspended"


def test_malformed_and_unknown_bearer_tokens(client):
    for value in (None, "Basic abc", "Bearer ", "Bearer stl_dev_short", "Bearer " + "stl_dev_" + "A" * 43):
        headers = {"X-Starling-Extension": "1.0.0"}
        if value:
            headers["Authorization"] = value
        response = client.get("/api/v1/session", headers=headers)
        assert response.status_code == 401 and response.json()["error"]["code"] == "signed_out"


def test_consent_accepts_only_the_current_version(app, client, settings, fake_state):
    create_user(client, "ana@example.com")
    headers = api_headers(connect_device(client)["token"])
    stale = client.post("/api/v1/consent", headers=headers, json={"version": "2020-01-01.1", "passive_learning": True})
    assert stale.status_code == 400 and stale.json()["error"]["code"] == "bad_request"
    malformed = client.post("/api/v1/consent", headers=headers, json={"version": settings.consent_version, "passive_learning": "yes"})
    assert malformed.status_code == 400
    accepted = client.post("/api/v1/consent", headers=headers, json={"version": settings.consent_version, "passive_learning": True})
    assert accepted.status_code == 200
    assert accepted.json() == {"consent": {"required_version": settings.consent_version,
                                           "accepted_version": settings.consent_version, "passive_learning": True}}
    with app.state.db.read() as conn:
        row = conn.execute("SELECT kind, version, passive_learning, revoked_at FROM consents").fetchone()
    assert tuple(row) == ("extension_disclosure", settings.consent_version, 1, None)


def test_withdrawing_consent_revokes_it_and_signs_out_the_device(app, client, settings, fake_state):
    create_user(client, "ana@example.com")
    headers = api_headers(connect_device(client)["token"])
    other = api_headers(connect_device(client)["token"])
    client.post("/api/v1/consent", headers=headers, json={"version": settings.consent_version, "passive_learning": True})
    assert client.post("/api/v1/consent/withdraw", headers=headers).status_code == 200
    with app.state.db.read() as conn:
        assert conn.execute("SELECT revoked_at FROM consents").fetchone()[0] is not None
    assert client.get("/api/v1/session", headers=headers).status_code == 401
    assert client.get("/api/v1/session", headers=other).status_code == 200


def test_session_api_and_account_page_show_the_same_real_state(client, settings):
    """Integration with the usage workstream's real `account_state` (not faked)."""
    create_user(client, "ana@example.com")
    device = connect_device(client)
    response = client.get("/api/v1/session", headers=api_headers(device["token"]))
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["account"]["email_masked"] == "a***@example.com"
    assert state["plan"]["code"] == "free"
    assert state["extension"] == {"min_version": settings.extension_min_version}
    page_state = context_value(client.get("/account"), "state")
    assert page_state["usage"] == state["usage"] and page_state["plan"] == state["plan"]
