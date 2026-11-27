"""Billing routes through the real session and CSRF dependencies (server core `auth.deps`)."""
from __future__ import annotations

import importlib.util
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.templating import Jinja2Templates

from starling_server.app import create_app
from starling_server.auth import csrf, sessions
from starling_server.billing import entitlements, processor, setup, webhooks
from starling_server.config import REPO_ROOT, load_settings
from starling_server.security import clock
from starling_server.web import flash

from ..conftest import PUBLIC_URL
from .conftest import WEBHOOK_SECRET, billing_env, make_account, make_client


@pytest.fixture
def web(tmp_path, fake, monkeypatch):
    settings = load_settings(billing_env(tmp_path / "data", STARLING_WEB_DIR=str(tmp_path / "no-web")))
    monkeypatch.setattr(clock, "now", lambda: fake.now)
    app = create_app(settings)
    client = make_client(settings, fake)
    report = setup.run_setup(app.state.db, settings, client)
    assert not report.problems
    settings = replace(
        settings,
        stripe_portal_configuration_id=report.env["STRIPE_PORTAL_CONFIGURATION_ID"],
        stripe_portal_upgrade_configuration_id=report.env["STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID"],
    )
    app.state.settings = settings
    app.state.stripe_client = client
    app.state.billing_background_jobs = False
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as http:
        yield SimpleNamespace(app=app, http=http, settings=settings, fake=fake, db=app.state.db, client=client)
    client.close()


def sign_in(web, account_id: str) -> str:
    cookie, session = web.db.write(lambda conn: sessions.create(
        conn, account_id=account_id, auth_epoch=1, now=web.fake.now, ip_prefix=None, user_agent="pytest"))
    web.http.cookies.set(web.settings.session_cookie_name, cookie)
    return csrf.session_token(web.settings, session.csrf_secret)


def post(web, path: str, token: str | None, **fields: str):
    data = dict(fields)
    if token is not None:
        data["csrf_token"] = token
    return web.http.post(path, data=data, headers={"origin": PUBLIC_URL})


def said(web, response) -> list[dict[str, str]]:
    """The flash message a redirect carries, read the way the next page reads it."""
    name = flash.flash_cookie_name(web.settings)
    request = SimpleNamespace(cookies={name: response.cookies.get(name)})
    return flash.read_flash(request, web.settings)


def deliver_all(web) -> None:
    for body, header in web.fake.deliveries([WEBHOOK_SECRET]):
        webhooks.intake(web.db, web.settings, body, header, web.fake.now)
    processor.process_pending(web.db, web.settings, web.client, web.fake.now)


def session_id_from(location: str) -> str:
    return location.rstrip("/").rsplit("/", 1)[-1]


def test_checkout_redirects_to_stripe(web):
    token = sign_in(web, make_account(web.db))
    response = post(web, "/billing/checkout", token, plan_code="starter")
    assert response.status_code == 303 and response.headers["location"].startswith("https://checkout.stripe.com/")
    assert response.headers["cache-control"] == "no-store"


def test_csrf_sign_in_and_verified_email_are_required(web):
    account = make_account(web.db)
    token = sign_in(web, account)
    before = len(web.fake.requests)
    assert web.http.post("/billing/checkout", data={"plan_code": "starter"}, headers={"origin": PUBLIC_URL}).status_code == 403
    cross_site = web.http.post("/billing/checkout", data={"csrf_token": token, "plan_code": "starter"},
                               headers={"origin": "https://attacker.example"})
    assert cross_site.status_code == 403
    web.http.cookies.clear()
    assert post(web, "/billing/portal", token).status_code in (303, 403)
    unverified_token = sign_in(web, make_account(web.db, verified=False))
    assert post(web, "/billing/checkout", unverified_token, plan_code="starter").status_code == 403
    assert len(web.fake.requests) == before  # no Stripe call for any refused request


def test_unknown_plan_returns_to_pricing_with_an_error(web):
    token = sign_in(web, make_account(web.db))
    response = post(web, "/billing/checkout", token, plan_code="gold")
    assert (response.status_code, response.headers["location"]) == (303, "/pricing?billing_error=unknown_plan")


