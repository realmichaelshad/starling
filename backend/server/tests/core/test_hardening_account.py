"""Adversarial cover for the account page actions: the data export and account deletion."""
from __future__ import annotations

import json
import sys
import types

import anyio
import pytest

from starling_server.account import deletion
from starling_server.auth import accounts
from starling_server.security import ratelimit

from .helpers import (
    NEW_PASSWORD,
    PASSWORD,
    account_row,
    api_headers,
    connect_device,
    context_value,
    create_user,
    link_token,
    login,
    post_form,
    signup,
)

DELETE_FORM = {"delete_password": PASSWORD, "delete_confirmation": "DELETE"}


@pytest.fixture
def fake_billing(monkeypatch):
    cancelled: list[str] = []
    module = types.ModuleType("starling_server.billing.service")
    module.cancel_for_deletion = lambda db, settings, account_id: cancelled.append(account_id)
    monkeypatch.setitem(sys.modules, "starling_server.billing.service", module)
    return cancelled


def stored_secrets(app, account_id: str) -> list[str]:
    """Every value the control database keeps that must never leave it."""
    with app.state.db.read() as conn:
        rows = [
            conn.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchall(),
            conn.execute("SELECT id_hash, csrf_secret FROM web_sessions WHERE account_id = ?", (account_id,)).fetchall(),
            conn.execute("SELECT token_hash FROM devices WHERE account_id = ?", (account_id,)).fetchall(),
            conn.execute("SELECT token_hash FROM email_tokens WHERE account_id = ?", (account_id,)).fetchall(),
            conn.execute("SELECT code_hash, state_hash FROM connect_requests WHERE account_id = ?", (account_id,)).fetchall(),
        ]
    secrets: list[str] = []
    for group in rows:
        for row in group:
            secrets.extend(str(value) for value in tuple(row) if value)
    return secrets


# --------------------------------------------------------------------------- export


def test_the_export_carries_no_secret_the_server_holds(app, client, fake_state, fake_pool):
    create_user(client, "ana@example.com")
    connect_device(client)
    row = account_row(app, "ana@example.com")
    secrets = stored_secrets(app, row["id"])
    assert secrets, "the test needs something to look for"

    response = post_form(client, "/account/export", {})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith('attachment; filename="starling-export-')
    text = response.text
    payload = json.loads(text)

    for secret in secrets:
        assert secret not in text, "the export leaked a stored secret"
    for forbidden in ("password_hash", "token_hash", "id_hash", "csrf_secret", "totp_secret_enc", "signup_ip_prefix"):
        assert forbidden not in text, forbidden
    assert payload["account"]["email"] == "ana@example.com"
    assert payload["devices"] and "label" in payload["devices"][0]
    assert payload["sessions"] and "ip_prefix" in payload["sessions"][0]
    assert payload["drafting"] == {"profile": {"samples": []}}


def test_the_export_is_rate_limited_per_account(app, client, second_client, fake_state, fake_pool):
    create_user(client, "ana@example.com")
    create_user(second_client, "bob@example.com")
    for _ in range(ratelimit.EXPORT_PER_ACCOUNT.limit):
        assert post_form(client, "/account/export", {}).status_code == 200
    blocked = post_form(client, "/account/export", {})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert "export" in context_value(blocked, "errors")
    assert post_form(second_client, "/account/export", {}).status_code == 200  # another account is unaffected


def test_a_broken_engine_export_never_produces_half_a_download(app, client, fake_state, fake_pool):
    create_user(client, "ana@example.com")
    fake_pool.export_body = b"<html>not json</html>"
    response = post_form(client, "/account/export", {})
    assert response.status_code == 503
    assert "export" in context_value(response, "errors")
    assert "not json" not in response.text


# --------------------------------------------------------------------------- deletion


def test_deletion_will_not_work_as_a_password_oracle(app, client, fake_state, fake_pool, fake_billing):
    """The delete form takes a password, so it must share the password-check throttle."""
    create_user(client, "ana@example.com")
    for _ in range(ratelimit.PASSWORD_CHECK_PER_ACCOUNT.limit):
        assert post_form(client, "/account/delete", {**DELETE_FORM, "delete_password": "wrong one here"}).status_code == 400
    blocked = post_form(client, "/account/delete", {**DELETE_FORM, "delete_password": "wrong one here"})
    assert blocked.status_code == 400
    assert "Try again in" in context_value(blocked, "errors")["delete_password"]
    # Even the right password now waits: the throttle is not a hint about which guess was right.
    assert post_form(client, "/account/delete", DELETE_FORM).status_code == 400
    assert account_row(app, "ana@example.com")["status"] == "active"
    assert fake_pool.deleted == [] and fake_billing == []


