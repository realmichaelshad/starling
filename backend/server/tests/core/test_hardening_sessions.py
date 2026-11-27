"""Adversarial cover for sign-in, session lifetime, CSRF and every redirect target.

The CSRF test walks the app's own route table, so a new state-changing form cannot be added
without this test noticing.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from starling_server.auth import csrf, sessions
from starling_server.security.crypto import sha256_hex
from starling_server.security.http import safe_next

from ..conftest import PUBLIC_URL
from .helpers import (
    NEW_PASSWORD,
    PASSWORD,
    account_row,
    create_user,
    login,
    page_token,
    post_form,
    session_cookie,
    signup,
)

EVIL = "https://evil.example"
# `/webhooks/stripe` is the one CSRF-exempt POST in the contract (section 13.15): it carries a
# Stripe signature instead. `/api/` never reads cookies, so it cannot be CSRF'd either.
CSRF_EXEMPT = {"/webhooks/stripe"}
PATH_PARAMETERS = {
    "device_id": "00000000-0000-4000-8000-000000000000",
    "account_id": "00000000-0000-4000-8000-000000000000",
    "page": "getting-started",
    "item_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",  # a writing sample (web/write.py)
}
# Admin routes resolve the admin identity before the CSRF token, so a signed-in customer is told
# the page does not exist rather than that the token was wrong. Either way the form does nothing.
REFUSALS = (403, 404)


def all_routes(container):
    """Every endpoint of the app, including the ones this FastAPI keeps inside included routers."""
    for route in getattr(container, "routes", None) or []:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from all_routes(nested)
        elif getattr(route, "routes", None):
            yield from all_routes(route)
        else:
            yield route


def website_post_paths(app) -> list[str]:
    found = set()
    for route in all_routes(app):
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if "POST" not in methods or path.startswith("/api/") or path in CSRF_EXEMPT:
            continue
        for name, value in PATH_PARAMETERS.items():
            path = path.replace("{" + name + "}", value)
        assert "{" not in path, f"add a stand-in for the path parameter in {path}"
        found.add(path)
    return sorted(found)


def session_row(app, cookie_value: str):
    with app.state.db.read() as conn:
        return conn.execute(
            "SELECT revoked_at FROM web_sessions WHERE id_hash = ?", (sha256_hex(cookie_value),)
        ).fetchone()


# --------------------------------------------------------------------------- CSRF on every form


def test_every_state_changing_form_needs_the_token_and_the_origin(app, client, fake_state):
    """Contract section 13.15, checked against the live route table rather than a hand-kept list."""
    create_user(client, "ana@example.com")
    paths = website_post_paths(app)
    assert "/login" in paths and "/account/delete" in paths and "/connect/approve" in paths
    token = page_token(client)
    for path in paths:
        expected = REFUSALS if path.startswith("/admin/") else (403,)
        foreign = post_form(client, path, {}, token=token, origin=EVIL)
        assert foreign.status_code in expected, f"{path} accepted a cross-site Origin"
        without_token = client.post(path, data={}, headers={"Origin": PUBLIC_URL}, follow_redirects=False)
        assert without_token.status_code in expected, f"{path} accepted a missing CSRF token"
        stolen = post_form(client, path, {}, token="x" * 43)
        assert stolen.status_code in expected, f"{path} accepted a forged CSRF token"


def test_no_state_changing_route_answers_a_get(app, client, fake_state):
    create_user(client, "ana@example.com")
    get_and_post = {"/signup", "/login", "/verify-email", "/forgot-password", "/reset-password",
                    "/admin/totp", "/admin/elevate", "/auth/google/new"}
    for path in website_post_paths(app):
        if path in get_and_post:
            continue
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 405, f"{path} answered a GET with {response.status_code}"


def test_a_missing_origin_is_only_accepted_with_a_same_origin_fetch_site(client, fake_state):
    create_user(client, "ana@example.com")
    token = page_token(client)
    body = {"csrf_token": token}
    assert client.post("/account/security/sessions", data=body, follow_redirects=False).status_code == 403
    for value in ("cross-site", "same-site", "none", ""):
        response = client.post(
            "/account/security/sessions", data=body, headers={"Sec-Fetch-Site": value}, follow_redirects=False
        )
        assert response.status_code == 403, value
    ok = client.post(
        "/account/security/sessions", data=body, headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False
    )
    assert ok.status_code == 303


@pytest.mark.parametrize(
    "origin, allowed",
    [
        (PUBLIC_URL, True),
        (PUBLIC_URL + "/", True),
        ("HTTP://127.0.0.1:8765", True),
        (" http://127.0.0.1:8765 ", True),
        ("http://127.0.0.1:8766", False),  # another port on the same host
        ("https://127.0.0.1:8765", False),  # another scheme
        ("http://127.0.0.1.:8765", False),  # trailing dot
        ("http://127.0.0.1:8765.evil.example", False),
        ("http://user:pass@127.0.0.1:8765", False),
        ("http://evil.example@127.0.0.1:8765", False),
        ("http://127.0.0.1:8765@evil.example", False),
        ("http://127.0.0.1:8765/path", False),
        ("http://127.0.0.1:8765?q=1", False),
        ("//127.0.0.1:8765", False),
        ("file://", False),
        ("", False),
        ("null", False),
    ],
)
def test_origin_header_values(settings, origin, allowed):
    class FakeRequest:
        def __init__(self, headers):
            self.headers = headers

    assert csrf.origin_allowed(FakeRequest({"origin": origin}), settings) is allowed


def test_a_forged_origin_cannot_sign_a_visitor_in(app, client, second_client, fake_state):
    """Login CSRF: a foreign page must not be able to plant the attacker's session."""
    create_user(client, "ana@example.com")
    page_token(second_client)
    attempt = post_form(second_client, "/login", {"email": "ana@example.com", "password": PASSWORD}, origin=EVIL)
    assert attempt.status_code == 403
    assert session_cookie(second_client, app.state.settings) is None


