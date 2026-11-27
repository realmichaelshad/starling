"""Account pages: overview, billing view, sessions, resend, export and deletion."""
from __future__ import annotations

import html
import json
import re
import sys
import types

import anyio
import pytest

from starling_server.account import deletion

from ..conftest import PUBLIC_URL
from .conftest import FakePool
from .helpers import (
    PASSWORD,
    account_row,
    api_headers,
    connect_device,
    context_value,
    create_user,
    link_token,
    login,
    outbox,
    post_form,
    signup,
)


@pytest.fixture
def fake_billing(monkeypatch):
    """Stands in for billing.service.cancel_for_deletion (billing workstream interface)."""
    cancelled: list[str] = []
    module = types.ModuleType("starling_server.billing.service")

    def cancel_for_deletion(db, settings, account_id):
        cancelled.append(account_id)

    module.cancel_for_deletion = cancel_for_deletion
    monkeypatch.setitem(sys.modules, "starling_server.billing.service", module)
    return cancelled


def test_account_pages_render_for_the_signed_in_user(app, client, fake_state):
    create_user(client, "ana@example.com")
    overview = client.get("/account")
    assert overview.status_code == 200
    assert context_value(overview, "state")["account"]["email_verified"] is True
    billing = client.get("/account/billing")
    assert context_value(billing, "invoices_url_available") is False
    assert [plan["code"] for plan in context_value(billing, "plans")] == ["free", "starter", "pro"]
    app.state.db.write(lambda c: c.execute(
        "INSERT INTO stripe_customers (account_id, customer_id, livemode, created_at) SELECT id, 'cus_example', 0, 0 FROM accounts"))
    assert context_value(client.get("/account/billing"), "invoices_url_available") is True
    assert client.get("/account/data").status_code == 200
    assert post_form(client, "/logout", {}).status_code == 303
    signed_out = client.get("/account/billing", follow_redirects=False)
    assert signed_out.status_code == 303 and signed_out.headers["location"] == "/login?next=%2Faccount%2Fbilling"


def test_security_page_lists_sessions_and_signs_out_the_others(client, second_client, fake_state):
    create_user(client, "ana@example.com")
    assert login(second_client, "ana@example.com").status_code == 303
    listed = context_value(client.get("/account/security"), "sessions")
    assert len(listed) == 2 and [s["current"] for s in listed].count(True) == 1
    assert all(len(s["id"]) == 12 and s["user_agent_summary"] == "Unknown browser" for s in listed)
    assert post_form(client, "/account/security/sessions", {}).status_code == 303
    assert second_client.get("/account", follow_redirects=False).status_code == 303
    assert client.get("/account", follow_redirects=False).status_code == 200
    assert len(context_value(client.get("/account/security"), "sessions")) == 1


def resend_says(client) -> str:
    """Press "Send the email again" on the account page; return where it went and what the page said."""
    response = post_form(client, "/account/verify/resend", {})
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    said = re.findall(r'<li class="\w+">([^<]*)</li>', html.unescape(page.text))
    return response.headers["location"] + " " + " ".join(said)


def test_verification_email_can_be_resent_with_spacing(app, client, fake_clock, fake_state):
    create_user(client, "ana@example.com", verified=False)
    assert resend_says(client).startswith("/verify-email/sent We sent a new email to ana@example.com")
    assert "You can send another in a minute" in resend_says(client)  # too soon
    assert [m["template"] for m in outbox(client, "ana@example.com")].count("verify_email") == 2
    fake_clock.advance(61)
    assert "We sent a new email" in resend_says(client)
    token = link_token(client, "ana@example.com", "/verify-email")
    assert post_form(client, "/verify-email", {"token": token}).status_code == 303
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None
    assert resend_says(client).startswith("/account")


def test_export_downloads_engine_data_or_explains_it_is_unavailable(app, client, fake_state, no_pool):
    create_user(client, "ana@example.com")
    unavailable = post_form(client, "/account/export", {})
    assert unavailable.status_code == 503 and "export" in context_value(unavailable, "errors")
    pool = FakePool()
    app.state.engine_pool = pool
    download = post_form(client, "/account/export", {})
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith('attachment; filename="starling-export-')
    row = account_row(app, "ana@example.com")
    payload = json.loads(download.content)
    # Both halves: the engine's drafting data and the gateway's own records.
    assert payload["drafting"] == json.loads(pool.export_body)
    assert payload["account"]["email"] == "ana@example.com" and payload["account"]["id"] == row["id"]
    assert "password_hash" not in payload["account"] and "totp_secret_enc" not in payload["account"]
    assert set(payload) >= {"exported_at", "account", "devices", "sessions", "consents", "billing", "usage"}
    assert payload["usage"] == {"counters": [], "credit_ledger": [], "requests": []}
    assert pool.requests == [(row["id"], row["tenant_seq"], "GET", "/profile/export")]
    assert client.post("/account/export", data={}, headers={"Origin": PUBLIC_URL}).status_code == 403


