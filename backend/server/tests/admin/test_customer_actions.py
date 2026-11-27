"""Grants, suspension, disconnecting browsers and the customer page's privacy rules.

The customer here is a real signed-up account with a connected browser and real entitlements, so the
numbers on the admin page are the ones `usage.service.account_state` gives the customer (CONTRACTS 6.2)
and a suspension is felt on the extension API exactly as CONTRACTS 6.3 describes.
"""
from __future__ import annotations

import pytest

from starling_server.devices import tokens as device_tokens
from starling_server.usage import service as usage_service

from ..core.helpers import api_headers, connect_device, context_value, create_user, login, post_form
from .conftest import PLANTED_DRAFT, add_reservation, add_subscription, audit_rows


@pytest.fixture
def member(app, client, second_browser, owner, fake_clock):
    """A customer with a website session, one connected browser and current consent, so metered
    reservations succeed and a refusal can only come from the admin action under test."""
    create_user(second_browser, "member@example.com")
    device = connect_device(second_browser)
    with app.state.db.read() as conn:
        account_id = conn.execute("SELECT id FROM accounts WHERE email_norm = 'member@example.com'").fetchone()["id"]
    app.state.db.write(lambda conn: conn.execute(
        "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at) "
        "VALUES (?, 'extension_disclosure', ?, 0, ?)",
        (account_id, app.state.settings.consent_version, fake_clock.value)))
    return {"id": account_id, "token": device["token"], "device_id": device["device_id"]}


def state(app, account_id, fake_clock):
    return usage_service.account_state(app.state.db, app.state.settings, account_id, fake_clock.value)


def flash_text(response) -> str:
    return response.text


# --------------------------------------------------------------------------- grants


def test_a_grant_raises_remaining_by_exactly_the_amount(app, client, owner, customer, fake_clock):
    before = state(app, customer, fake_clock)["usage"]
    granted = post_form(client, f"/admin/customers/{customer}/grant", {"credits": "40", "reason": "Outage credit"},
                        follow=True)
    assert granted.status_code == 200
    after = state(app, customer, fake_clock)["usage"]
    assert after["remaining"] == before["remaining"] + 40
    assert after["used"] == before["used"] and after["allowance"] == before["allowance"]

    with app.state.db.read() as conn:
        rows = conn.execute("SELECT kind, amount, actor, reason FROM credit_ledger WHERE account_id = ?",
                            (customer,)).fetchall()
    assert [tuple(row) for row in rows] == [("admin_grant", 40, owner.email, "Outage credit")]
    assert audit_rows(app.state.db, "grant_credits")[0]["target_account_id"] == customer


@pytest.mark.parametrize("credits_value,reason", [("0", "Nope"), ("10001", "Too big"), ("", "Empty"),
                                                  ("12.5", "Not whole"), ("-5", "Negative"), ("5", "")])
def test_grants_outside_one_to_ten_thousand_or_without_a_reason_change_nothing(app, client, owner, customer,
                                                                              fake_clock, credits_value, reason):
    before = state(app, customer, fake_clock)["usage"]["remaining"]
    refused = post_form(client, f"/admin/customers/{customer}/grant", {"credits": credits_value, "reason": reason},
                        follow=True)
    assert refused.status_code == 200 and "No credits were added" in flash_text(refused)
    assert state(app, customer, fake_clock)["usage"]["remaining"] == before
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0] == 0
    assert len(audit_rows(app.state.db, "grant_credits")) == 1  # refusals are still audited, exactly once

    # The negative control: the same form inside the range works.
    assert post_form(client, f"/admin/customers/{customer}/grant", {"credits": "10000", "reason": "Ceiling"}).status_code == 303
    assert state(app, customer, fake_clock)["usage"]["remaining"] == before + 10_000


# --------------------------------------------------------------------------- suspension


