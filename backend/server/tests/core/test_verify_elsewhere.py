"""Confirmed on the phone, the sign-up tab on the computer carries on by itself.

The link opened in another browser confirms the address and signs nobody in THERE. The browser that
signed up asks GET /verify-email/status, then signs itself in with POST /verify-email/continue: it
typed the account's password a moment ago, so this proves nothing the sign-in page would not.
"""
from __future__ import annotations

import html

from fastapi.testclient import TestClient

from starling_server.web import flash

from ..conftest import PUBLIC_URL
from .helpers import NEW_PASSWORD, PASSWORD, context_value, create_user, link_token, login, post_form, signup
from .test_signup_code import code_for, enter, signed_in


def status(client) -> str:
    response = client.get("/verify-email/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()["state"]


def open_link_elsewhere(client, other, email: str):
    return post_form(other, "/verify-email", {"token": link_token(client, email, "/verify-email")})


def carry_on(client):
    return post_form(client, "/verify-email/continue", {})


def said(client) -> str:
    return html.unescape(client.get("/privacy").text)


def test_the_sign_up_tab_carries_on_once_the_link_is_opened_elsewhere(app, client, second_client):
    post_form(client, "/signup", {"email": "ana@example.com", "password": PASSWORD, "next": "/pricing"})
    assert status(client) == "waiting"
    early = carry_on(client)
    assert early.headers["location"] == "/verify-email/sent"  # nothing to carry on with yet
    assert "not confirmed yet" in said(client)
    assert not signed_in(client)

    phone = open_link_elsewhere(client, second_client, "ana@example.com")
    assert phone.headers["location"] == "/login"
    assert "Your other browser continues by itself" in said(second_client)
    assert not signed_in(second_client)

    assert status(client) == "confirmed"
    assert context_value(client.get("/verify-email/sent"), "confirmed") is True
    response = carry_on(client)
    assert response.status_code == 303 and response.headers["location"] == "/pricing"  # where they were going
    assert "Welcome to Starling" in said(client)
    assert signed_in(client)


def test_carrying_on_works_once(app, client):
    settings = app.state.settings
    signup(client, "ana@example.com")
    notice = client.cookies.get(flash.notice_cookie_name(settings))
    third = TestClient(app, base_url=PUBLIC_URL)  # another browser; the app's lifespan belongs to `client`
    open_link_elsewhere(client, third, "ana@example.com")
    assert carry_on(client).headers["location"] == "/account"
    post_form(client, "/logout", {})
    assert not signed_in(client)
    # The same notice cookie again, as if it had been copied: the sign-up it names is finished.
    client.cookies.set(flash.notice_cookie_name(settings), notice, domain="127.0.0.1", path="/")
    assert status(client) == "waiting"
    assert carry_on(client).headers["location"] == "/verify-email/sent"
    assert not signed_in(client)


def test_a_second_sign_up_still_needs_its_code_after_the_link(app, client, second_client):
    """An unconfirmed account parked on the address, then its owner signs up and opens the email on a phone.

    The owner's sign-up takes the account over with the owner's password, which only the code or the link
    in the owner's own browser may do. The parked sign-up's tab does not ride on the owner's click either.
    """
    signup(client, "ana@example.com", "the squatter's password")
    signup(second_client, "ana@example.com", NEW_PASSWORD)
    third = TestClient(app, base_url=PUBLIC_URL)
    phone = open_link_elsewhere(second_client, third, "ana@example.com")
    assert phone.headers["location"] == "/login"
    assert "type the code from the email in the browser where you signed up" in said(third)

    assert status(second_client) == "waiting"
    assert carry_on(second_client).headers["location"] == "/verify-email/sent"
    assert not signed_in(second_client)
    assert status(client) == "waiting"
    assert carry_on(client).headers["location"] == "/verify-email/sent"
    assert not signed_in(client)

    assert enter(second_client, code_for(second_client, "ana@example.com")).status_code == 303
    assert signed_in(second_client)
    assert login(client, "ana@example.com", NEW_PASSWORD).status_code == 303


def test_the_tab_does_not_carry_on_after_the_password_changed(client, second_client, fake_clock):
    signup(client, "ana@example.com")
    open_link_elsewhere(client, second_client, "ana@example.com")
    fake_clock.advance(5)
    assert login(second_client, "ana@example.com").status_code == 303
    changed = post_form(second_client, "/account/security/password",
                        {"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert changed.status_code == 303
    assert status(client) == "waiting"
    assert carry_on(client).headers["location"] == "/verify-email/sent"
    assert not signed_in(client)


def test_an_address_that_already_had_an_account_waits_like_any_other(client, second_client):
    create_user(client, "taken@example.com")
    signup(second_client, "taken@example.com", NEW_PASSWORD)
    assert status(second_client) == "waiting"
    assert context_value(second_client.get("/verify-email/sent"), "confirmed") is False
    assert carry_on(second_client).headers["location"] == "/verify-email/sent"
    assert not signed_in(second_client)


def test_a_browser_that_did_not_sign_up_learns_nothing(app, client):
    signup(client, "ana@example.com")
    third = TestClient(app, base_url=PUBLIC_URL)
    open_link_elsewhere(client, third, "ana@example.com")
    stranger = TestClient(app, base_url=PUBLIC_URL)
    assert status(stranger) == "waiting"
    assert carry_on(stranger).headers["location"] == "/login"
    assert not signed_in(stranger)


def test_a_signed_in_account_sees_its_confirmation_arrive(client, second_client, fake_clock):
    create_user(client, "ana@example.com", verified=False)
    assert status(client) == "waiting"
    fake_clock.advance(61)
    assert post_form(client, "/account/verify/resend", {}).headers["location"] == "/verify-email/sent"
    open_link_elsewhere(client, second_client, "ana@example.com")
    assert status(client) == "confirmed"
    assert context_value(client.get("/verify-email/sent"), "confirmed") is True
    response = carry_on(client)
    assert response.headers["location"] == "/account"
    assert "Your email address is confirmed." in said(client)
