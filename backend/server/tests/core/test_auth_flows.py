"""Sign-up, sign-in, CSRF, sessions, verification, password reset and email change, end to end."""
from __future__ import annotations

from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.auth import admin, email_tokens, passwords, sessions
from starling_server.config import load_settings
from starling_server.security import ratelimit

from ..conftest import PUBLIC_URL, base_env
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
    outbox,
    page_token,
    post_form,
    session_cookie,
    signup,
)

WRONG = "wrong horse battery"


def cookie_names(response) -> list[str]:
    return sorted(header.split("=", 1)[0] for header in response.headers.get_list("set-cookie"))


# --------------------------------------------------------------------------- uniform responses


def test_signup_does_not_reveal_existing_accounts(app, client, second_client):
    create_user(client, "taken@example.com")
    fresh = signup(second_client, "fresh@example.com")
    fresh_page = second_client.get("/verify-email/sent")
    taken = signup(second_client, "taken@example.com", NEW_PASSWORD)
    taken_page = second_client.get("/verify-email/sent")

    assert fresh.status_code == taken.status_code == 303
    assert fresh.headers["location"] == taken.headers["location"] == "/verify-email/sent"
    assert cookie_names(fresh) == cookie_names(taken)
    # The page shows the address as typed (so a typo can be seen) and nothing else that differs.
    def neutral(text: str, address: str) -> str:
        return text.replace(address, "X").replace(address[0] + "***@example.com", "X")

    assert neutral(fresh_page.text, "fresh@example.com") == neutral(taken_page.text, "taken@example.com")

    # The mailbox owners learn what happened; the account itself is untouched.
    assert [m["template"] for m in outbox(second_client, "fresh@example.com")] == ["verify_email"]
    assert [m["template"] for m in outbox(second_client, "taken@example.com")] == ["verify_email", "signup_existing"]
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE email_norm = 'taken@example.com'").fetchone()[0] == 1
    assert login(second_client, "taken@example.com", NEW_PASSWORD).status_code == 400
    assert login(second_client, "taken@example.com", PASSWORD).status_code == 303


def test_login_failure_is_identical_for_unknown_address_and_wrong_password(client, second_client, monkeypatch):
    create_user(client, "ana@example.com")
    page_token(second_client)  # the browser already has its pre-session cookie
    calls: list[str] = []
    real_verify = passwords.PasswordHasher.verify

    def spy(self, hash, password):
        calls.append(hash)
        return real_verify(self, hash, password)

    monkeypatch.setattr(passwords.PasswordHasher, "verify", spy)
    unknown = login(second_client, "nobody@example.com", PASSWORD)
    unknown_calls = len(calls)
    wrong = login(second_client, "ana@example.com", WRONG)

    assert unknown.status_code == wrong.status_code == 400
    assert unknown.text.replace("nobody@example.com", "E") == wrong.text.replace("ana@example.com", "E")
    assert cookie_names(unknown) == cookie_names(wrong) == []
    assert unknown_calls == 1 and len(calls) == 2  # the same argon2 work in both cases (dummy hash)