def test_suspend_blocks_the_device_api_and_unsuspend_restores_it(app, client, second_browser, member, fake_clock):
    headers = api_headers(member["token"])
    assert second_browser.get("/api/v1/session", headers=headers).status_code == 200

    suspended = post_form(client, f"/admin/customers/{member['id']}/suspend", {"suspend_reason": "Abuse report"},
                          follow=True)
    assert suspended.status_code == 200
    with app.state.db.read() as conn:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (member["id"],)).fetchone()["status"] == "suspended"

    # The bumped auth_epoch signs the connected browser out (CONTRACTS 6.3: 401 signed_out) ...
    refused = second_browser.get("/api/v1/session", headers=headers)
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "signed_out"
    assert second_browser.get("/account", follow_redirects=False).status_code == 303  # and the website session too
    # ... and the account itself is refused while suspended (403 account_suspended).
    with pytest.raises(usage_service.UsageError) as caught:
        usage_service.reserve(app.state.db, app.state.settings, member["id"], "key-while-suspended", "h" * 64,
                              "/api/v1/assist", "chat", fake_clock.value)
    assert (caught.value.http_status, caught.value.code) == (403, "account_suspended")

    lifted = post_form(client, f"/admin/customers/{member['id']}/unsuspend", {"suspend_reason": "Resolved"}, follow=True)
    assert lifted.status_code == 200
    with app.state.db.read() as conn:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (member["id"],)).fetchone()["status"] == "active"
    assert isinstance(usage_service.reserve(app.state.db, app.state.settings, member["id"], "key-after-lift", "h" * 64,
                                            "/api/v1/assist", "chat", fake_clock.value), usage_service.Reservation)
    # The customer signs in again and reconnects the extension; the old token stays dead.
    assert second_browser.get("/api/v1/session", headers=headers).status_code == 401
    assert login(second_browser, "member@example.com").status_code == 303
    assert second_browser.get("/api/v1/session",
                              headers=api_headers(connect_device(second_browser)["token"])).status_code == 200


def test_suspension_needs_a_reason_and_only_applies_to_an_active_account(app, client, owner, customer, fake_clock):
    assert post_form(client, f"/admin/customers/{customer}/suspend", {"suspend_reason": ""}, follow=True).status_code == 200
    with app.state.db.read() as conn:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (customer,)).fetchone()["status"] == "active"

    assert post_form(client, f"/admin/customers/{customer}/suspend", {"suspend_reason": "x" * 201},
                     follow=True).status_code == 200
    with app.state.db.read() as conn:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (customer,)).fetchone()["status"] == "active"

    assert post_form(client, f"/admin/customers/{customer}/suspend", {"suspend_reason": "Abuse"}).status_code == 303
    twice = post_form(client, f"/admin/customers/{customer}/suspend", {"suspend_reason": "Again"}, follow=True)
    assert "Only an active account can be suspended" in flash_text(twice)
    assert len(audit_rows(app.state.db, "suspend_account")) == 4


def test_unsuspend_never_clears_a_billing_dispute(app, client, owner, customer, fake_clock):
    add_subscription(app.state.db, customer, subscription_id="sub_disputed", plan_code="starter",
                     now=fake_clock.value, disputed_at=fake_clock.value - 3600)
    assert post_form(client, f"/admin/customers/{customer}/suspend", {"suspend_reason": "Dispute"}).status_code == 303
    lifted = post_form(client, f"/admin/customers/{customer}/unsuspend", {"suspend_reason": "Reviewed"}, follow=True)
    assert "payment dispute" in flash_text(lifted)
    with app.state.db.read() as conn:
        row = conn.execute("SELECT disputed_at FROM subscriptions WHERE subscription_id = 'sub_disputed'").fetchone()
        status = conn.execute("SELECT status FROM accounts WHERE id = ?", (customer,)).fetchone()["status"]
    assert row["disputed_at"] == fake_clock.value - 3600 and status == "active"
    assert audit_rows(app.state.db, "unsuspend_account")[0]["params_json"].count('"dispute_open": true') == 1


