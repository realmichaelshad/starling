"""Regression: forms on `Referrer-Policy: no-referrer` pages must work in a real browser.

The email verification, password reset and extension approval pages are served with
`Referrer-Policy: no-referrer`. Browsers then send `Origin: null` with those pages' own form
POSTs (Fetch, "serializing a request origin"), together with `Sec-Fetch-Site: same-origin`.
Refusing `Origin: null` outright made all three forms answer 403 in Chrome, so no customer
could verify an email address, reset a password or connect the extension. Found by the
browser journeys (`e2e/browser`), which drive a real Chrome; an HTTP client that sends the
public origin never sees it.
"""
from __future__ import annotations

import pytest

from starling_server.auth.csrf import origin_allowed

from ..conftest import TEST_EXTENSION_ID
from .helpers import (
    NEW_PASSWORD,
    connect_params,
    create_user,
    csrf_token,
    link_token,
    open_connect,
    page_token,
    pkce_pair,
    post_form,
    signup,
)
from .test_units import make_request

# What Chrome sends with a form POST from a `no-referrer` page of the same site.
BROWSER_NO_REFERRER = {"Sec-Fetch-Site": "same-origin"}


def test_null_origin_needs_a_same_origin_fetch_site(settings):
    same_origin = make_request(headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert origin_allowed(same_origin, settings)
    assert origin_allowed(make_request(headers={"Origin": "NULL", "Sec-Fetch-Site": "Same-Origin"}), settings)
    # Negative controls: `null` alone, and `null` from anywhere else, stay refused.
    assert not origin_allowed(make_request(headers={"Origin": "null"}), settings)
    for site in ("cross-site", "same-site", "none", ""):
        refused = make_request(headers={"Origin": "null", "Sec-Fetch-Site": site})
        assert not origin_allowed(refused, settings), site
    # A real foreign origin is never rescued by the fetch-site header.
    foreign = make_request(headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"})
    assert not origin_allowed(foreign, settings)


def test_the_verification_form_works_the_way_a_browser_posts_it(client):
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    page = client.get("/verify-email", params={"token": token})
    assert page.headers["referrer-policy"] == "no-referrer"
    cross_site = post_form(client, "/verify-email", {"token": token}, token=csrf_token(page), origin="null",
                           headers={"Sec-Fetch-Site": "cross-site"})
    assert cross_site.status_code == 403  # negative control
    posted = post_form(client, "/verify-email", {"token": token}, token=csrf_token(page), origin="null",
                       headers=BROWSER_NO_REFERRER)
    assert posted.status_code == 303, posted.text


def test_the_password_reset_form_works_the_way_a_browser_posts_it(client, second_client):
    create_user(client, "ana@example.com")
    assert post_form(second_client, "/forgot-password", {"email": "ana@example.com"}).status_code == 200
    token = link_token(second_client, "ana@example.com", "/reset-password")
    page = second_client.get("/reset-password", params={"token": token})
    assert page.headers["referrer-policy"] == "no-referrer"
    posted = post_form(second_client, "/reset-password", {"token": token, "password": NEW_PASSWORD},
                       token=csrf_token(page), origin="null", headers=BROWSER_NO_REFERRER)
    assert posted.status_code == 303, posted.text


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_the_extension_approval_form_works_the_way_a_browser_posts_it(client, fake_state, decision):
    create_user(client, "ana@example.com")
    _verifier, challenge = pkce_pair()
    page, request_id, state = open_connect(client, connect_params(challenge))
    assert page.headers["referrer-policy"] == "no-referrer"
    fields = {"connect_request_id": request_id, "connect_state": state}
    refused = post_form(client, f"/connect/{decision}", fields, token=csrf_token(page), origin="null")
    assert refused.status_code == 403  # negative control: `null` with no fetch-site header
    posted = post_form(client, f"/connect/{decision}", fields, token=csrf_token(page), origin="null",
                       headers=BROWSER_NO_REFERRER)
    assert posted.status_code == 303, posted.text
    assert posted.headers["location"].startswith(f"https://{TEST_EXTENSION_ID}.chromiumapp.org/connect?")


def test_a_token_from_another_browser_is_still_refused_with_a_null_origin(app, client):
    from fastapi.testclient import TestClient

    from ..conftest import PUBLIC_URL

    create_user(client, "ana@example.com")
    other_browser = TestClient(app, base_url=PUBLIC_URL)
    stolen = post_form(client, "/account/security/sessions", {}, token=page_token(other_browser), origin="null",
                       headers=BROWSER_NO_REFERRER)
    assert stolen.status_code == 403
