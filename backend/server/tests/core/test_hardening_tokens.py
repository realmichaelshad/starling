"""Adversarial cover for email verification and password reset tokens.

The token in an email is a bearer secret with a long life in someone's inbox, so every rule
about it (single use, expiry, purpose, what retires it, where its link points) is checked here.
"""
from __future__ import annotations

import sys
import types
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from fastapi.testclient import TestClient

from starling_server.account import deletion
from starling_server.auth import email_tokens
from starling_server.security import ratelimit
from starling_server.security.crypto import sha256_hex

from ..conftest import PUBLIC_URL
from .helpers import (
    NEW_PASSWORD,
    PASSWORD,
    account_row,
    context_value,
    create_user,
    link_token,
    login,
    outbox,
    page_token,
    post_form,
    signup,
)

POISON = {"Host": "evil.example", "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"}


@pytest.fixture
def fake_billing(monkeypatch):
    """Stands in for billing.service.cancel_for_deletion (billing workstream interface)."""
    cancelled: list[str] = []
    module = types.ModuleType("starling_server.billing.service")
    module.cancel_for_deletion = lambda db, settings, account_id: cancelled.append(account_id)
    monkeypatch.setitem(sys.modules, "starling_server.billing.service", module)
    return cancelled


def token_rows(app, account_id: str | None = None) -> list[dict]:
    sql = "SELECT id, account_id, purpose, token_hash, new_email, expires_at, used_at FROM email_tokens"
    params: tuple = ()
    if account_id:
        sql += " WHERE account_id = ?"
        params = (account_id,)
    with app.state.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def forgot(client, email: str, **kwargs):
    return post_form(client, "/forgot-password", {"email": email}, **kwargs)


# --------------------------------------------------------------------------- storage


def test_tokens_are_stored_only_as_sha256_digests(app, client):
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    rows = token_rows(app)
    assert len(rows) == 1
    assert rows[0]["token_hash"] == sha256_hex(token)
    assert len(rows[0]["token_hash"]) == 64
    # Nothing in the row can be turned back into the link.
    assert token not in repr(rows[0])


def test_a_token_that_differs_by_one_character_is_not_accepted(app, client):
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    assert post_form(client, "/verify-email", {"token": tampered}).status_code == 400
    assert account_row(app, "ana@example.com")["email_verified_at"] is None
    assert post_form(client, "/verify-email", {"token": token}).status_code == 303


@pytest.mark.parametrize("token", ["", "short", "x" * 200, "../../etc/passwd", "' OR 1=1 --", "%00", None])
def test_malformed_tokens_are_refused_without_a_server_error(client, token):
    data = {} if token is None else {"token": token}
    assert post_form(client, "/verify-email", data).status_code == 400
    assert post_form(client, "/reset-password", {**data, "password": NEW_PASSWORD}).status_code == 400


# --------------------------------------------------------------------------- verification


def test_a_verification_token_works_exactly_once(app, client):
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    assert post_form(client, "/verify-email", {"token": token}).status_code == 303
    verified_at = account_row(app, "ana@example.com")["email_verified_at"]
    assert verified_at is not None
    again = post_form(client, "/verify-email", {"token": token})
    assert again.status_code == 400 and context_value(again, "token_valid") is False
    assert account_row(app, "ana@example.com")["email_verified_at"] == verified_at


def test_opening_the_link_never_consumes_the_token(app, client):
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    page = client.get("/verify-email", params={"token": token})
    assert page.status_code == 200 and context_value(page, "token_valid") is True
    assert page.headers["referrer-policy"] == "no-referrer"
    assert account_row(app, "ana@example.com")["email_verified_at"] is None  # a GET changed nothing
    assert token_rows(app)[0]["used_at"] is None
    assert post_form(client, "/verify-email", {"token": token}).status_code == 303


def test_a_verification_token_expires(app, client, fake_clock):
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    fake_clock.advance(email_tokens.VERIFY_TTL_S)
    assert client.get("/verify-email", params={"token": token}).status_code == 400
    assert post_form(client, "/verify-email", {"token": token}).status_code == 400
    assert account_row(app, "ana@example.com")["email_verified_at"] is None


