"""Sign in with Google, end to end through the gateway, against a stand-in Google (fake_google.py).

The rules under test are Google's own guidance on trusting an address: Google is authoritative for
Gmail and Workspace (`hd`) addresses, so an existing account on one is joined at once; for any other
address the account's password is needed once. And the one attack that account linking invites:
someone parks an unconfirmed account, with a password they know, on another person's address and
waits for the owner to arrive with Google.
"""
from __future__ import annotations

import html

import pytest

from starling_server.auth import google, identities
from starling_server.auth.accounts import NO_PASSWORD
from starling_server.config import load_settings, validate
from starling_server.security.middleware import content_security_policy

from ..conftest import PUBLIC_URL
from .fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from .helpers import PASSWORD, account_row, context_value, create_user, login, outbox, post_form, signup

GMAIL = "ana@gmail.com"


@pytest.fixture
def env(env):
    values = dict(env)
    values["GOOGLE_CLIENT_ID"] = CLIENT_ID
    values["GOOGLE_CLIENT_SECRET"] = CLIENT_SECRET
    return values


@pytest.fixture
def fake_google(monkeypatch, settings):
    fake = FakeGoogle(settings.public_url + google.CALLBACK_PATH)
    monkeypatch.setattr(google, "post_token", fake.post_token)
    monkeypatch.setattr(google, "get_json", fake.get_json)
    google._KEYS.clear()
    yield fake
    google._KEYS.clear()


def start(client, path: str = "/auth/google?intent=signup"):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


def round_trip(client, fake, path: str = "/auth/google?intent=signup", *, sub: str = "g-100", email: str = GMAIL, **claims):
    state, code = fake.authorize(start(client, path), sub=sub, email=email, **claims)
    return client.get("/auth/google/callback", params={"state": state, "code": code}, follow_redirects=False)


def flashes(client) -> str:
    return html.unescape(client.get("/privacy").text)


def logout(client) -> None:
    assert post_form(client, "/logout", {}).status_code == 303


def identity_of(app, email: str):
    row = account_row(app, email)
    with app.state.db.read() as conn:
        return identities.for_account(conn, row["id"]) if row else None


# --------------------------------------------------------------------------- off and on


def test_everything_is_a_404_until_google_is_configured(tmp_path, web_dir):
    from fastapi.testclient import TestClient

    from starling_server.app import create_app

    from ..conftest import base_env

    values = base_env(tmp_path / "off")
    values["STARLING_WEB_DIR"] = str(web_dir)
    settings = load_settings(values)
    assert not settings.google_sign_in_enabled
    with TestClient(create_app(settings), base_url=PUBLIC_URL) as client:
        for path in ("/auth/google", "/auth/google/callback", "/auth/google/new"):
            assert client.get(path, follow_redirects=False).status_code == 404, path
    assert "accounts.google.com" not in content_security_policy(settings)


def test_the_connect_google_form_may_redirect_to_google(settings):
    # Chrome applies form-action to the redirect after a form; without this "Connect Google" does nothing.
    assert "form-action 'self'" in content_security_policy(settings)
    assert "https://accounts.google.com" in content_security_policy(settings)


def test_half_a_configuration_is_refused(env):
    values = dict(env)
    values.pop("GOOGLE_CLIENT_SECRET")
    problems = validate(load_settings(values), "gateway")
    assert any("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET" in p for p in problems)
    values = dict(env, GOOGLE_CLIENT_ID="1234-project-id")
    assert any("apps.googleusercontent.com" in p for p in validate(load_settings(values), "gateway"))


def test_the_harness_stand_in_for_google_is_refused_in_production(env):
    values = dict(env, STARLING_ENV="production", STARLING_E2E_OIDC_BASE="http://127.0.0.1:9999")
    assert any("STARLING_E2E_OIDC_BASE" in p for p in validate(load_settings(values), "gateway"))
    # And even if it got past the check, production would still talk to Google itself.
    assert google.endpoints(load_settings(values)).token == google.TOKEN_URL