def test_account_deletion_removes_access_and_personal_data(app, client, fake_state, fake_pool, fake_billing):
    create_user(client, "ana@example.com")
    device = connect_device(client)
    row = account_row(app, "ana@example.com")
    # The word, in any case, and nothing else.
    assert post_form(client, "/account/delete", {"delete_password": PASSWORD, "delete_confirmation": "delete it"}).status_code == 400
    assert post_form(client, "/account/delete", {"delete_password": "wrong horse battery", "delete_confirmation": "DELETE"}).status_code == 400
    assert account_row(app, "ana@example.com")["status"] == "active" and fake_pool.deleted == [] and fake_billing == []

    done = post_form(client, "/account/delete", {"delete_password": PASSWORD, "delete_confirmation": "DELETE"})
    assert done.status_code == 303 and done.headers["location"] == "/goodbye"
    assert fake_billing == [row["id"]]
    assert fake_pool.deleted == [(row["id"], row["tenant_seq"])]
    with app.state.db.read() as conn:
        deleted = conn.execute("SELECT * FROM accounts WHERE id = ?", (row["id"],)).fetchone()
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (row["id"],)).fetchone()[0]
            for table in ("web_sessions", "devices", "email_tokens", "connect_requests")
        }
        # Only the goodbye is still addressed to them, and it goes as soon as it is sent (test_goodbye.py).
        mail_left = conn.execute(
            "SELECT COUNT(*) FROM email_outbox WHERE to_email = 'ana@example.com' AND template != 'account_deleted'"
        ).fetchone()[0]
        tombstone = conn.execute("SELECT account_hash FROM tombstones").fetchall()
    assert deleted["status"] == "deleted" and deleted["email"] == f"deleted-{row['id']}@invalid"
    assert deleted["password_hash"] == "!" and deleted["deleted_at"] is not None and deleted["tenant_seq"] == row["tenant_seq"]
    assert counts == {"web_sessions": 0, "devices": 0, "email_tokens": 0, "connect_requests": 0} and mail_left == 0
    assert len(tombstone) == 1 and len(tombstone[0][0]) == 64 and "ana" not in tombstone[0][0]

    assert client.get("/account", follow_redirects=False).status_code == 303
    assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 401
    assert login(client, "ana@example.com").status_code == 400
    assert signup(client, "ana@example.com").status_code == 303  # the address is free for a new account
    assert account_row(app, "ana@example.com")["id"] != row["id"]


def test_deletion_that_cannot_finish_stays_pending_and_resumes(app, client, settings, fake_state, fake_pool, fake_billing):
    create_user(client, "ana@example.com")
    row = account_row(app, "ana@example.com")
    fake_pool.fail_delete = True
    assert post_form(client, "/account/delete", {"delete_password": PASSWORD, "delete_confirmation": "DELETE"}).status_code == 303
    with app.state.db.read() as conn:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (row["id"],)).fetchone()[0] == "deleting"
    assert client.get("/account", follow_redirects=False).status_code == 303  # already signed out
    fake_pool.fail_delete = False
    assert anyio.run(deletion.resume_pending_deletions, app.state.db, settings, fake_pool) == [row["id"]]
    with app.state.db.read() as conn:
        assert conn.execute("SELECT status FROM accounts WHERE id = ?", (row["id"],)).fetchone()[0] == "deleted"


def test_deletion_is_unavailable_without_an_engine_pool(app, client, fake_state, fake_billing, no_pool):
    create_user(client, "ana@example.com")
    response = post_form(client, "/account/delete", {"delete_password": PASSWORD, "delete_confirmation": "DELETE"})
    assert response.status_code == 503 and "delete" in context_value(response, "errors")
    assert account_row(app, "ana@example.com")["status"] == "active" and fake_billing == []
