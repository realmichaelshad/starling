"""Journey 10: Continue with Google, against the stand-in Google (local, mocked provider).

Proves the round trip a browser makes: the gateway sends it to "Google" with PKCE, state and
nonce; the account chooser sends it back with a one-time code; the gateway swaps the code for a
signed ID token, checks it and signs the person in. A new Google account gets a confirmed account
with no password and no confirmation email, and the extension connects to it through the same
sign-in, with nothing typed. The stand-in is `e2e/harness/fake_google.py`; real Google sign-in is
checked by whoever deploys Starling (docs/DEPLOYMENT.md).
"""
from __future__ import annotations

import html
import re
from urllib.parse import quote

import httpx

from e2e.tests.client import Website, new_state, pkce_pair, redirect_query, redirect_uri_for

EMAIL = "google.customer@gmail.com"
CHOOSER_FIELD = re.compile(r'name="(state|nonce|redirect_uri|code_challenge)" value="([^"]*)"')


def google_round_trip(site: Website, start_path: str, email: str) -> httpx.Response:
    """Press the Google button, pick `email` on the stand-in chooser, and come back to the site."""
    start = site.get(start_path)
    assert start.status_code == 303, start.text[:300]
    chooser_url = start.headers["location"]
    with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as browser_at_google:
        page = browser_at_google.get(chooser_url)
        assert page.status_code == 200 and "Choose an account" in page.text, page.text[:300]
        fields = {name: html.unescape(value) for name, value in CHOOSER_FIELD.findall(page.text)}
        chosen = browser_at_google.post(
            chooser_url.split("?", 1)[0],
            data={**fields, "email": email, "email_verified": "true", "choice": "continue"},
        )
    assert chosen.status_code == 303
    back = chosen.headers["location"]
    assert back.startswith(site.base_url + "/auth/google/callback?"), back
    return site.get(back[len(site.base_url):])


def test_signing_up_with_google_needs_no_password_and_no_email(stack, harness):
    site = Website(stack["gateway_url"], client_ip="203.0.113.71")
    try:
        done = google_round_trip(site, "/auth/google?intent=signup", EMAIL)
        assert done.status_code == 303 and done.headers["location"] == "/account"
        assert site.get("/account").status_code == 200
        row = harness.one("SELECT id, email_verified_at, password_hash FROM accounts WHERE email_norm = ?", (EMAIL,))
        assert row is not None and row["email_verified_at"] is not None and row["password_hash"] == "!none"
        identity = harness.one("SELECT provider, email FROM account_identities WHERE account_id = ?", (row["id"],))
        assert identity == {"provider": "google", "email": EMAIL}
        assert [m for m in site.outbox(EMAIL) if m["template"] == "verify_email"] == []
    finally:
        site.close()


def test_the_extension_connects_through_the_same_google_sign_in(stack, harness):
    site = Website(stack["gateway_url"], client_ip="203.0.113.72")
    try:
        verifier, challenge = pkce_pair()
        params = {
            "client_id": stack["extension_id"],
            "redirect_uri": redirect_uri_for(stack["extension_id"]),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": new_state(),
            "device_label": "Chrome on Windows",
        }
        connect = site.connect_page(params)
        assert connect.status_code == 303 and connect.headers["location"].startswith("/login?next=")
        target = connect.headers["location"].split("next=", 1)[1]  # already quoted by the gateway
        back = google_round_trip(site, f"/auth/google?next={target}", EMAIL)
        assert back.status_code == 303 and back.headers["location"].startswith("/connect?"), back.headers.get("location")
        query = redirect_query(site.approve_connect(params))
        token = site.http.post("/api/v1/devices/token", json={
            "grant_type": "authorization_code", "code": query["code"], "code_verifier": verifier,
            "redirect_uri": params["redirect_uri"], "client_id": params["client_id"],
            "device_label": "Chrome on Windows", "extension_version": "1.0.0",
        })
        assert token.status_code == 200, token.text
        device_token = token.json()["token"]
        session = site.http.get("/api/v1/session", headers={
            "Authorization": f"Bearer {device_token}", "X-Starling-Extension": "1.0.0", "X-Starling-Time-Zone": "UTC"})
        assert session.status_code == 200 and session.json()["account"]["email_verified"] is True
    finally:
        site.close()


def test_a_cancelled_google_sign_in_goes_back_to_where_it_started(stack):
    site = Website(stack["gateway_url"], client_ip="203.0.113.73")
    try:
        start = site.get("/auth/google?intent=signup&next=" + quote("/connect?x=1", safe=""))
        chooser_url = start.headers["location"]
        with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as browser_at_google:
            page = browser_at_google.get(chooser_url)
            fields = {name: html.unescape(value) for name, value in CHOOSER_FIELD.findall(page.text)}
            cancelled = browser_at_google.post(chooser_url.split("?", 1)[0], data={**fields, "choice": "cancel"})
        back = cancelled.headers["location"]
        landed = site.get(back[len(site.base_url):])
        assert landed.status_code == 303 and landed.headers["location"].startswith("/signup?next=")
    finally:
        site.close()