def test_portal_flows_upgrade_downgrade_and_undo(web):
    token = sign_in(web, make_account(web.db))
    checkout = post(web, "/billing/checkout", token, plan_code="pro")
    web.fake.complete_checkout(session_id_from(checkout.headers["location"]))
    deliver_all(web)
    for flow in ("", "payment_method_update", "subscription_cancel"):
        response = post(web, "/billing/portal", token, flow=flow)
        assert response.status_code == 303 and response.headers["location"].startswith("https://billing.stripe.com/")
    flows = [s["flow"]["type"] if s["flow"] else None for s in list(web.fake.portal_sessions.values())[-3:]]
    assert flows == [None, "payment_method_update", "subscription_cancel"]
    assert post(web, "/billing/portal", token, flow="subscription_update").headers["location"] == "/account/billing?billing_error=bad_request"

    assert post(web, "/billing/upgrade", token, plan_code="starter").headers["location"] == "/account/billing?billing_error=not_an_upgrade"
    scheduled = post(web, "/billing/downgrade", token, plan_code="starter")
    assert scheduled.headers["location"] == "/account/billing?billing=downgrade_scheduled"
    # Nothing visible changes for weeks, and an undo makes a panel vanish: both say what happened.
    assert said(web, scheduled) == [{"level": "success", "text": "Your downgrade is scheduled. You keep your current plan until the end of this period."}]
    undone = post(web, "/billing/downgrade/undo", token)
    assert undone.headers["location"] == "/account/billing?billing=downgrade_undone"
    assert said(web, undone) == [{"level": "success", "text": "The scheduled change is cancelled. Your plan stays as it is."}]


def test_forged_return_grants_nothing_and_own_session_polls(web):
    account = make_account(web.db)
    token = sign_in(web, account)
    requests = len(web.fake.requests)
    forged = web.http.get("/billing/return", params={"session_id": "cs_test_forged"})
    assert forged.status_code == 200 and "could not find" in forged.text
    assert len(web.fake.requests) == requests
    with web.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkout_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM entitlement_periods WHERE source = 'subscription'").fetchone()[0] == 0
    assert entitlements.current_entitlement(web.db, web.settings, account, web.fake.now).plan_code == "free"

    checkout = post(web, "/billing/checkout", token, plan_code="starter")
    session_id = session_id_from(checkout.headers["location"])
    waiting = web.http.get("/billing/return", params={"session_id": session_id})
    assert "waiting for Stripe" in waiting.text and 'http-equiv="refresh"' in waiting.text
    web.fake.complete_checkout(session_id)
    assert "waiting for Stripe" in web.http.get("/billing/return", params={"session_id": session_id}).text  # nothing granted yet
    deliver_all(web)
    done = web.http.get("/billing/return", params={"session_id": session_id})
    assert "You are on Starter" in done.text and "refresh" not in done.text