def test_forgot_password_response_is_identical(client, second_client):
    create_user(client, "ana@example.com")
    page_token(second_client)
    known = post_form(second_client, "/forgot-password", {"email": "ana@example.com"})
    unknown = post_form(second_client, "/forgot-password", {"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    # The page repeats the address the visitor typed, and nothing else differs.
    assert known.text.replace("ana@example.com", "E") == unknown.text.replace("nobody@example.com", "E")
    assert context_value(known, "sent") is True
    assert outbox(second_client, "ana@example.com")[-1]["template"] == "password_reset"
    assert outbox(second_client, "nobody@example.com") == []


def test_signup_validation_errors(app, client, tmp_path):
    response = signup(client, "not-an-address", "short")
    assert response.status_code == 400
    errors = context_value(response, "errors")
    assert set(errors) == {"email", "password"}
    assert signup(client, "ana@example.com", "password123").status_code == 400
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


# --------------------------------------------------------------------------- brute force


def test_login_guessing_is_delayed_per_email(client, second_client, fake_clock):
    create_user(client, "ana@example.com")
    for _ in range(ratelimit.LOGIN_FREE_FAILURES):
        assert login(second_client, "ana@example.com", WRONG).status_code == 400
    delayed = login(second_client, "ana@example.com", PASSWORD)  # even the right password waits
    assert delayed.status_code == 429
    assert int(delayed.headers["retry-after"]) >= 1
    # Negative control: another address from the same browser is not delayed.
    assert login(second_client, "other@example.com", WRONG).status_code == 400
    # A delay, not a lockout: after waiting, the owner signs in.
    fake_clock.advance(ratelimit.login_delay_s(ratelimit.LOGIN_FREE_FAILURES))
    assert login(second_client, "ana@example.com", PASSWORD).status_code == 303


def test_login_attempts_are_limited_per_ip_prefix(app, client, monkeypatch):
    monkeypatch.setattr(ratelimit, "LOGIN_PER_IP", ratelimit.Rule("login_ip", 3, 900))
    attacker = TestClient(app, base_url=PUBLIC_URL, client=("203.0.113.10", 5000))
    neighbour = TestClient(app, base_url=PUBLIC_URL, client=("203.0.113.99", 5000))
    elsewhere = TestClient(app, base_url=PUBLIC_URL, client=("198.51.100.20", 5000))
    for index in range(3):
        assert login(attacker, f"user{index}@example.com", WRONG).status_code == 400
    blocked = login(attacker, "user9@example.com", WRONG)
    assert blocked.status_code == 429 and "retry-after" in blocked.headers
    assert login(neighbour, "user8@example.com", WRONG).status_code == 429  # same /24
    spoofed = post_form(attacker, "/login", {"email": "user7@example.com", "password": WRONG},
                        headers={"X-Forwarded-For": "192.0.2.1"})
    assert spoofed.status_code == 429  # the header is ignored without a trusted proxy
    assert login(elsewhere, "user6@example.com", WRONG).status_code == 400  # negative control


# --------------------------------------------------------------------------- CSRF


def test_state_changing_forms_require_token_and_same_origin(app, client, fake_state):
    create_user(client, "ana@example.com")
    token = page_token(client)
    target = "/account/security/sessions"
    assert post_form(client, target, {}, token=token).status_code == 303  # negative control
    assert client.post(target, data={}, headers={"Origin": PUBLIC_URL}, follow_redirects=False).status_code == 403
    assert post_form(client, target, {}, token="").status_code == 403
    assert post_form(client, target, {}, token=token, origin="https://evil.example").status_code == 403
    assert post_form(client, target, {}, token=token, origin="null").status_code == 403
    assert post_form(client, target, {}, token=token, origin=None).status_code == 403
    assert post_form(client, target, {}, token=token, origin=None, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert post_form(client, target, {}, token=token, origin=None, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 303
    other_browser = TestClient(app, base_url=PUBLIC_URL)
    assert post_form(client, target, {}, token=page_token(other_browser)).status_code == 403


def test_login_csrf_is_refused(app, client, second_client, settings):
    create_user(client, "attacker@example.com")
    attacker_browser = TestClient(app, base_url=PUBLIC_URL)
    attacker_token = page_token(attacker_browser)
    victim = second_client
    page_token(victim)  # the victim has visited the site before
    forged = victim.post(
        "/login",
        data={"email": "attacker@example.com", "password": PASSWORD, "csrf_token": attacker_token},
        headers={"Origin": PUBLIC_URL},
        follow_redirects=False,
    )
    assert forged.status_code == 403
    cross_site = post_form(victim, "/login", {"email": "attacker@example.com", "password": PASSWORD}, origin="https://evil.example")
    assert cross_site.status_code == 403
    assert session_cookie(victim, settings) is None
    assert login(victim, "attacker@example.com").status_code == 303  # negative control: the victim's own form


def test_csrf_token_signed_with_previous_key_only_during_rotation(tmp_path, web_dir):
    from .test_units import ROTATED_KEY
    from ..conftest import TEST_SECRET_KEY

    def app_for(env):
        env["STARLING_WEB_DIR"] = str(web_dir)
        return create_app(load_settings(env))

    data_dir = tmp_path / "rotation"
    old_app = app_for(base_env(data_dir))
    with TestClient(old_app, base_url=PUBLIC_URL) as browser:
        old_token = page_token(browser)
        cookies = dict(browser.cookies)

    rotating = base_env(data_dir)
    rotating.update({"STARLING_SECRET_KEY": ROTATED_KEY, "STARLING_SECRET_KEY_PREVIOUS": TEST_SECRET_KEY})
    with TestClient(app_for(rotating), base_url=PUBLIC_URL, cookies=cookies) as browser:
        accepted = post_form(browser, "/forgot-password", {"email": "ana@example.com"}, token=old_token)
        assert accepted.status_code == 200
        assert page_token(browser) != old_token  # new pages are signed with the current key

    finished = base_env(data_dir)
    finished["STARLING_SECRET_KEY"] = ROTATED_KEY
    with TestClient(app_for(finished), base_url=PUBLIC_URL, cookies=cookies) as browser:
        assert post_form(browser, "/forgot-password", {"email": "ana@example.com"}, token=old_token).status_code == 403


# --------------------------------------------------------------------------- sessions


def test_session_id_rotates_on_login(app, client, settings, fake_state):
    create_user(client, "ana@example.com")
    first = session_cookie(client, settings)
    assert login(client, "ana@example.com").status_code == 303
    second = session_cookie(client, settings)
    assert first and second and first != second
    stale = TestClient(app, base_url=PUBLIC_URL, cookies={settings.session_cookie_name: first})
    assert stale.get("/account", follow_redirects=False).status_code == 303
    current = TestClient(app, base_url=PUBLIC_URL, cookies={settings.session_cookie_name: second})
    assert current.get("/account", follow_redirects=False).status_code == 200


def test_session_cookie_attributes(tmp_path, web_dir, client):
    https = "https://starling.example"
    env = base_env(tmp_path / "https-data")
    env.update({"STARLING_WEB_DIR": str(web_dir), "STARLING_PUBLIC_URL": https})
    with TestClient(create_app(load_settings(env)), base_url=https) as browser:
        assert post_form(browser, "/signup", {"email": "ana@example.com", "password": PASSWORD}, origin=https).status_code == 303
        response = post_form(browser, "/login", {"email": "ana@example.com", "password": PASSWORD}, origin=https)
        assert response.status_code == 303
        header = next(h for h in response.headers.get_list("set-cookie") if h.startswith("__Host-starling_session="))
        attributes = [part.strip().lower() for part in header.split(";")[1:]]
        for expected in ("httponly", "secure", "path=/", "samesite=lax"):
            assert expected in attributes
        assert any(a.startswith("max-age=") for a in attributes)
        assert not any(a.startswith("domain=") for a in attributes)
        fresh_browser = TestClient(browser.app, base_url=https)
        csrf_cookie = next(h for h in fresh_browser.get("/terms").headers.get_list("set-cookie") if h.startswith("__Host-starling_csrf="))
        assert all(flag in csrf_cookie.lower() for flag in ("httponly", "secure", "path=/", "samesite=lax"))
    # Negative control on plain http (development): no __Host- prefix and no Secure flag.
    assert signup(client, "bob@example.com").status_code == 303
    plain = login(client, "bob@example.com")
    header = next(h for h in plain.headers.get_list("set-cookie") if h.startswith("starling_session="))
    assert "secure" not in header.lower()


def test_logout_is_post_only(client, settings, fake_state):
    create_user(client, "ana@example.com")
    assert client.get("/logout", follow_redirects=False).status_code == 405
    assert client.get("/account", follow_redirects=False).status_code == 200
    assert post_form(client, "/logout", {}).status_code == 303
    assert session_cookie(client, settings) is None
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_session_idle_and_absolute_expiry(client, fake_clock, fake_state):
    create_user(client, "ana@example.com")
    fake_clock.advance(sessions.IDLE_S - 60)
    assert client.get("/account", follow_redirects=False).status_code == 200  # the visit extends the idle window
    fake_clock.advance(sessions.IDLE_S - 60)
    assert client.get("/account", follow_redirects=False).status_code == 200
    fake_clock.advance(sessions.ABSOLUTE_S - 2 * (sessions.IDLE_S - 60) + 1)  # past 30 days although recently active
    expired = client.get("/account", follow_redirects=False)
    assert expired.status_code == 303 and expired.headers["location"] == "/login?next=%2Faccount"


def test_session_idle_expiry(client, fake_clock, fake_state):
    create_user(client, "ana@example.com")
    fake_clock.advance(sessions.IDLE_S + 1)
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_login_next_must_stay_on_this_site(client):
    assert signup(client, "ana@example.com").status_code == 303
    assert login(client, "ana@example.com", next_path="https://evil.example/").headers["location"] == "/account"
    assert login(client, "ana@example.com", next_path="/account/devices").headers["location"] == "/account/devices"


# --------------------------------------------------------------------------- verification and reset


def test_verification_needs_a_post_and_works_once(app, client):
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    for _ in range(2):
        page = client.get("/verify-email", params={"token": token})
        assert page.status_code == 200 and context_value(page, "token_valid") is True
    assert account_row(app, "ana@example.com")["email_verified_at"] is None
    assert post_form(client, "/verify-email", {"token": token}).status_code == 303
    assert account_row(app, "ana@example.com")["email_verified_at"] is not None
    assert post_form(client, "/verify-email", {"token": token}).status_code == 400
    assert context_value(client.get("/verify-email", params={"token": token}), "token_valid") is False


def test_verification_link_expires(app, client, fake_clock):
    assert signup(client, "ana@example.com").status_code == 303
    token = link_token(client, "ana@example.com", "/verify-email")
    fake_clock.advance(email_tokens.VERIFY_TTL_S + 1)
    assert post_form(client, "/verify-email", {"token": token}).status_code == 400
    assert account_row(app, "ana@example.com")["email_verified_at"] is None


def test_password_reset_revokes_sessions_and_devices(client, second_client, fake_state):
    create_user(client, "ana@example.com")
    device = connect_device(client)
    assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 200
    assert client.get("/account", follow_redirects=False).status_code == 200

    assert post_form(second_client, "/forgot-password", {"email": "ana@example.com"}).status_code == 200
    token = link_token(second_client, "ana@example.com", "/reset-password")
    assert context_value(second_client.get("/reset-password", params={"token": token}), "token_valid") is True
    assert post_form(second_client, "/reset-password", {"token": token, "password": NEW_PASSWORD}).status_code == 303

    assert client.get("/account", follow_redirects=False).status_code == 303
    revoked = client.get("/api/v1/session", headers=api_headers(device["token"]))
    assert revoked.status_code == 401 and revoked.json()["error"]["code"] == "signed_out"
    assert post_form(second_client, "/reset-password", {"token": token, "password": "yet another passphrase"}).status_code == 400
    assert login(second_client, "ana@example.com", NEW_PASSWORD).status_code == 303
    assert "password_reset_notice" in [m["template"] for m in outbox(second_client, "ana@example.com")]


def test_reset_link_expires_after_an_hour(client, second_client, fake_clock):
    create_user(client, "ana@example.com")
    post_form(second_client, "/forgot-password", {"email": "ana@example.com"})
    token = link_token(second_client, "ana@example.com", "/reset-password")
    fake_clock.advance(email_tokens.RESET_TTL_S + 1)
    assert post_form(second_client, "/reset-password", {"token": token, "password": NEW_PASSWORD}).status_code == 400
    assert login(second_client, "ana@example.com", PASSWORD).status_code == 303


def test_password_change_keeps_only_the_current_browser(app, client, second_client, settings, fake_state):
    create_user(client, "ana@example.com")
    assert login(second_client, "ana@example.com").status_code == 303
    device = connect_device(client)
    epoch_before = account_row(app, "ana@example.com")["auth_epoch"]
    cookie_before = session_cookie(client, settings)
    wrong = post_form(client, "/account/security/password", {"current_password": WRONG, "new_password": NEW_PASSWORD})
    assert wrong.status_code == 400
    assert second_client.get("/account", follow_redirects=False).status_code == 200  # negative control
    changed = post_form(client, "/account/security/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert changed.status_code == 303
    assert client.get("/account", follow_redirects=False).status_code == 200
    assert second_client.get("/account", follow_redirects=False).status_code == 303
    assert client.get("/api/v1/session", headers=api_headers(device["token"])).status_code == 401
    assert account_row(app, "ana@example.com")["auth_epoch"] == epoch_before + 1  # every device and session invalidated
    assert session_cookie(client, settings) not in (None, cookie_before)  # the current browser got a new session id
    assert outbox(client, "ana@example.com")[-1]["template"] == "password_changed_notice"


def test_email_change_flow(app, client, second_client, settings, fake_state):
    create_user(client, "owner@example.com")
    admin.grant_admin(app.state.db, settings, "owner@example.com")
    assert account_row(app, "owner@example.com")["is_admin"] == 1

    refused = post_form(client, "/account/security/email", {"email_password": WRONG, "new_email": "new@example.com"})
    assert refused.status_code == 400 and outbox(client, "new@example.com") == []

    requested = post_form(client, "/account/security/email", {"email_password": PASSWORD, "new_email": "new@example.com"})
    assert requested.status_code == 303
    confirm_token = link_token(client, "new@example.com", "/verify-email")
    notice = [m for m in outbox(client, "owner@example.com") if m["template"] == "email_change_notice"]
    assert notice and "7 days" in notice[-1]["body"]
    revert_token = link_token(client, "owner@example.com", "/reset-password")

    assert client.get("/verify-email", params={"token": confirm_token}).status_code == 200
    assert account_row(app, "owner@example.com") is not None  # GET changes nothing
    assert post_form(client, "/verify-email", {"token": confirm_token}).status_code == 303
    moved = account_row(app, "new@example.com")
    assert moved is not None and moved["is_admin"] == 0 and moved["totp_secret_enc"] is None
    assert account_row(app, "owner@example.com") is None

    reverted = post_form(second_client, "/reset-password", {"token": revert_token, "password": NEW_PASSWORD})
    assert reverted.status_code == 303
    restored = account_row(app, "owner@example.com")
    assert restored is not None and restored["is_admin"] == 0 and restored["id"] == moved["id"]
    assert client.get("/account", follow_redirects=False).status_code == 303  # signed out everywhere
    assert login(second_client, "owner@example.com", NEW_PASSWORD).status_code == 303
