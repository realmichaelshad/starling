"""Journey 1: sign up, verify, sign in, and see the free plan (local, mocked providers).

Proves: the sign-up form creates an account, the verification email really reaches the
development outbox, the link in it verifies the address, and the three views of the account
(the `/account` page, `GET /api/v1/session` and the control database) show the same free plan
and the same allowance from `config/plans.toml`. The CSRF token and the Origin check are both
enforced on the sign-up form.
"""
from __future__ import annotations

import time

import pytest

from e2e.tests.client import Account, Control, Website


def assert_free_plan_agrees(account: Account, harness: Control, plans: dict, *, used: int = 0) -> dict:
    """The one assertion journey 1 makes, in all three places (CONTRACTS 6.2)."""
    allowance = plans["free"]["monthly_credits"]
    session = account.device.session()
    page = account.site.account_usage()
    row = harness.one(
        "SELECT p.plan_code, p.allowance, p.source FROM entitlement_periods p WHERE p.account_id = ? "
        "ORDER BY p.period_start DESC LIMIT 1",
        (account.id,),
    )
    assert session["plan"]["code"] == "free" and session["plan"]["status"] == "free"
    assert session["usage"]["allowance"] == allowance
    assert session["usage"]["used"] == used
    assert session["usage"]["remaining"] == allowance - used - session["usage"]["reserved"]
    assert page["plan_code"] == "free"
    assert page["usage"]["allowance"] == allowance
    assert page["usage"]["used"] == used
    assert page["usage"]["remaining"] == session["usage"]["remaining"]
    assert row is not None and row["plan_code"] == "free" and row["source"] == "free"
    assert row["allowance"] == allowance
    return session


def test_signup_sends_a_verification_email_and_verifying_signs_the_account_in(
    accounts, harness: Control, plans: dict
):
    account = accounts("signup", with_device=True)

    messages = account.site.outbox(account.email)
    assert messages, "no email reached the development outbox"
    verification = [m for m in messages if "/verify-email?token=" in (m.get("body") or "")]
    assert verification, f"no verification link in {[m['template'] for m in messages]}"
    assert account.email not in verification[-1]["body"].split("token=")[1], "the token is not the address"

    row = harness.one("SELECT email_verified_at, status FROM accounts WHERE id = ?", (account.id,))
    assert row["status"] == "active" and row["email_verified_at"] is not None

    session = assert_free_plan_agrees(account, harness, plans)
    assert session["account"]["email_verified"] is True
    assert session["account"]["email_masked"].endswith("@starling.example")
    assert account.email.split("@")[0] not in session["account"]["email_masked"]


def test_the_account_page_and_the_session_show_the_configured_allowance(accounts, harness: Control, plans: dict):
    account = accounts("signup", with_device=True)
    session = assert_free_plan_agrees(account, harness, plans)
    assert session["usage"]["unit"] == "draft" and session["usage"]["unit_label"] == "drafts"
    assert session["extension"]["min_version"] == "1.0.0"
    assert session["service"]["status"] == "ok"
    assert "Free" in account.site.account_usage()["html"]


@pytest.mark.parametrize(
    "case",
    ["missing_token", "wrong_token", "no_origin", "foreign_origin"],
)
def test_the_signup_form_needs_its_csrf_token_and_a_same_origin_post(site: Website, harness: Control, case: str):
    email = f"csrf-{case}-{int(time.time())}@starling.example"
    site.get("/signup")  # take the pre-session cookie, as a browser would
    kwargs: dict = {}
    if case == "missing_token":
        kwargs["token"] = ""
    elif case == "wrong_token":
        kwargs["token"] = "not-the-right-token"
    elif case == "no_origin":
        kwargs["origin"] = None
    else:
        kwargs["origin"] = "https://evil.example"
    response = site.post_form("/signup", {"email": email, "password": "a long enough passphrase"}, **kwargs)
    assert response.status_code == 403, response.text[:300]
    assert harness.one("SELECT id FROM accounts WHERE email_norm = ?", (email.lower(),)) is None


def test_the_verification_link_ignores_a_poisoned_host_header(site: Website, harness: Control):
    """Only the whole stack proves this: behind the harness's trusted proxy, `Host` and
    `X-Forwarded-Host` are attacker-controlled, and a link built from either would put an
    attacker's address into a customer's inbox."""
    email = f"host-poison-{int(time.time())}@starling.example"
    response = site.post_form(
        "/signup",
        {"email": email, "password": "a long enough passphrase"},
        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"},
    )
    assert response.status_code == 303, response.text[:300]
    messages = site.outbox(email)
    assert messages, "no email reached the development outbox"
    bodies = "\n".join(message.get("body") or "" for message in messages)
    assert "evil.example" not in bodies
    # `link_token` only matches links on the configured public origin, so finding one proves it.
    token = site.link_token(email, "/verify-email")
    assert site.post_form("/verify-email", {"token": token}).status_code == 303
    assert harness.one("SELECT email_verified_at FROM accounts WHERE email_norm = ?", (email.lower(),))["email_verified_at"]


def test_signing_out_ends_the_session_on_the_server_not_only_in_the_browser(site: Website, gateway_url: str):
    """A cookie copied out of the browser must stop working the moment the customer signs out."""
    email = f"signout-{int(time.time())}@starling.example"
    assert site.signup(email).status_code == 303
    assert site.verify_email(email).status_code == 303
    assert site.login(email).status_code == 303
    cookie = next(
        (item for item in site.http.cookies.jar if item.name.endswith("starling_session")), None
    )
    assert cookie is not None, "no session cookie was set"

    copy = Website(gateway_url)
    try:
        copy.http.cookies.set(cookie.name, cookie.value)
        assert copy.get("/account").status_code == 200  # the copy really is signed in
        assert site.logout().status_code == 303
        signed_out = copy.get("/account")
        assert signed_out.status_code == 303 and "/login" in signed_out.headers.get("location", "")
    finally:
        copy.close()


def test_a_correct_post_is_accepted_which_is_the_negative_control_for_the_csrf_checks(site: Website, harness: Control):
    """The same form, correctly posted, is accepted: the four refusals above are not vacuous."""
    email = f"csrf-control-{int(time.time())}@starling.example"
    response = site.post_form("/signup", {"email": email, "password": "a long enough passphrase"})
    assert response.status_code == 303
    assert harness.one("SELECT id FROM accounts WHERE email_norm = ?", (email.lower(),)) is not None


def test_negative_control_the_allowance_assertion_can_fail(accounts, harness: Control, plans: dict):
    """Change the account's credits through the operator path and journey 1's check must fail.

    Nothing in the product is edited: `usage.grants.debit_credits` is the owner dashboard's own
    action, and the assertion helper is the one the happy-path tests use.
    """
    account = accounts("signup", with_device=True)
    assert_free_plan_agrees(account, harness, plans)
    harness.debit_credits(account.id, 5, reason="negative control for journey 1")
    try:
        with pytest.raises(AssertionError):
            assert_free_plan_agrees(account, harness, plans)
    finally:
        _grant_back(harness, account.id, 5)
    assert_free_plan_agrees(account, harness, plans)


def _grant_back(harness: Control, account_id: str, amount: int) -> None:
    from e2e.harness.paths import add_server_to_path

    add_server_to_path()
    from starling_server.config import load_settings
    from starling_server.db import Database
    from starling_server.usage import grants

    settings = load_settings(harness._settings_env())
    db = Database(harness.db_path)
    try:
        grants.grant_credits(db, settings, account_id, amount, "e2e-harness", "undo the negative control")
    finally:
        db.close()
