"""Refusals look like the site and offer a way forward; a stale sign-in form simply comes back.

Before this, every refusal other than 404 and 500 was a bare browser page: "Forbidden", one line,
no header, no link. The most common one, a form left open too long, hit people signing in.
"""
from __future__ import annotations

from .helpers import PASSWORD, context_value, create_user, page_token, post_form


def test_a_stale_sign_in_form_comes_back_fresh_with_what_was_typed(client):
    create_user(client, "ana@example.com")
    client.post("/logout", data={"csrf_token": page_token(client, "/account/security")}, headers={"Origin": "http://127.0.0.1:8765"})
    response = post_form(client, "/login", {"email": "ana@example.com", "password": PASSWORD}, token="stale" * 9)
    assert response.status_code == 403
    assert context_value(response, "form")["email"] == "ana@example.com"
    assert "refreshed it" in context_value(response, "errors")["form"]
    # The page carries a fresh token, and the next try simply works.
    fresh = response.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    again = post_form(client, "/login", {"email": "ana@example.com", "password": PASSWORD}, token=fresh)
    assert again.status_code == 303


def test_a_stale_sign_up_form_keeps_where_the_person_was_going(client):
    response = post_form(client, "/signup", {"email": "bo@example.com", "password": PASSWORD, "next": "/connect?x=1"},
                         token="stale" * 9)
    assert response.status_code == 403
    assert context_value(response, "next") == "/connect?x=1"
    assert context_value(response, "form")["email"] == "bo@example.com"


def test_any_other_stale_form_gets_the_problem_page_with_a_way_back(client):
    create_user(client, "ana@example.com")
    response = post_form(client, "/account/security/password", {"current_password": PASSWORD, "new_password": "x" * 12},
                         token="stale" * 9, headers={"Referer": "http://127.0.0.1:8765/account/security"})
    assert response.status_code == 403
    assert context_value(response, "title") == "This page expired"
    assert context_value(response, "action") == {"label": "Reload the page", "href": "/account/security"}


def test_the_way_back_never_leaves_the_site(client):
    create_user(client, "ana@example.com")
    response = post_form(client, "/account/security/password", {"current_password": PASSWORD, "new_password": "x" * 12},
                         token="stale" * 9, headers={"Referer": "https://evil.example/phish"})
    assert context_value(response, "action")["href"] == "/"


def test_an_unconfirmed_address_is_told_how_to_confirm(client, fake_state):
    create_user(client, "ana@example.com", verified=False)
    response = post_form(client, "/billing/checkout", {"plan": "starter"})
    assert response.status_code == 403
    assert context_value(response, "title") == "Confirm your email first"
    assert "ana@example.com" in context_value(response, "message")
    assert context_value(response, "action")["href"] == "/verify-email/sent"


def test_too_many_attempts_says_how_long(client, monkeypatch):
    from starling_server.security import ratelimit

    monkeypatch.setattr(ratelimit, "TOKEN_FORM_PER_IP", ratelimit.Rule("email_token_ip", 1, 600))
    post_form(client, "/verify-email", {"token": "x" * 43})
    response = post_form(client, "/verify-email", {"token": "x" * 43})
    assert response.status_code == 429
    assert context_value(response, "title") == "Too many attempts"
    assert "minute" in context_value(response, "message")