def test_everything_is_signed_out_before_the_slow_deletion_steps(app, client, second_client, fake_state, fake_pool, fake_billing):
    """Step one revokes access. If billing or the engine then fails, the customer is still out."""
    create_user(client, "ana@example.com")
    assert login(second_client, "ana@example.com").status_code == 303
    device = connect_device(client)
    fake_pool.fail_delete = True
    assert post_form(client, "/account/delete", DELETE_FORM).status_code == 303

    assert client.get("/account", follow_redirects=False).status_code == 303
    assert second_client.get("/account", follow_redirects=False).status_code == 303
    assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 401
    assert login(second_client, "ana@example.com").status_code == 400
    with app.state.db.read() as conn:
        row = conn.execute("SELECT status FROM accounts WHERE email_norm = 'ana@example.com'").fetchone()
        tokens = conn.execute("SELECT COUNT(*) FROM email_tokens WHERE used_at IS NULL").fetchone()[0]
    assert row["status"] == "deleting" and tokens == 0


def test_resuming_a_deletion_is_idempotent(app, client, settings, fake_state, fake_pool, fake_billing):
    create_user(client, "ana@example.com")
    account_id = account_row(app, "ana@example.com")["id"]
    fake_pool.fail_delete = True
    assert post_form(client, "/account/delete", DELETE_FORM).status_code == 303
    fake_pool.fail_delete = False

    assert anyio.run(deletion.resume_pending_deletions, app.state.db, settings, fake_pool) == [account_id]
    assert anyio.run(deletion.resume_pending_deletions, app.state.db, settings, fake_pool) == []
    # Running the whole deletion again over a finished account changes nothing and never raises.
    anyio.run(deletion.delete_account, app.state.db, settings, fake_pool, account_id)
    with app.state.db.read() as conn:
        row = conn.execute("SELECT status, email, deleted_at FROM accounts WHERE id = ?", (account_id,)).fetchone()
        tombstones = conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]
    assert row["status"] == "deleted" and row["email"] == f"deleted-{account_id}@invalid"
    assert tombstones == 1


def test_deleting_an_account_that_never_existed_is_a_no_op(app, settings, fake_pool, fake_billing):
    anyio.run(deletion.delete_account, app.state.db, settings, fake_pool, "00000000-0000-4000-8000-000000000000")
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    assert fake_pool.deleted == [] and fake_billing == []


def test_the_tombstone_covers_the_whole_mailbox_not_just_the_address(app, client, settings, fake_state, fake_pool, fake_billing):
    """Plus tags and Gmail dots reach one mailbox, so they share one deletion tombstone and one
    free allowance."""
    create_user(client, "A.na+work@Gmail.com")
    account_id = account_row(app, "a.na+work@gmail.com")["id"]
    anyio.run(deletion.delete_account, app.state.db, settings, fake_pool, account_id)
    with app.state.db.read() as conn:
        stored = conn.execute("SELECT account_hash FROM tombstones").fetchone()[0]
    assert stored == deletion.tombstone_hash(settings, accounts.allowance_key("ana@gmail.com"))
    assert stored == deletion.tombstone_hash(settings, accounts.allowance_key("a.n.a+other@googlemail.com"))
    assert stored != deletion.tombstone_hash(settings, accounts.allowance_key("anb@gmail.com"))


def test_a_deleted_address_can_sign_up_again_with_a_new_account(app, client, second_client, settings, fake_state, fake_pool, fake_billing):
    create_user(client, "ana@example.com")
    first_id = account_row(app, "ana@example.com")["id"]
    anyio.run(deletion.delete_account, app.state.db, settings, fake_pool, first_id)
    assert signup(second_client, "ana@example.com", NEW_PASSWORD).status_code == 303
    second = account_row(app, "ana@example.com")
    assert second["id"] != first_id and second["tenant_seq"] != account_row(app, f"deleted-{first_id}@invalid")["tenant_seq"]
    token = link_token(second_client, "ana@example.com", "/verify-email")
    assert post_form(second_client, "/verify-email", {"token": token}).status_code == 303
    assert login(second_client, "ana@example.com", NEW_PASSWORD).status_code == 303


def test_a_deleted_account_keeps_nothing_that_identifies_the_customer(app, client, settings, fake_state, fake_pool, fake_billing):
    create_user(client, "ana@example.com")
    connect_device(client)
    row = account_row(app, "ana@example.com")
    anyio.run(deletion.delete_account, app.state.db, settings, fake_pool, row["id"])
    with app.state.db.read() as conn:
        after = conn.execute("SELECT * FROM accounts WHERE id = ?", (row["id"],)).fetchone()
        leftovers = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (row["id"],)).fetchone()[0]
            for table in ("web_sessions", "devices", "email_tokens", "connect_requests")
        }
        # One message is still owed to the address: the goodbye that confirms the deletion. Its row
        # is removed the moment it is sent (sender.EPHEMERAL_TEMPLATES, test_goodbye.py); nothing
        # else addressed to the customer may be left.
        outbox_left = conn.execute(
            "SELECT COUNT(*) FROM email_outbox WHERE lower(to_email) = 'ana@example.com' AND template != 'account_deleted'"
        ).fetchone()[0]
    values = " ".join(str(value) for value in tuple(after))
    assert "ana@example.com" not in values and "ana" not in after["email_norm"].split("@")[0].replace(row["id"], "")
    assert after["password_hash"] == "!" and after["signup_ip_prefix"] is None
    assert leftovers == {"web_sessions": 0, "devices": 0, "email_tokens": 0, "connect_requests": 0}
    assert outbox_left == 0
