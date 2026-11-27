"""Confirming a sign-up signs the person in: the 6-digit code, or the link in the same browser.

Both need two things at once: the browser that signed up (its notice cookie) and the inbox (the
code or the link). Either alone is not enough. The link opened in another browser still confirms
the address, as before, but signs nobody in.
"""
from __future__ import annotations

import html
import re

from starling_server.auth import email_tokens

from ..conftest import PUBLIC_URL
from .helpers import NEW_PASSWORD, PASSWORD, account_row, context_value, link_token, login, outbox, post_form, signup

CODE_RE = re.compile(r"Your confirmation code is (\d{3}) (\d{3})")


def code_for(client, email: str) -> str:
    for message in reversed(outbox(client, email)):
        match = CODE_RE.search(message["body"] or "")
        if match:
            return match.group(1) + match.group(2)
    raise AssertionError(f"no code sent to {email}")


def signed_in(client) -> bool:
    return client.get("/account/security", follow_redirects=False).status_code == 200


def said(client) -> str:
    return html.unescape(client.get("/privacy").text)


def enter(client, code: str):
    return post_form(client, "/verify-email/code", {"code": code})


def test_the_code_signs_in_the_browser_that_signed_up(app, client):
    assert signup(client, "ana@example.com").status_code == 303
    page = client.get("/verify-email/sent")
    assert context_value(page, "email") == "ana@example.com"  # in full, so a typo can be seen
    assert context_value(page, "code_form") is True
    message = outbox(client, "ana@example.com")[-1]
    assert message["subject"].startswith("Your Starling code: ")
    response = enter(client, code_for(client, "ana@example.com"))
    assert response.status_code == 303 and response.headers["location"] == "/account"
    assert signed_in(client)
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None
    # The code is spent.
    again = enter(client, code_for(client, "ana@example.com"))
    assert again.status_code in (303, 400)


def test_the_code_may_be_typed_with_its_space(client):
    signup(client, "ana@example.com")
    code = code_for(client, "ana@example.com")
    assert enter(client, f"{code[:3]} {code[3:]}").status_code == 303
    assert signed_in(client)


def test_after_the_code_the_person_goes_where_they_were_going(client):
    target = "/connect?client_id=abc"
    post_form(client, "/signup", {"email": "ana@example.com", "password": PASSWORD, "next": target})
    response = enter(client, code_for(client, "ana@example.com"))
    assert response.headers["location"] == target


def test_the_link_in_the_same_browser_signs_in_too(app, client):
    signup(client, "ana@example.com")
    token = link_token(client, "ana@example.com", "/verify-email")
    response = post_form(client, "/verify-email", {"token": token})
    assert response.status_code == 303 and response.headers["location"] == "/account"
    assert signed_in(client)
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None


def test_the_link_in_another_browser_confirms_but_signs_nobody_in(app, client, second_client):
    signup(client, "ana@example.com")
    token = link_token(client, "ana@example.com", "/verify-email")
    response = post_form(second_client, "/verify-email", {"token": token})
    assert response.headers["location"] == "/login"
    assert not signed_in(second_client)
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None
    # The tab where the person signed up can still finish with the code.
    assert enter(client, code_for(client, "ana@example.com")).status_code == 303
    assert signed_in(client)


def test_a_code_typed_in_another_browser_does_nothing(app, client, second_client):
    signup(client, "ana@example.com")
    response = enter(second_client, code_for(client, "ana@example.com"))
    assert response.status_code == 400
    assert not signed_in(second_client)
    assert account_row(app, "ana@example.com")["email_verified_at"] is None


def test_five_wrong_codes_finish_that_code_but_not_the_link(app, client):
    signup(client, "ana@example.com")
    right = code_for(client, "ana@example.com")
    wrong = "000000" if right != "000000" else "111111"
    for attempt in range(email_tokens.CODE_ATTEMPTS):
        response = enter(client, wrong)
        assert response.status_code == 400
    assert "stopped working after too many tries" in context_value(response, "errors")["code"]
    assert enter(client, right).status_code == 400  # even the right one, now
    assert not signed_in(client)
    token = link_token(client, "ana@example.com", "/verify-email")
    assert post_form(client, "/verify-email", {"token": token}).status_code == 303
    assert signed_in(client)


def test_a_code_that_is_not_six_digits_is_not_counted(client):
    signup(client, "ana@example.com")
    for _ in range(email_tokens.CODE_ATTEMPTS + 2):
        response = enter(client, "12 34")
        assert context_value(response, "errors")["code"] == "Enter the 6-digit code from the email."
    assert enter(client, code_for(client, "ana@example.com")).status_code == 303