# --------------------------------------------------------------------------- session identity


def planted_browser(app, settings, value: str) -> TestClient:
    browser = TestClient(app, base_url=PUBLIC_URL)
    # Set it for the real host so a cookie the server issues later replaces it instead of
    # sitting beside it.
    browser.cookies.set(settings.session_cookie_name, value, domain="127.0.0.1", path="/")
    return browser


def test_a_planted_session_id_is_never_adopted(app, settings, fake_state):
    """Session fixation: an id the attacker chose must stay worthless after the victim signs in."""
    planted = "P" * 43
    victim = planted_browser(app, settings, planted)
    assert signup(victim, "ana@example.com").status_code == 303
    assert login(victim, "ana@example.com").status_code == 303
    issued = victim.cookies.get(settings.session_cookie_name)
    assert issued and issued != planted
    assert victim.get("/account", follow_redirects=False).status_code == 200
    attacker = planted_browser(app, settings, planted)
    assert attacker.get("/account", follow_redirects=False).status_code == 303
    assert session_row(app, planted) is None


def test_the_csrf_pre_session_cookie_is_not_a_session(app, client, settings):
    assert signup(client, "ana@example.com").status_code == 303
    page_token(client)
    pre_session = client.cookies.get(csrf.presession_cookie_name(settings))
    assert pre_session and pre_session != session_cookie(client, settings)
    forged = TestClient(app, base_url=PUBLIC_URL, cookies={settings.session_cookie_name: pre_session})
    assert forged.get("/account", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("value", ["", "x", "x" * 43, "x" * 200, "../../etc", "%00", "a b"])
def test_a_tampered_session_cookie_is_ignored(app, settings, value, fake_state):
    browser = TestClient(app, base_url=PUBLIC_URL, cookies={settings.session_cookie_name: value})
    response = browser.get("/account", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_signing_in_again_ends_the_previous_session_server_side(app, client, settings, fake_state):
    create_user(client, "ana@example.com")
    first = session_cookie(client, settings)
    assert login(client, "ana@example.com").status_code == 303
    assert session_row(app, first)["revoked_at"] is not None


def test_sign_out_ends_the_session_in_the_database_not_only_in_the_browser(app, client, settings, fake_state):
    create_user(client, "ana@example.com")
    cookie = session_cookie(client, settings)
    copy = TestClient(app, base_url=PUBLIC_URL, cookies={settings.session_cookie_name: cookie})
    assert copy.get("/account", follow_redirects=False).status_code == 200
    assert post_form(client, "/logout", {}).status_code == 303
    assert session_row(app, cookie)["revoked_at"] is not None
    # A browser that kept the cookie is signed out too, because the server forgot the session.
    assert copy.get("/account", follow_redirects=False).status_code == 303


def test_activity_never_pushes_a_session_past_its_absolute_expiry(app, client, settings, fake_clock, fake_state):
    create_user(client, "ana@example.com")
    cookie = session_cookie(client, settings)
    with app.state.db.read() as conn:
        row = conn.execute(
            "SELECT created_at, abs_expires_at FROM web_sessions WHERE id_hash = ?", (sha256_hex(cookie),)
        ).fetchone()
    assert row["abs_expires_at"] == row["created_at"] + sessions.ABSOLUTE_S
    # Two visits just inside the idle window keep the session alive and slide the idle clock,
    # but never move the absolute one.
    for _ in range(2):
        fake_clock.advance(sessions.IDLE_S - 3600)
        assert client.get("/account", follow_redirects=False).status_code == 200
        with app.state.db.read() as conn:
            current = conn.execute(
                "SELECT idle_expires_at, abs_expires_at FROM web_sessions WHERE id_hash = ?", (sha256_hex(cookie),)
            ).fetchone()
        assert current["abs_expires_at"] == row["abs_expires_at"]
        assert current["idle_expires_at"] <= row["abs_expires_at"]
    # A constantly active browser is still signed out 30 days after it signed in.
    fake_clock.advance(sessions.ABSOLUTE_S)
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_the_cookie_never_outlives_the_session(app, client, settings, fake_state):
    create_user(client, "ana@example.com")
    header = next(h for h in login(client, "ana@example.com").headers.get_list("set-cookie")
                  if h.startswith(settings.session_cookie_name + "="))
    max_age = int(next(part.split("=", 1)[1] for part in header.split(";") if part.strip().lower().startswith("max-age")))
    assert 0 < max_age <= sessions.ABSOLUTE_S


def test_password_change_rotates_the_id_and_kills_the_old_one(app, client, settings, fake_state):
    create_user(client, "ana@example.com")
    before = session_cookie(client, settings)
    assert post_form(client, "/account/security/password",
                     {"current_password": PASSWORD, "new_password": NEW_PASSWORD}).status_code == 303
    after = session_cookie(client, settings)
    assert after and after != before
    assert session_row(app, before)["revoked_at"] is not None
    stale = TestClient(app, base_url=PUBLIC_URL, cookies={settings.session_cookie_name: before})
    assert stale.get("/account", follow_redirects=False).status_code == 303


def test_signing_out_other_sessions_leaves_only_this_browser(app, client, second_client, settings, fake_state):
    create_user(client, "ana@example.com")
    assert login(second_client, "ana@example.com").status_code == 303
    mine = session_cookie(client, settings)
    theirs = session_cookie(second_client, settings)
    assert post_form(client, "/account/security/sessions", {}).status_code == 303
    assert session_row(app, theirs)["revoked_at"] is not None
    assert session_row(app, mine)["revoked_at"] is None
    assert client.get("/account", follow_redirects=False).status_code == 200
    assert second_client.get("/account", follow_redirects=False).status_code == 303


def test_the_rule_for_what_survives_a_password_change_and_a_reset(app, client, second_client, settings, fake_state):
    """The decided rule, stated in one place:

    - Voluntary change (the current password was typed): every other browser and every connected
      extension is signed out; this browser stays signed in on a new session id.
    - Reset from an email link (the password was not known): every browser and extension that was
      signed in is signed out. The browser that used the link is signed in on a NEW session: whoever
      holds the link has just chosen the password and could sign in with it a second later, so
      making them type it again protects nothing (it was once the rule, and only cost every honest
      reset a second sign-in).
    """
    from .helpers import api_headers, connect_device, link_token

    create_user(client, "ana@example.com")
    assert login(second_client, "ana@example.com").status_code == 303
    device = connect_device(client)
    assert post_form(client, "/account/security/password",
                     {"current_password": PASSWORD, "new_password": NEW_PASSWORD}).status_code == 303
    assert client.get("/account", follow_redirects=False).status_code == 200  # this browser survives
    assert second_client.get("/account", follow_redirects=False).status_code == 303
    assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 401

    third = TestClient(app, base_url=PUBLIC_URL)
    assert login(third, "ana@example.com", NEW_PASSWORD).status_code == 303
    later = connect_device(third)
    assert post_form(third, "/forgot-password", {"email": "ana@example.com"}).status_code == 200
    token = link_token(third, "ana@example.com", "/reset-password")
    before = third.cookies.get(settings.session_cookie_name)
    assert post_form(third, "/reset-password", {"token": token, "password": "a third long passphrase"}).status_code == 303
    assert third.get("/account", follow_redirects=False).status_code == 200  # the browser that used the link
    assert third.cookies.get(settings.session_cookie_name) != before  # on a new session, not the old one
    assert client.get("/account", follow_redirects=False).status_code == 303  # every other browser is out
    assert third.get("/api/v1/session", headers=api_headers(later["token"])).status_code == 401  # and every extension


def test_an_email_change_does_not_sign_anything_out(app, client, second_client, settings, fake_state):
    """Confirming a new address needs the current password, so the session that asked for it and
    the customer's other browsers keep working; only the revert link signs everything out."""
    from .helpers import api_headers, connect_device, link_token

    create_user(client, "ana@example.com")
    device = connect_device(client)
    assert post_form(client, "/account/security/email",
                     {"new_email": "new@example.com", "email_password": PASSWORD}).status_code == 303
    token = link_token(client, "new@example.com", "/verify-email")
    assert post_form(client, "/verify-email", {"token": token}).status_code == 303
    assert client.get("/account", follow_redirects=False).status_code == 200
    assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 200
    # The old address no longer signs in; the new one does.
    assert login(second_client, "ana@example.com").status_code == 400
    assert login(second_client, "new@example.com").status_code == 303


# --------------------------------------------------------------------------- account status


def test_an_account_being_deleted_cannot_sign_in(app, client, second_client, fake_state):
    create_user(client, "ana@example.com")
    row = account_row(app, "ana@example.com")
    app.state.db.write(lambda conn: conn.execute("UPDATE accounts SET status = 'deleting' WHERE id = ?", (row["id"],)))
    assert login(second_client, "ana@example.com").status_code == 400
    assert client.get("/account", follow_redirects=False).status_code == 303  # the live session stops too
    # And the reset route offers no way back in.
    assert post_form(second_client, "/forgot-password", {"email": "ana@example.com"}).status_code == 200
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_tokens WHERE purpose = 'reset'").fetchone()[0] == 0


def test_a_suspended_account_can_still_reach_its_own_account_pages(app, client, fake_state):
    """Suspension stops the product, not the customer's access to billing and their data."""
    create_user(client, "ana@example.com")
    row = account_row(app, "ana@example.com")
    app.state.db.write(lambda conn: conn.execute("UPDATE accounts SET status = 'suspended' WHERE id = ?", (row["id"],)))
    assert client.get("/account", follow_redirects=False).status_code == 200
    assert login(client, "ana@example.com").status_code == 303


# --------------------------------------------------------------------------- redirect targets


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example",
        "///evil.example",
        "//evil.example/path",
        "/\\evil.example",
        "\\\\evil.example",
        "\\/evil.example",
        "https://evil.example",
        "https:evil.example",
        "http:/evil.example",
        "javascript:alert(1)",
        "data:text/html,x",
        "/account\nLocation: https://evil.example",
        "/account\r\nSet-Cookie: a=b",
        "/account\tx",
        "evil.example",
        "",
        "x" * 3000,
    ],
)
def test_next_targets_that_leave_this_site_are_dropped(value):
    assert safe_next(value, "/account") == "/account"


@pytest.mark.parametrize("value", ["/account", "/account/devices", "/account?tab=1", "/pricing#plans", "/a/b/c"])
def test_same_origin_next_targets_survive(value):
    assert safe_next(value, "/fallback") == value


@pytest.mark.parametrize("value", ["//evil.example", "/\\evil.example", "https://evil.example", "\\\\evil.example"])
def test_the_login_form_never_redirects_off_site(client, value, fake_state):
    assert signup(client, "ana@example.com").status_code == 303
    response = login(client, "ana@example.com", next_path=value)
    assert response.status_code == 303 and response.headers["location"] == "/account"


def test_the_login_page_link_back_stays_on_this_site(client):
    page = client.get("/login", params={"next": "//evil.example"})
    assert page.status_code == 200
    from .helpers import context_value

    assert context_value(page, "next") == "/account"