def test_the_client_secret_never_reaches_an_engine():
    from starling_server.engines import spawner

    assert "GOOGLE_CLIENT_SECRET" in spawner.SECRET_ENV_NAMES


# --------------------------------------------------------------------------- the round trip


def test_the_request_to_google_is_a_pkce_code_flow_with_a_fresh_state_and_nonce(client, fake_google, settings):
    first = start(client)
    second = start(client)
    assert first != second
    assert "code_challenge=" in first and "nonce=" in first and "state=" in first
    cookie = client.cookies.get(google.start_cookie_name(settings))
    assert cookie and "." in cookie  # signed; the verifier never leaves this browser in the clear
    response = round_trip(client, fake_google)
    assert response.status_code == 303
    call = fake_google.token_calls[-1]
    assert call["code_verifier"] and call["client_secret"] == CLIENT_SECRET


def test_sign_up_with_google_makes_a_confirmed_account_with_no_password(app, client, fake_google, fake_state):
    response = round_trip(client, fake_google)
    assert response.headers["location"] == "/account"
    row = account_row(app, GMAIL)
    assert row["email_verified_at"] is not None
    assert row["password_hash"] == NO_PASSWORD
    assert identity_of(app, GMAIL).subject == "g-100"
    page = client.get("/account")
    assert page.status_code == 200 and "Welcome to Starling" in html.unescape(page.text)
    # No confirmation email: Google already confirmed the address.
    assert outbox(client, GMAIL) == []


def test_signing_in_again_finds_the_account_by_google_id_not_by_address(app, client, second_client, fake_google):
    round_trip(client, fake_google)
    account_id = account_row(app, GMAIL)["id"]
    # The person changed the address on their Google account; it is still the same Google account.
    response = round_trip(second_client, fake_google, "/auth/google", email="ana.new@gmail.com")
    assert response.status_code == 303 and response.headers["location"] == "/account"
    with app.state.db.read() as conn:
        assert identities.find(conn, "google", "g-100").account_id == account_id
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_from_the_sign_in_page_a_new_google_account_is_asked_before_one_is_made(app, client, fake_google, fake_state):
    response = round_trip(client, fake_google, "/auth/google")
    assert response.headers["location"] == "/auth/google/new"
    assert account_row(app, GMAIL) is None
    page = client.get("/auth/google/new")
    assert context_value(page, "email") == GMAIL
    created = post_form(client, "/auth/google/new", {})
    assert created.status_code == 303 and created.headers["location"] == "/account"
    assert account_row(app, GMAIL)["password_hash"] == NO_PASSWORD
    assert client.get("/account").status_code == 200