def test_a_verification_token_dies_when_the_address_it_was_issued_for_changes(app, client, second_client, fake_state):
    """The token proves control of one mailbox; it must not verify a different address."""
    create_user(client, "ana@example.com", verified=False)
    stale = link_token(client, "ana@example.com", "/verify-email")
    assert post_form(client, "/account/security/email", {"new_email": "new@example.com", "email_password": PASSWORD}).status_code == 303
    change = link_token(client, "new@example.com", "/verify-email")
    assert post_form(client, "/verify-email", {"token": change}).status_code == 303
    assert account_row(app, "new@example.com")["email_verified_at"] is not None
    # The link that was sent to the old mailbox no longer does anything.
    assert post_form(second_client, "/verify-email", {"token": stale}).status_code == 400


def test_a_verification_token_of_a_deleted_account_is_refused(app, client, settings, fake_state, fake_pool, fake_billing):
    create_user(client, "ana@example.com", verified=False)
    token = link_token(client, "ana@example.com", "/verify-email")
    row = account_row(app, "ana@example.com")
    anyio.run(deletion.delete_account, app.state.db, settings, fake_pool, row["id"])
    assert post_form(client, "/verify-email", {"token": token}).status_code == 400
    assert token_rows(app) == []


def _resend_sent(client) -> bool:
    """True when the resend went out, False when the page said to wait (both land on the code page)."""
    before = len(outbox(client, "ana@example.com"))
    response = post_form(client, "/account/verify/resend", {})
    assert response.headers["location"] == "/verify-email/sent"
    return len(outbox(client, "ana@example.com")) == before + 1


def test_resending_verification_is_spaced_and_capped(app, client, fake_clock, fake_state):
    create_user(client, "ana@example.com", verified=False)
    sent = 1  # the sign-up link
    assert _resend_sent(client)
    sent += 1
    # A second press within the spacing window sends nothing and says so.
    assert not _resend_sent(client)
    for _ in range(10):
        fake_clock.advance(ratelimit.VERIFY_RESEND_SPACING.window_s)
        if _resend_sent(client):
            sent += 1
    # The press that the spacing rule refused did not eat one of the day's links.
    assert sent == 1 + ratelimit.VERIFY_RESEND_PER_ACCOUNT.limit
    assert len(outbox(client, "ana@example.com")) == sent
    # The daily cap lifts with the window, it is not a permanent lockout.
    fake_clock.advance(ratelimit.VERIFY_RESEND_PER_ACCOUNT.window_s)
    assert _resend_sent(client)


def test_resending_for_a_verified_account_sends_nothing(app, client, fake_state):
    create_user(client, "ana@example.com")
    before = len(outbox(client, "ana@example.com"))
    response = post_form(client, "/account/verify/resend", {})
    assert response.headers["location"] == "/account"
    assert len(outbox(client, "ana@example.com")) == before


# --------------------------------------------------------------------------- password reset


def test_a_reset_token_works_exactly_once(app, client, second_client):
    create_user(client, "ana@example.com")
    assert forgot(second_client, "ana@example.com").status_code == 200
    token = link_token(client, "ana@example.com", "/reset-password")
    assert post_form(second_client, "/reset-password", {"token": token, "password": NEW_PASSWORD}).status_code == 303
    again = post_form(second_client, "/reset-password", {"token": token, "password": "third passphrase here"})
    assert again.status_code == 400 and context_value(again, "token_valid") is False
    assert login(second_client, "ana@example.com", NEW_PASSWORD).status_code == 303