def test_status_json_for_polling(web):
    signed_out = web.http.get("/billing/status", headers={"accept": "application/json"})
    assert signed_out.status_code == 401 and signed_out.json()["error"]["code"] == "signed_out"
    sign_in(web, make_account(web.db))
    response = web.http.get("/billing/status", headers={"accept": "application/json"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    plan = response.json()["state"]["plan"]
    assert (plan["code"], plan["status"]) == ("free", "free")


def test_return_template_renders_with_the_website_environment(web):
    module_path = REPO_ROOT / "web" / "templating.py"
    if not (REPO_ROOT / "web" / "templates" / "billing" / "return.html").is_file():
        pytest.skip("website templates are not installed")
    spec = importlib.util.spec_from_file_location("starling_web_templating", module_path)
    templating = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(templating)
    web.app.state.templates = Jinja2Templates(env=templating.create_environment(REPO_ROOT / "web"))
    token = sign_in(web, make_account(web.db))
    checkout = post(web, "/billing/checkout", token, plan_code="starter")
    session_id = session_id_from(checkout.headers["location"])
    pending = web.http.get("/billing/return", params={"session_id": session_id})
    assert pending.status_code == 200 and "data-billing-poll" in pending.text
    forged = web.http.get("/billing/return", params={"session_id": "cs_test_forged"})
    assert forged.status_code == 200 and "could not find that checkout" in forged.text


def test_keeping_a_plan_that_is_set_to_end_takes_one_click_and_no_trip_to_stripe(web):
    token = sign_in(web, make_account(web.db))
    checkout = post(web, "/billing/checkout", token, plan_code="starter")
    web.fake.complete_checkout(session_id_from(checkout.headers["location"]))
    deliver_all(web)
    subscription_id = next(iter(web.fake.subscriptions))
    web.fake.set_cancel_at_period_end(subscription_id, True)
    deliver_all(web)
    portals = len(web.fake.portal_sessions)
    kept = post(web, "/billing/resume", token)
    assert kept.status_code == 303 and kept.headers["location"] == "/account/billing?billing=resumed"
    assert said(web, kept)[0]["text"].startswith("Starter is back on.")
    assert web.fake.subscriptions[subscription_id]["cancel_at_period_end"] is False
    assert len(web.fake.portal_sessions) == portals  # never sent to Stripe's pages for it
    again = post(web, "/billing/resume", token)
    assert again.headers["location"] == "/account/billing?billing_error=not_cancelling"


def test_back_from_the_portal_the_page_reads_stripe_before_the_webhook(web):
    token = sign_in(web, make_account(web.db))
    checkout = post(web, "/billing/checkout", token, plan_code="pro")
    web.fake.complete_checkout(session_id_from(checkout.headers["location"]))
    deliver_all(web)
    subscription_id = next(iter(web.fake.subscriptions))
    web.fake.set_cancel_at_period_end(subscription_id, True)  # cancelled on Stripe's page; no webhook yet

    def cancelling() -> int:
        with web.db.read() as conn:
            return conn.execute(
                "SELECT cancel_at_period_end FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()[0]

    # This fixture has no website templates, so the page itself does not render here (the core
    # suite renders it); what matters is that Stripe is read before the page is built.
    assert cancelling() == 0
    web.http.get("/account/billing")
    assert cancelling() == 0  # an ordinary visit does not read Stripe
    web.http.get("/account/billing?portal=cancel")
    assert cancelling() == 1  # the page the customer lands on already knows


def test_coming_back_from_stripe_reads_stripe_a_few_times_at_most(web, monkeypatch):
    """A page on another site can load the return link again and again in a signed-in browser."""
    from starling_server.billing import service as billing_service
    from starling_server.security import ratelimit

    calls = []
    monkeypatch.setattr(billing_service, "refresh_after_stripe", lambda *args: calls.append(args[-1]))
    account_id = make_account(web.db)
    sign_in(web, account_id)
    for _ in range(ratelimit.BILLING_REFRESH_PER_ACCOUNT.limit + 3):
        web.http.get("/account/billing?portal=home")  # no templates in this fixture: the read is what counts
    assert calls == [account_id] * ratelimit.BILLING_REFRESH_PER_ACCOUNT.limit
    web.fake.now += ratelimit.BILLING_REFRESH_PER_ACCOUNT.window_s + 1
    web.http.get("/account/billing?portal=home")
    assert len(calls) == ratelimit.BILLING_REFRESH_PER_ACCOUNT.limit + 1  # control: the window passes


def test_every_way_back_from_stripe_says_where_the_customer_went(web):
    token = sign_in(web, make_account(web.db))
    checkout = post(web, "/billing/checkout", token, plan_code="pro")
    web.fake.complete_checkout(session_id_from(checkout.headers["location"]))
    deliver_all(web)
    for flow, back in (("", "home"), ("payment_method_update", "card"), ("subscription_cancel", "cancel")):
        post(web, "/billing/portal", token, flow=flow)
        session = list(web.fake.portal_sessions.values())[-1]
        assert session["return_url"].endswith(f"/account/billing?portal={back}")