def test_the_confirmation_page_without_a_google_sign_in_goes_back_to_sign_up(client, fake_google):
    response = client.get("/auth/google/new", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/signup"
    assert post_form(client, "/auth/google/new", {}).headers["location"] == "/signup"


def test_next_survives_the_round_trip_to_google(client, fake_google, fake_state):
    target = "/connect?client_id=abc"
    response = round_trip(client, fake_google, "/auth/google?intent=signup&next=%2Fconnect%3Fclient_id%3Dabc")
    assert response.headers["location"] == target


def test_an_open_redirect_is_not_possible_through_next(client, fake_google):
    response = round_trip(client, fake_google, "/auth/google?intent=signup&next=https%3A%2F%2Fevil.example")
    assert response.headers["location"] == "/account"


# --------------------------------------------------------------------------- existing accounts


def test_an_existing_gmail_account_is_joined_at_once(app, client, second_client, fake_google, fake_state):
    create_user(client, GMAIL)
    response = round_trip(second_client, fake_google, "/auth/google")
    assert response.headers["location"] == "/account"
    assert identity_of(app, GMAIL) is not None
    assert "Google is now connected" in html.unescape(second_client.get("/account").text)
    assert outbox(second_client, GMAIL)[-1]["template"] == "google_linked_notice"
    # The password still works.
    logout(second_client)
    assert login(client, GMAIL).status_code == 303


def test_a_workspace_address_is_joined_at_once_too(app, client, second_client, fake_google):
    create_user(client, "ana@firm.example")
    response = round_trip(second_client, fake_google, "/auth/google", email="ana@firm.example", hd="firm.example")
    assert response.headers["location"] == "/account"
    assert identity_of(app, "ana@firm.example") is not None


def test_any_other_address_needs_the_accounts_password_once(app, client, second_client, fake_google, fake_state):
    """Google only checked this address when the Google account was made; that is not proof of today."""
    create_user(client, "ana@example.com")
    response = round_trip(second_client, fake_google, "/auth/google", email="ana@example.com")
    assert response.headers["location"] == "/login"
    assert identity_of(app, "ana@example.com") is None
    page = second_client.get("/login")
    waiting = context_value(page, "google_link")
    assert waiting["email"] == "ana@example.com"
    assert context_value(page, "form")["email"] == "ana@example.com"
    signed_in = login(second_client, "ana@example.com")
    assert signed_in.status_code == 303
    assert identity_of(app, "ana@example.com").subject == "g-100"
    # And from now on Google alone is enough.
    logout(second_client)
    third = round_trip(second_client, fake_google, "/auth/google", email="ana@example.com")
    assert third.headers["location"] == "/account"


def test_a_waiting_google_account_never_joins_a_different_account(app, client, second_client, fake_google):
    create_user(client, "ana@example.com")
    create_user(second_client, "bo@example.com")
    logout(second_client)
    round_trip(second_client, fake_google, "/auth/google", email="ana@example.com")
    assert login(second_client, "bo@example.com").status_code == 303
    assert identity_of(app, "bo@example.com") is None
    assert identity_of(app, "ana@example.com") is None


def test_an_account_parked_on_someone_elses_gmail_is_taken_back_by_its_owner(app, client, second_client, fake_google):
    """The pre-hijack: an attacker signs up with the victim's Gmail and a password, and never confirms it.

    When the owner arrives with Google, the account becomes theirs and the attacker's password,
    sessions and extension connections stop working.
    """
    assert signup(client, GMAIL, "attacker knows this one").status_code == 303
    assert login(client, GMAIL, "attacker knows this one").status_code == 303
    assert client.get("/account/security").status_code == 200
    response = round_trip(second_client, fake_google, "/auth/google")
    assert response.headers["location"] == "/account"
    row = account_row(app, GMAIL)
    assert row["email_verified_at"] is not None and row["password_hash"] == NO_PASSWORD
    assert client.get("/account/security", follow_redirects=False).status_code == 303  # attacker signed out
    assert login(client, GMAIL, "attacker knows this one").status_code == 400
    assert "old password was removed" in flashes(second_client)


def test_a_google_account_with_an_unconfirmed_address_is_refused(app, client, fake_google):
    response = round_trip(client, fake_google, email_verified=False)
    assert response.headers["location"] == "/signup"
    assert account_row(app, GMAIL) is None
    assert "has not confirmed the email address" in flashes(client)


def test_email_verified_as_the_string_true_is_accepted(app, client, fake_google):
    round_trip(client, fake_google, email_verified="true")
    assert account_row(app, GMAIL) is not None


# --------------------------------------------------------------------------- what can go wrong on the way


def test_a_callback_without_its_start_cookie_does_nothing(app, client, second_client, fake_google):
    state, code = fake_google.authorize(start(client), sub="g-100", email=GMAIL)
    # Someone else's browser (or a link sent to the victim) cannot finish this sign-in.
    response = second_client.get("/auth/google/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert account_row(app, GMAIL) is None
    assert fake_google.token_calls == []
    assert "timed out" in flashes(second_client)


def test_a_callback_with_the_wrong_state_does_nothing(app, client, fake_google):
    _, code = fake_google.authorize(start(client), sub="g-100", email=GMAIL)
    response = client.get("/auth/google/callback", params={"state": "forged", "code": code}, follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert account_row(app, GMAIL) is None


def test_the_start_cookie_expires(client, fake_google, fake_clock):
    state, code = fake_google.authorize(start(client), sub="g-100", email=GMAIL)
    fake_clock.advance(google.START_TTL_S + 1)
    response = client.get("/auth/google/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert response.headers["location"] == "/login"


def test_cancelling_at_google_goes_back_quietly(client, fake_google):
    location = start(client, "/auth/google?intent=signup")
    state = dict(part.split("=", 1) for part in location.split("?", 1)[1].split("&"))["state"]
    response = client.get("/auth/google/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
    assert response.headers["location"] == "/signup"
    assert "cancelled" in flashes(client)


def test_google_failing_to_answer_is_a_calm_try_again(app, client, fake_google):
    fake_google.token_status = 503
    response = round_trip(client, fake_google, "/auth/google")
    assert response.headers["location"] == "/login"
    assert "did not confirm the sign-in" in flashes(client)
    assert account_row(app, GMAIL) is None


def test_a_code_is_good_once(app, client, fake_google):
    state, code = fake_google.authorize(start(client), sub="g-100", email=GMAIL)
    client.get("/auth/google/callback", params={"state": state, "code": code}, follow_redirects=False)
    logout(client)
    again = client.get("/auth/google/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert again.headers["location"] == "/login"  # the start cookie went with the first use


@pytest.mark.parametrize(
    "tamper, reason",
    [
        (lambda c: {**c, "aud": "someone-else.apps.googleusercontent.com"}, "audience"),
        (lambda c: {**c, "iss": "https://accounts.evil.example"}, "issuer"),
        (lambda c: {**c, "exp": c["iat"] - 3600}, "expired"),
        (lambda c: {**c, "iat": c["iat"] + 3600}, "issued in the future"),
        (lambda c: {**c, "nonce": "another-sign-in"}, "nonce"),
        (lambda c: {**c, "azp": "someone-else.apps.googleusercontent.com"}, "authorized party"),
        (lambda c: {k: v for k, v in c.items() if k != "sub"}, "no subject"),
    ],
)
def test_every_claim_is_checked(app, client, fake_google, tamper, reason):
    fake_google.tamper = tamper
    response = round_trip(client, fake_google)
    assert response.headers["location"] == "/signup", reason
    assert account_row(app, GMAIL) is None, reason


async def _verify(settings, token, nonce="n"):
    return await google.verify_id_token(settings, token, nonce=nonce)


def _claims(now, nonce="n"):
    return {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "g-1", "email": GMAIL,
            "email_verified": True, "iat": now, "exp": now + 600, "nonce": nonce}


def test_a_token_signed_with_any_other_key_is_refused(settings, fake_google):
    import anyio
    from cryptography.hazmat.primitives.asymmetric import rsa

    from starling_server.security import clock

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = fake_google.sign(_claims(clock.now()), key=other)
    with pytest.raises(google.GoogleError, match="signature"):
        anyio.run(_verify, settings, forged)
    good = fake_google.sign(_claims(clock.now()))
    assert anyio.run(_verify, settings, good).subject == "g-1"


@pytest.mark.parametrize("alg", ["none", "HS256", "RS512"])
def test_only_rs256_is_accepted_whatever_the_token_says(settings, fake_google, alg):
    import anyio

    from starling_server.security import clock

    token = fake_google.sign(_claims(clock.now()), alg=alg)
    with pytest.raises(google.GoogleError, match="signed the way Google signs"):
        anyio.run(_verify, settings, token)


def test_an_unknown_key_id_refetches_the_keys_but_not_on_every_token(settings, fake_google):
    import anyio

    from starling_server.security import clock

    anyio.run(_verify, settings, fake_google.sign(_claims(clock.now())))
    assert fake_google.jwks_calls == 1
    for _ in range(5):
        with pytest.raises(google.GoogleError):
            anyio.run(_verify, settings, fake_google.sign(_claims(clock.now()), kid="rotated-away"))
    assert fake_google.jwks_calls == 1  # a flood of forged key ids is not a flood of fetches


def test_callbacks_are_rate_limited_per_network(client, fake_google, monkeypatch):
    from starling_server.security import ratelimit

    monkeypatch.setattr(ratelimit, "GOOGLE_CALLBACK_PER_IP", ratelimit.Rule("google_callback_ip", 2, 600))
    for _ in range(2):
        round_trip(client, fake_google)
        logout(client)
    calls = len(fake_google.token_calls)
    response = round_trip(client, fake_google)
    assert "Too many sign-in attempts" in flashes(client)
    assert len(fake_google.token_calls) == calls
    assert response.status_code == 303


# --------------------------------------------------------------------------- accounts without a password


def test_a_google_only_account_cannot_be_signed_in_to_with_any_password(client, second_client, fake_google, settings):
    round_trip(client, fake_google)
    for guess in ("", "!none", PASSWORD):
        response = login(second_client, GMAIL, guess)
        assert response.status_code == 400
        assert "If you signed up with Google" in response.text


def test_deleting_a_google_only_account_needs_a_recent_sign_in(app, client, fake_google, fake_clock, fake_pool):
    round_trip(client, fake_google)
    fake_clock.advance(11 * 60)
    stale = post_form(client, "/account/delete", {"delete_confirmation": "DELETE"})
    assert stale.status_code == 400
    assert context_value(stale, "errors") == {"delete": "For your security, confirm it is you with Google first."}
    assert context_value(stale, "sign_in")["recent"] is False
    # Confirm with Google: a fresh sign-in, same account, straight back to the page.
    confirmed = round_trip(client, fake_google, "/auth/google?intent=reauth&next=/account/data")
    assert confirmed.headers["location"] == "/account/data"
    assert fake_google.codes == {}  # the code was used
    done = post_form(client, "/account/delete", {"delete_confirmation": "DELETE"})
    assert done.status_code == 303 and done.headers["location"] == "/goodbye"
    assert account_row(app, GMAIL) is None
    # Deleting removed the link to Google too: the same Google account can start again.
    fresh = round_trip(client, fake_google)
    assert fresh.headers["location"] == "/account"
    assert account_row(app, GMAIL) is not None


def test_confirming_with_a_different_google_account_is_refused(client, fake_google, fake_clock):
    round_trip(client, fake_google)
    fake_clock.advance(11 * 60)
    response = round_trip(client, fake_google, "/auth/google?intent=reauth&next=/account/data", sub="g-999", email="bo@gmail.com")
    assert response.headers["location"] == "/account/data"
    assert "different Google account" in flashes(client)
    page = client.get("/account/data")
    assert context_value(page, "sign_in")["recent"] is False


def test_a_google_only_account_can_add_a_password_without_signing_anything_out(app, client, second_client, fake_google, fake_pool):
    round_trip(client, fake_google)
    round_trip(second_client, fake_google, "/auth/google")
    added = post_form(client, "/account/security/password", {"new_password": PASSWORD})
    assert added.status_code == 303
    assert "Password added" in flashes(client)
    assert second_client.get("/account/security", follow_redirects=False).status_code == 200  # still signed in
    assert outbox(client, GMAIL)[-1]["template"] == "password_added_notice"
    logout(client)
    assert login(client, GMAIL).status_code == 303


def test_adding_a_password_later_needs_a_fresh_confirmation(client, fake_google, fake_clock):
    round_trip(client, fake_google)
    fake_clock.advance(11 * 60)
    response = post_form(client, "/account/security/password", {"new_password": PASSWORD})
    assert response.status_code == 400
    assert context_value(response, "errors") == {"password": "For your security, confirm it is you with Google first."}


def test_a_google_only_account_changes_its_email_after_a_recent_sign_in(client, fake_google):
    round_trip(client, fake_google)
    response = post_form(client, "/account/security/email", {"new_email": "ana.work@example.com"})
    assert response.status_code == 303
    assert outbox(client, "ana.work@example.com")[-1]["template"] == "email_change_confirm"


def test_the_security_page_describes_how_the_account_signs_in(client, fake_google):
    round_trip(client, fake_google)
    sign_in = context_value(client.get("/account/security"), "sign_in")
    assert sign_in["has_password"] is False and sign_in["recent"] is True and sign_in["google_available"] is True
    assert sign_in["google"]["email_masked"].endswith("@gmail.com") and "ana@" not in sign_in["google"]["email_masked"]
    assert sign_in["reauth_url"] == "/auth/google?intent=reauth&next=/account/security"


def test_the_owner_area_asks_a_google_only_owner_for_a_password_first(app, client, fake_google):
    round_trip(client, fake_google, email="owner@example.com")  # listed in STARLING_OWNER_EMAILS by the fixtures
    app.state.db.write(lambda conn: conn.execute("UPDATE accounts SET is_admin = 1 WHERE email_norm = 'owner@example.com'"))
    response = post_form(client, "/admin/totp", {"password": "", "code": "123456"})
    assert response.status_code == 400
    assert "needs a password" in context_value(response, "errors")["form"]


# --------------------------------------------------------------------------- connecting and disconnecting


def test_connecting_google_from_the_security_page(app, client, second_client, fake_google):
    create_user(client, "ana@example.com")
    location = post_form(client, "/auth/google/link", {}).headers["location"]
    state, code = fake_google.authorize(location, sub="g-300", email="ana.personal@gmail.com")
    assert fake_google.codes[code]["login_hint"] == "ana@example.com"
    response = client.get("/auth/google/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert response.headers["location"] == "/account/security"
    assert identity_of(app, "ana@example.com").subject == "g-300"
    # The Google account's own address differs, and that is fine: the link is the Google id.
    signed_in = round_trip(second_client, fake_google, "/auth/google", sub="g-300", email="ana.personal@gmail.com")
    assert signed_in.headers["location"] == "/account"
    assert account_row(app, "ana.personal@gmail.com") is None


def test_a_google_account_joins_only_one_starling_account(app, client, second_client, fake_google):
    round_trip(client, fake_google)  # g-100 now belongs to ana@gmail.com
    create_user(second_client, "bo@example.com")
    location = post_form(second_client, "/auth/google/link", {}).headers["location"]
    state, code = fake_google.authorize(location, sub="g-100", email=GMAIL)
    second_client.get("/auth/google/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert "already signs in to a different Starling account" in flashes(second_client)
    assert identity_of(app, "bo@example.com") is None


def test_connecting_needs_the_same_signed_in_account_at_both_ends(app, client, fake_google):
    create_user(client, "ana@example.com")
    location = post_form(client, "/auth/google/link", {}).headers["location"]
    logout(client)
    state, code = fake_google.authorize(location, sub="g-300", email="ana@gmail.com")
    response = client.get("/auth/google/callback", params={"state": state, "code": code}, follow_redirects=False)
    assert response.headers["location"].startswith("/login")
    assert identity_of(app, "ana@example.com") is None


def test_disconnecting_google_needs_the_password(app, client, fake_google):
    create_user(client, GMAIL)
    logout(client)
    round_trip(client, fake_google, "/auth/google")  # joined at once: a Gmail address
    wrong = post_form(client, "/account/security/google/disconnect", {"google_password": "not it"})
    assert wrong.status_code == 400 and "google_password" in context_value(wrong, "errors")
    right = post_form(client, "/account/security/google/disconnect", {"google_password": PASSWORD})
    assert right.status_code == 303
    assert identity_of(app, GMAIL) is None
    assert outbox(client, GMAIL)[-1]["template"] == "google_unlinked_notice"


def test_a_google_only_account_cannot_disconnect_google(app, client, fake_google):
    round_trip(client, fake_google)
    response = post_form(client, "/account/security/google/disconnect", {"google_password": ""})
    assert response.status_code == 400
    assert "Set a password first" in context_value(response, "errors")["google"]
    assert identity_of(app, GMAIL) is not None


def test_the_export_lists_the_connected_google_account(client, fake_google, fake_pool):
    import json

    round_trip(client, fake_google)
    body = json.loads(post_form(client, "/account/export", {}).content)
    methods = body["sign_in_methods"]
    assert methods[0]["provider"] == "google" and methods[0]["email"] == GMAIL and methods[0]["subject"] == "g-100"