def test_a_token_submitted_twice_at_once_still_only_works_once(app, client, fake_clock):
    """Two tabs pressing confirm together: the writer transaction is what makes it single use."""
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    browsers = [TestClient(app, base_url=PUBLIC_URL) for _ in range(2)]
    tokens = [page_token(browser) for browser in browsers]

    def submit(index: int):
        return post_form(browsers[index], "/verify-email", {"token": token}, token=tokens[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(future.result().status_code for future in [pool.submit(submit, 0), pool.submit(submit, 1)])
    assert codes == [303, 400]
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None
    assert len([row for row in token_rows(app) if row["used_at"] is not None]) == 1


def test_a_reset_token_expires_and_never_leaves_a_usable_row(app, client, second_client, fake_clock):
    create_user(client, "ana@example.com")
    assert forgot(second_client, "ana@example.com").status_code == 200
    token = link_token(client, "ana@example.com", "/reset-password")
    fake_clock.advance(email_tokens.RESET_TTL_S)
    assert client.get("/reset-password", params={"token": token}).status_code == 400
    assert post_form(second_client, "/reset-password", {"token": token, "password": NEW_PASSWORD}).status_code == 400
    assert login(second_client, "ana@example.com", NEW_PASSWORD).status_code == 400
    assert login(second_client, "ana@example.com", PASSWORD).status_code == 303


def test_asking_for_a_new_reset_link_retires_the_previous_one(app, client, second_client, fake_clock):
    """A reset link sits in an inbox for an hour. Asking for another one must retire the first,
    so a link that leaked (a forwarded mail, a shared machine) stops working as soon as the
    customer asks for a fresh one."""
    create_user(client, "ana@example.com")
    assert forgot(second_client, "ana@example.com").status_code == 200
    first = link_token(client, "ana@example.com", "/reset-password")
    fake_clock.advance(60)
    assert forgot(second_client, "ana@example.com").status_code == 200
    second = link_token(client, "ana@example.com", "/reset-password")
    assert first != second
    assert post_form(second_client, "/reset-password", {"token": first, "password": NEW_PASSWORD}).status_code == 400
    assert post_form(second_client, "/reset-password", {"token": second, "password": NEW_PASSWORD}).status_code == 303


def test_a_new_reset_link_does_not_retire_the_email_change_revert_link(app, client, second_client, fake_state):
    """The revert link is the customer's only way back from a hijacked email change; a reset
    request (which an attacker can trigger) must never cancel it."""
    create_user(client, "ana@example.com")
    assert post_form(client, "/account/security/email", {"new_email": "new@example.com", "email_password": PASSWORD}).status_code == 303
    revert = link_token(client, "ana@example.com", "/reset-password")
    assert forgot(second_client, "ana@example.com").status_code == 200
    assert client.get("/reset-password", params={"token": revert}).status_code == 200


def test_resetting_an_unverified_account_proves_the_address_and_verifies_it(app, client, second_client):
    assert signup(client, "ana@example.com").status_code == 303
    assert account_row(app, "ana@example.com")["email_verified_at"] is None
    assert forgot(second_client, "ana@example.com").status_code == 200
    token = link_token(client, "ana@example.com", "/reset-password")
    assert post_form(second_client, "/reset-password", {"token": token, "password": NEW_PASSWORD}).status_code == 303
    # The link only reached the mailbox owner, so the address is proven.
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None
    assert login(second_client, "ana@example.com", NEW_PASSWORD).status_code == 303


def test_reset_requests_are_throttled_per_address_without_revealing_it(app, client, second_client):
    create_user(client, "ana@example.com")
    known = [forgot(second_client, "ana@example.com") for _ in range(ratelimit.FORGOT_PER_EMAIL.limit + 2)]
    unknown = forgot(second_client, "nobody@example.com")
    assert {response.status_code for response in known} == {200}
    assert unknown.status_code == 200
    # The throttle is invisible to the sender: the pages differ only by the address typed.
    assert known[-1].text.replace("ana@example.com", "E") == unknown.text.replace("nobody@example.com", "E")
    links = [m for m in outbox(client, "ana@example.com") if m["template"] == "password_reset"]
    assert len(links) == ratelimit.FORGOT_PER_EMAIL.limit


def test_reset_requests_are_throttled_per_network(app, monkeypatch):
    monkeypatch.setattr(ratelimit, "FORGOT_PER_IP", ratelimit.Rule("forgot_ip", 2, 3600))
    attacker = TestClient(app, base_url=PUBLIC_URL, client=("203.0.113.10", 5000))
    elsewhere = TestClient(app, base_url=PUBLIC_URL, client=("198.51.100.5", 5000))
    assert forgot(attacker, "a@example.com").status_code == 200
    assert forgot(attacker, "b@example.com").status_code == 200
    blocked = forgot(attacker, "c@example.com")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert forgot(elsewhere, "d@example.com").status_code == 200


def test_a_reset_token_cannot_be_used_as_a_verification_token_or_the_other_way_round(app, client, second_client):
    assert signup(client, "ana@example.com").status_code == 303
    verify = link_token(client, "ana@example.com", "/verify-email")
    assert forgot(second_client, "ana@example.com").status_code == 200
    reset = link_token(client, "ana@example.com", "/reset-password")
    assert post_form(second_client, "/verify-email", {"token": reset}).status_code == 400
    assert post_form(second_client, "/reset-password", {"token": verify, "password": NEW_PASSWORD}).status_code == 400
    assert client.get("/reset-password", params={"token": verify}).status_code == 400
    assert client.get("/verify-email", params={"token": reset}).status_code == 400
    # Both still work where they belong.
    assert post_form(second_client, "/reset-password", {"token": reset, "password": NEW_PASSWORD}).status_code == 303


def test_token_forms_are_rate_limited_per_network(app, monkeypatch):
    monkeypatch.setattr(ratelimit, "TOKEN_FORM_PER_IP", ratelimit.Rule("email_token_ip", 3, 600))
    guesser = TestClient(app, base_url=PUBLIC_URL, client=("203.0.113.10", 5000))
    elsewhere = TestClient(app, base_url=PUBLIC_URL, client=("198.51.100.5", 5000))
    guess = "x" * 43
    for _ in range(3):
        assert post_form(guesser, "/verify-email", {"token": guess}).status_code == 400
    blocked = post_form(guesser, "/verify-email", {"token": guess})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert post_form(guesser, "/reset-password", {"token": guess, "password": NEW_PASSWORD}).status_code == 429
    assert post_form(elsewhere, "/verify-email", {"token": guess}).status_code == 400


# --------------------------------------------------------------------------- link origin


@pytest.mark.parametrize("path", ["/signup", "/forgot-password"])
def test_email_links_come_from_the_configured_origin_not_the_request_host(app, client, path):
    """Host header poisoning must not be able to plant an attacker's link in a customer's inbox."""
    if path == "/forgot-password":
        assert signup(client, "ana@example.com").status_code == 303
        outbox(client, "ana@example.com")  # drain nothing; just make sure the route exists
    page_token(client)
    data = {"email": "ana@example.com"} if path == "/forgot-password" else {"email": "ana@example.com", "password": PASSWORD}
    response = post_form(client, path, data, headers=POISON)
    assert response.status_code in (200, 303)
    bodies = "\n".join(message["body"] or "" for message in outbox(client, "ana@example.com"))
    assert "evil.example" not in bodies
    assert PUBLIC_URL in bodies


def test_every_queued_email_only_ever_points_at_the_configured_origin(app, client, second_client, fake_state):
    """Walk every template that carries a link and prove none of them can be redirected."""
    create_user(client, "ana@example.com", verified=False)
    post_form(client, "/account/verify/resend", {}, headers=POISON)
    post_form(client, "/account/security/email", {"new_email": "new@example.com", "email_password": PASSWORD}, headers=POISON)
    post_form(second_client, "/signup", {"email": "ana@example.com", "password": PASSWORD}, headers=POISON)
    forgot(second_client, "ana@example.com", headers=POISON)
    with app.state.db.read() as conn:
        rows = conn.execute("SELECT template, body FROM email_outbox").fetchall()
    templates = {row["template"] for row in rows}
    assert {"verify_email", "email_change_confirm", "email_change_notice", "password_reset"} <= templates
    for row in rows:
        assert "evil.example" not in (row["body"] or ""), row["template"]
        assert PUBLIC_URL in (row["body"] or ""), row["template"]