def test_the_code_expires_with_the_link(client, fake_clock):
    signup(client, "ana@example.com")
    code = code_for(client, "ana@example.com")
    fake_clock.advance(email_tokens.VERIFY_TTL_S + 1)
    assert enter(client, code).status_code == 400
    assert not signed_in(client)


def test_signing_up_with_a_confirmed_address_shows_the_same_page_and_no_code_works(app, client, second_client):
    from .helpers import create_user

    create_user(client, "taken@example.com")
    signup(second_client, "taken@example.com", NEW_PASSWORD)
    page = second_client.get("/verify-email/sent")
    assert context_value(page, "code_form") is True  # the same page as for a new address
    assert outbox(second_client, "taken@example.com")[-1]["template"] == "signup_existing"
    for guess in ("123456", "000000"):
        assert enter(second_client, guess).status_code == 400
    assert not signed_in(second_client)
    assert login(second_client, "taken@example.com", PASSWORD).status_code == 303


def test_an_account_parked_on_someone_elses_address_goes_to_the_one_who_confirms(app, client, second_client):
    """Someone signs up with another person's address and never confirms. The owner signs up too.

    The owner's code comes to the owner's inbox and works only in the owner's browser; entering it
    hands them the account with THEIR password, and the parked sign-up's password, sessions and
    connected browsers stop working.
    """
    assert signup(client, "ana@example.com", "the squatter's password").status_code == 303
    assert login(client, "ana@example.com", "the squatter's password").status_code == 303  # unconfirmed, but signed in
    assert signup(second_client, "ana@example.com", NEW_PASSWORD).status_code == 303
    assert enter(second_client, code_for(second_client, "ana@example.com")).status_code == 303
    assert signed_in(second_client)
    assert not signed_in(client)
    assert login(client, "ana@example.com", "the squatter's password").status_code == 400
    assert login(client, "ana@example.com", NEW_PASSWORD).status_code == 303
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None


def test_the_first_sign_ups_link_opened_elsewhere_changes_no_password(app, client, second_client):
    signup(client, "ana@example.com", "the squatter's password")
    first_link = link_token(client, "ana@example.com", "/verify-email")
    signup(second_client, "ana@example.com", NEW_PASSWORD)
    # Opened in a third browser: it confirms the address, and hands nobody the account.
    from fastapi.testclient import TestClient

    third = TestClient(app, base_url=PUBLIC_URL)  # another browser; the app's lifespan belongs to `client`
    assert post_form(third, "/verify-email", {"token": first_link}).headers["location"] == "/login"
    assert not signed_in(third)
    assert login(client, "ana@example.com", NEW_PASSWORD).status_code == 400


def test_send_again_while_signed_out_sends_a_new_code_and_retires_the_old(client, fake_clock):
    signup(client, "ana@example.com")
    first = code_for(client, "ana@example.com")
    too_soon = post_form(client, "/verify-email/resend", {})
    assert too_soon.headers["location"] == "/verify-email/sent"
    fake_clock.advance(61)
    sent = post_form(client, "/verify-email/resend", {})
    assert sent.headers["location"] == "/verify-email/sent"
    assert "We sent a new email to ana@example.com" in said(client)
    second = code_for(client, "ana@example.com")
    if second != first:
        assert enter(client, first).status_code == 400
    assert enter(client, second).status_code == 303


def test_send_again_says_the_same_whether_or_not_the_address_had_an_account(client, second_client, fake_clock):
    from .helpers import create_user

    create_user(client, "taken@example.com")
    signup(second_client, "taken@example.com", NEW_PASSWORD)
    fake_clock.advance(61)
    before = len(outbox(second_client, "taken@example.com"))
    response = post_form(second_client, "/verify-email/resend", {})
    assert response.headers["location"] == "/verify-email/sent"
    assert "We sent a new email to taken@example.com" in said(second_client)
    assert len(outbox(second_client, "taken@example.com")) == before  # nothing to resend, and nothing said about it


def test_a_signed_in_account_confirms_with_the_code_from_a_resend(app, client, fake_clock, fake_state):
    from .helpers import create_user

    create_user(client, "ana@example.com", verified=False)
    fake_clock.advance(61)
    assert post_form(client, "/account/verify/resend", {}).headers["location"] == "/verify-email/sent"
    page = client.get("/verify-email/sent")
    assert context_value(page, "code_form") is True
    assert enter(client, code_for(client, "ana@example.com")).status_code == 303
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None


def test_the_check_your_email_page_offers_the_right_webmail(client):
    signup(client, "ana@gmail.com")
    assert context_value(client.get("/verify-email/sent"), "mail_link")["label"] == "Open Gmail"
