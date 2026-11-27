"""Who may open the owner dashboard, and what every POST costs in the audit log (CONTRACTS 13.2).

Each protection is checked with its negative control: the same request by an owner who has cleared
that gate succeeds.
"""
from __future__ import annotations

import pytest

from starling_server.auth import admin, sessions, totp

from ..core.helpers import context_value, create_user, post_form
from .conftest import OWNER_EMAIL, audit_rows, enroll_and_elevate

PAGES = ("/admin", "/admin/customers", "/admin/failures", "/admin/webhooks", "/admin/audit", "/admin/ai")


@pytest.fixture(autouse=True)
def fake_services(monkeypatch):
    """No admin page or action may reach the services process over the network."""
    from starling_server.services import client as services_client

    async def status(settings, **kwargs):
        return {"ok": True, "keys": {"primary": {"configured": True, "last4": "Q7wX"}, "active": "primary"},
                "models": {"quality": "test-model", "fast": "test-model", "vision": "test-model"},
                "upstream": {"base_url": "https://api.test.invalid/v1"}}

    async def check(settings, **kwargs):
        return {"ok": True, "results": [{"model": "test-model", "ok": True, "latency_ms": 12}]}

    monkeypatch.setattr(services_client, "services_status", status)
    monkeypatch.setattr(services_client, "ai_check", check)
    return {"status": status, "check": check}


def test_a_signed_out_visitor_and_an_ordinary_customer_never_see_the_dashboard(client, second_browser):
    for path in PAGES:
        signed_out = client.get(path, follow_redirects=False)
        assert signed_out.status_code == 303, path
        assert signed_out.headers["location"].startswith("/login?next="), path

    create_user(second_browser, "member@example.com")
    for path in PAGES:
        assert second_browser.get(path, follow_redirects=False).status_code == 404, path
    # ... and cannot act either: the POST is refused before CSRF is even considered.
    assert second_browser.post("/admin/ai/pause", data={"pause_reason": "x"}, follow_redirects=False).status_code == 404


def test_an_admin_without_an_authenticator_is_sent_to_totp_and_then_gets_in(app, client, settings, fake_clock, plain_admin):
    for path in PAGES:
        needs_totp = client.get(path, follow_redirects=False)
        assert needs_totp.status_code == 303 and needs_totp.headers["location"] == "/admin/totp", path

    enroll_and_elevate(client, fake_clock)  # the negative control
    for path in PAGES:
        assert client.get(path, follow_redirects=False).status_code == 200, path


def test_an_admin_who_is_not_elevated_is_sent_to_elevate_and_then_gets_in(app, client, settings, fake_clock):
    create_user(client, OWNER_EMAIL)
    admin.grant_admin(app.state.db, settings, OWNER_EMAIL)
    secret = context_value(client.get("/admin/totp"), "secret")
    key = totp.decode_secret(secret)
    assert post_form(client, "/admin/totp", {"password": "correct horse battery",
                                             "code": totp.totp(key, fake_clock.value)}).status_code == 303

    for path in PAGES:
        needs_elevation = client.get(path, follow_redirects=False)
        assert needs_elevation.status_code == 303, path
        assert needs_elevation.headers["location"].startswith("/admin/elevate?next="), path

    fake_clock.advance(totp.STEP_S)
    assert post_form(client, "/admin/elevate", {"code": totp.totp(key, fake_clock.value), "next": "/admin"}).status_code == 303
    assert client.get("/admin", follow_redirects=False).status_code == 200

    # Elevation expires; GET pages are protected too, not only the POSTs.
    fake_clock.advance(sessions.ADMIN_ELEVATION_S + 1)
    for path in PAGES:
        assert client.get(path, follow_redirects=False).status_code == 303, path


def test_every_post_needs_the_csrf_token(app, client, owner, customer):
    forms = [
        (f"/admin/customers/{customer}/grant", {"credits": "5", "reason": "Goodwill"}),
        (f"/admin/customers/{customer}/suspend", {"suspend_reason": "Abuse report"}),
        (f"/admin/customers/{customer}/revoke-devices", {}),
        (f"/admin/customers/{customer}/resync-billing", {"resync_confirm": "yes"}),
        ("/admin/ai/pause", {"pause_reason": "Upstream outage"}),
        ("/admin/ai/resume", {}),
        ("/admin/ai/test", {}),
    ]
    for path, data in forms:
        missing = client.post(path, data=data, headers={"Origin": "http://127.0.0.1:8765"}, follow_redirects=False)
        assert missing.status_code == 403, path
        wrong = post_form(client, path, data, token="not-the-token")
        assert wrong.status_code == 403, path
        cross_site = post_form(client, path, data, origin="https://evil.example")
        assert cross_site.status_code == 403, path
    assert audit_rows(app.state.db, "grant_credits") == []  # a refused POST changes nothing

    # The negative control: the same form with the page's token is accepted.
    accepted = post_form(client, f"/admin/customers/{customer}/grant", {"credits": "5", "reason": "Goodwill"})
    assert accepted.status_code == 303
    assert len(audit_rows(app.state.db, "grant_credits")) == 1


def test_each_action_writes_exactly_one_audit_row(app, client, owner, customer):
    db = app.state.db
    actions = [
        (f"/admin/customers/{customer}/grant", {"credits": "5", "reason": "Goodwill"}, "grant_credits"),
        (f"/admin/customers/{customer}/suspend", {"suspend_reason": "Abuse report"}, "suspend_account"),
        (f"/admin/customers/{customer}/unsuspend", {"suspend_reason": "Resolved"}, "unsuspend_account"),
        (f"/admin/customers/{customer}/revoke-devices", {}, "revoke_devices"),
        (f"/admin/customers/{customer}/resync-billing", {"resync_confirm": "yes"}, "resync_billing"),
        ("/admin/ai/pause", {"pause_reason": "Upstream outage"}, "ai_pause"),
        ("/admin/ai/resume", {"resume_reason": "Recovered"}, "ai_resume"),
        ("/admin/ai/test", {}, "ai_test"),
    ]
    for path, data, action in actions:
        response = post_form(client, path, data)
        assert response.status_code == 303, (path, response.text)
        rows = audit_rows(db, action)
        assert len(rows) == 1, action
        assert rows[0]["admin_id"] == owner.account_id
        assert rows[0]["ip_prefix"]
        params = rows[0]["params_json"] or ""
        assert "outcome" in params
        for secret in ("csrf", "password", "token", "sk-"):
            assert secret not in params.lower(), (action, params)

    # A refused action is still recorded once, with its outcome.
    assert post_form(client, f"/admin/customers/{customer}/grant", {"credits": "0", "reason": ""}).status_code == 303
    grants = audit_rows(db, "grant_credits")
    assert len(grants) == 2 and '"outcome": "invalid"' in grants[1]["params_json"]
    assert [row["action"] for row in audit_rows(db)][:1] == ["admin_grant"]  # the CLI grant of owner access


def test_an_unknown_customer_is_a_404_not_an_action(app, client, owner):
    assert client.get("/admin/customers/00000000-0000-4000-8000-000000000000", follow_redirects=False).status_code == 404
    refused = post_form(client, "/admin/customers/00000000-0000-4000-8000-000000000000/grant",
                        {"credits": "5", "reason": "Goodwill"})
    assert refused.status_code == 404
    assert audit_rows(app.state.db, "grant_credits") == []
