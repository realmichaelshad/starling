"""Forgot password without the chores: the sent page names the address, and the reset signs you in."""
from __future__ import annotations

import html

from .helpers import NEW_PASSWORD, PASSWORD, context_value, create_user, link_token, login, outbox, post_form


def test_the_sent_page_repeats_the_address_whether_or_not_it_has_an_account(client, second_client):
    create_user(client, "ana@example.com")
    known = post_form(second_client, "/forgot-password", {"email": "ana@example.com"})
    unknown = post_form(second_client, "/forgot-password", {"email": "nobody@example.com"})
    assert context_value(known, "form")["email"] == "ana@example.com"
    assert context_value(unknown, "form")["email"] == "nobody@example.com"
    assert context_value(known, "sent") is True and context_value(unknown, "sent") is True


def test_the_reset_signs_this_browser_in_and_everything_else_out(app, client, second_client):
    create_user(client, "ana@example.com")
    post_form(second_client, "/forgot-password", {"email": "ana@example.com"})
    token = link_token(second_client, "ana@example.com", "/reset-password")
    done = post_form(second_client, "/reset-password", {"token": token, "password": NEW_PASSWORD})
    assert done.status_code == 303 and done.headers["location"] == "/account"
    assert "Your new password is saved and you are signed in" in html.unescape(second_client.get("/privacy").text)
    assert second_client.get("/account/security", follow_redirects=False).status_code == 200  # signed in here
    assert client.get("/account/security", follow_redirects=False).status_code == 303  # and out everywhere else
    assert login(client, "ana@example.com", PASSWORD).status_code == 400
    assert login(client, "ana@example.com", NEW_PASSWORD).status_code == 303


def test_a_signed_in_person_who_forgot_their_password_can_email_themselves_a_link(client):
    create_user(client, "ana@example.com")
    before = len(outbox(client, "ana@example.com"))
    response = post_form(client, "/account/security/reset-link", {})
    assert response.status_code == 303
    assert outbox(client, "ana@example.com")[-1]["template"] == "password_reset"
    assert len(outbox(client, "ana@example.com")) == before + 1


def test_a_google_only_account_asking_for_a_reset_is_told_it_signs_in_with_google(client, second_client, monkeypatch):
    from starling_server.auth import accounts

    create_user(client, "ana@example.com")
    client.app.state.db.write(lambda conn: conn.execute(
        "UPDATE accounts SET password_hash = ? WHERE email_norm = 'ana@example.com'", (accounts.NO_PASSWORD,)))
    post_form(second_client, "/forgot-password", {"email": "ana@example.com"})
    message = outbox(second_client, "ana@example.com")[-1]
    assert message["template"] == "password_reset_google"
    assert "Continue with Google" in message["body"] and "/reset-password?token=" in message["body"]


def test_a_wrong_current_password_says_so_plainly(client):
    create_user(client, "ana@example.com")
    response = post_form(client, "/account/security/password", {"current_password": "nope nope nope", "new_password": NEW_PASSWORD})
    assert context_value(response, "errors")["current_password"] == "That is not your current password."