def test_an_owner_cannot_suspend_or_disconnect_their_own_account(app, client, owner):
    refused = post_form(client, f"/admin/customers/{owner.account_id}/suspend", {"suspend_reason": "Oops"}, follow=True)
    assert "suspend your own account" in flash_text(refused)
    refused_devices = post_form(client, f"/admin/customers/{owner.account_id}/revoke-devices", {}, follow=True)
    assert "disconnect your own browsers" in flash_text(refused_devices)
    with app.state.db.read() as conn:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (owner.account_id,)).fetchone()["status"] == "active"
    assert client.get("/admin", follow_redirects=False).status_code == 200  # still signed in and elevated
    assert [row["params_json"].count("refused_self") for row in audit_rows(app.state.db, "suspend_account")] == [1]

    # The negative control: the same actions on somebody else's account go through.
    page = client.get(f"/admin/customers/{owner.account_id}")
    assert context_value(page, "customer")["is_self"] is True


# --------------------------------------------------------------------------- devices and billing


def test_revoke_devices_invalidates_every_device_token(app, client, second_browser, member, fake_clock):
    second_device = connect_device(second_browser)
    assert second_browser.get("/api/v1/session", headers=api_headers(member["token"])).status_code == 200

    with app.state.db.read() as conn:
        epoch_before = conn.execute("SELECT auth_epoch FROM accounts WHERE id = ?", (member["id"],)).fetchone()[0]

    revoked = post_form(client, f"/admin/customers/{member['id']}/revoke-devices", {}, follow=True)
    assert "Disconnected 2 browsers" in flash_text(revoked)
    for token in (member["token"], second_device["token"]):
        answer = second_browser.get("/api/v1/session", headers=api_headers(token))
        assert answer.status_code == 401 and answer.json()["error"]["code"] == "signed_out"
    with app.state.db.read() as conn:
        reasons = [row["revoked_reason"] for row in conn.execute("SELECT revoked_reason FROM devices WHERE account_id = ?",
                                                                 (member["id"],))]
        account = conn.execute("SELECT auth_epoch, status FROM accounts WHERE id = ?", (member["id"],)).fetchone()
    assert reasons == ["admin_revoked", "admin_revoked"]
    assert account["status"] == "active" and account["auth_epoch"] == epoch_before  # not a sign-out
    assert second_browser.get("/account", follow_redirects=False).status_code == 200

    again = post_form(client, f"/admin/customers/{member['id']}/revoke-devices", {}, follow=True)
    assert "No browsers were connected" in flash_text(again)


def test_resync_billing_without_stripe_calls_nothing(app, client, owner, customer, monkeypatch):
    """No Stripe client on app.state: the reconciler is never called, whatever the form says."""
    from starling_server.billing import reconcile

    monkeypatch.setattr(reconcile, "resync_account", lambda *a, **k: pytest.fail("Stripe was called"))
    assert app.state.settings.stripe_secret_key == ""
    assert getattr(app.state, "stripe_client", None) is None
    for data in ({}, {"resync_confirm": "yes"}):
        answer = post_form(client, f"/admin/customers/{customer}/resync-billing", data, follow=True)
        assert "Stripe is not configured on this server" in flash_text(answer)
    assert all('"not_configured"' in row["params_json"] for row in audit_rows(app.state.db, "resync_billing"))


def test_resync_billing_asks_for_confirmation_then_reports_what_changed(app, client, owner, customer, monkeypatch):
    """With Stripe configured, an unticked box calls nothing and a ticked one reports the counters."""
    from starling_server.admin import actions
    from starling_server.billing import reconcile

    calls: list[str] = []

    def fake_resync(db, settings, stripe_client, account_id, now=None):
        calls.append(account_id)
        return {"ok": True, "subscriptions": 2, "mirror_changed": 1, "missing": 0, "periods_granted": 1,
                "upgrades_applied": 0, "periods_created": 1, "canceled_duplicates": 1, "entitlement_changed": True}

    monkeypatch.setattr(reconcile, "resync_account", fake_resync)
    monkeypatch.setattr(actions.reconcile, "resync_account", fake_resync)
    app.state.stripe_client = object()  # billing.lifecycle would set a real StripeClient here
    try:
        unconfirmed = post_form(client, f"/admin/customers/{customer}/resync-billing", {}, follow=True)
        assert "Tick the box" in flash_text(unconfirmed) and calls == []

        done = post_form(client, f"/admin/customers/{customer}/resync-billing", {"resync_confirm": "yes"}, follow=True)
        assert calls == [customer]
        assert "2 subscriptions checked" in flash_text(done) and "1 duplicate subscription canceled" in flash_text(done)
        assert "The plan or allowance changed" in flash_text(done)
    finally:
        app.state.stripe_client = None
    outcomes = [row["params_json"] for row in audit_rows(app.state.db, "resync_billing")]
    assert len(outcomes) == 2 and '"unconfirmed"' in outcomes[0] and '"done"' in outcomes[1]
    assert '"canceled_duplicates": 1' in outcomes[1]


# --------------------------------------------------------------------------- privacy (CONTRACTS 13.9)


def test_the_customer_page_never_shows_a_stored_draft(app, client, owner, customer, fake_clock):
    db = app.state.db
    add_reservation(db, customer, created_at=fake_clock.value - 600, state="committed",
                    result_json='{"ok": true, "decision": {"draft": "' + PLANTED_DRAFT + '"}}')
    add_reservation(db, customer, created_at=fake_clock.value - 300, state="released",
                    result_json='{"answer": "' + PLANTED_DRAFT + '"}')
    assert post_form(client, f"/admin/customers/{customer}/grant", {"credits": "3", "reason": "Goodwill"}).status_code == 303

    for path in (f"/admin/customers/{customer}", "/admin/customers", "/admin", "/admin/failures", "/admin/audit"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert PLANTED_DRAFT not in page.text, path
        assert "result_json" not in page.text, path

    detail = client.get(f"/admin/customers/{customer}")
    shown = context_value(detail, "customer")
    assert set(shown) == {"id", "email", "tenant_seq", "created_at", "email_verified", "account_status", "is_admin",
                          "is_self", "dispute_open", "stripe_customer_id", "devices_active", "subscription"}
    assert context_value(detail, "cost").keys() == {"period_cost_micros", "ceiling_micros", "ai_calls",
                                                    "released_with_spend"}
    for entry in context_value(detail, "recent_errors"):
        assert set(entry) == {"at", "code", "route", "count"}


def test_the_customer_page_shows_the_numbers_the_customer_sees(app, client, owner, customer, fake_clock):
    page = client.get(f"/admin/customers/{customer}")
    assert context_value(page, "state") == usage_service.account_state(app.state.db, app.state.settings, customer,
                                                                      fake_clock.value)


def test_customer_search_matches_email_substrings_and_pages(app, client, owner, fake_clock):
    from .conftest import make_customer

    ids = [make_customer(app.state.db, email=f"person{n:03d}@search.example", now=fake_clock.value - n)
           for n in range(55)]
    assert len(ids) == 55
    first = client.get("/admin/customers", params={"q": "search.example"})
    assert len(context_value(first, "customers")) == 50
    assert context_value(first, "page") == {"number": 1, "has_prev": False, "has_next": True}
    second = client.get("/admin/customers", params={"q": "search.example", "page": "2"})
    assert len(context_value(second, "customers")) == 5
    assert context_value(second, "page") == {"number": 2, "has_prev": True, "has_next": False}
    assert context_value(client.get("/admin/customers", params={"q": "person001"}), "customers")[0]["email"] == \
        "person001@search.example"
    # A LIKE wildcard in the query is a literal, not a match-everything.
    assert context_value(client.get("/admin/customers", params={"q": "%"}), "customers") == []
    assert context_value(client.get("/admin/customers", params={"q": "nobody"}), "customers") == []


def test_devices_active_counts_only_live_tokens(app, client, second_browser, member, fake_clock):
    page = client.get(f"/admin/customers/{member['id']}")
    assert context_value(page, "customer")["devices_active"] == 1
    app.state.db.write(lambda conn: device_tokens.revoke_all(conn, member["id"], fake_clock.value, "test"))
    page = client.get(f"/admin/customers/{member['id']}")
    assert context_value(page, "customer")["devices_active"] == 0
