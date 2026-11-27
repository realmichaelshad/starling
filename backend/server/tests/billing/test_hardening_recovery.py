"""Adversarial recovery, account deletion and checkout construction.

Recovery has to be able to repair a day of lost webhooks without paying anyone twice, and
it has to survive a provider that fails halfway. Deletion has to stop the money before the
account disappears, and refuse to finish while it cannot.
"""
from __future__ import annotations

import threading
from dataclasses import replace

import anyio
import pytest

from fastapi.testclient import TestClient
from types import SimpleNamespace

from starling_server.app import create_app
from starling_server.auth import csrf, sessions
from starling_server.billing import entitlements, mirror, processor, reconcile, service, setup
from starling_server.billing.stripe_client import StripeError
from starling_server.config import load_settings
from starling_server.security import clock

from ..conftest import PUBLIC_URL
from .conftest import DAY, WEBHOOK_SECRET, billing_env, make_account, make_client
from .fake_stripe import START
from .test_hardening_processor import granted_invoices, periods


@pytest.fixture
def web_billing(tmp_path, fake, monkeypatch):
    """The billing routes over HTTP, signed in, with the fake Stripe behind them."""
    settings = load_settings(billing_env(tmp_path / "data", STARLING_WEB_DIR=str(tmp_path / "no-web")))
    monkeypatch.setattr(clock, "now", lambda: fake.now)
    app = create_app(settings)
    client = make_client(settings, fake)
    report = setup.run_setup(app.state.db, settings, client)
    assert not report.problems, report.problems
    settings = replace(
        settings,
        stripe_portal_configuration_id=report.env["STRIPE_PORTAL_CONFIGURATION_ID"],
        stripe_portal_upgrade_configuration_id=report.env["STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID"],
    )
    app.state.settings = settings
    app.state.stripe_client = client
    app.state.billing_background_jobs = False
    account = make_account(app.state.db)
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as http:
        cookie, session = app.state.db.write(lambda conn: sessions.create(
            conn, account_id=account, auth_epoch=1, now=fake.now, ip_prefix=None, user_agent="pytest"))
        http.cookies.set(settings.session_cookie_name, cookie)
        token = csrf.session_token(settings, session.csrf_secret)
        yield SimpleNamespace(app=app, http=http, settings=settings, fake=fake, db=app.state.db, client=client), token, account
    client.close()


# --------------------------------------------------------------------------- a day of missed webhooks


def test_a_day_of_lost_webhooks_is_repaired_without_granting_twice(billing):
    account = make_account(billing.db)
    result = billing.checkout(account)
    ids = billing.fake.complete_checkout(result.session_id)
    billing.fake.advance(30 * DAY)
    renewal = billing.fake.advance_period(ids["subscription"])
    # Every webhook of the purchase and the renewal was lost.
    billing.fake.take_events()
    assert billing.entitlement(account).plan_code == "free"
    recovered = reconcile.reconcile_events(billing.db, billing.settings, billing.client, now=billing.now)
    assert recovered > 0
    billing.process()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("starter", 250)
    assert sorted(granted_invoices(billing, account)) == sorted([ids["invoice"], renewal["invoice"]])
    # A second pass of both recovery jobs may not add anything.
    reconcile.reconcile_events(billing.db, billing.settings, billing.client, now=billing.now)
    billing.process()
    reconcile.full_resync(billing.db, billing.settings, billing.client, now=billing.now)
    assert sorted(granted_invoices(billing, account)) == sorted([ids["invoice"], renewal["invoice"]])
    assert billing.entitlement(account).allowance == 250


def test_the_event_reconciler_pages_through_more_than_one_page(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    subscription = billing.fake.subscriptions[ids["subscription"]]
    for _ in range(150):
        billing.fake.emit("customer.subscription.updated", subscription)
    billing.fake.take_events()  # all of them are "lost"
    recovered = reconcile.reconcile_events(billing.db, billing.settings, billing.client, now=billing.now)
    assert recovered >= 150, "pagination must not stop at the first page"
    stored = billing.one("SELECT COUNT(*) AS n FROM billing_events")["n"]
    assert stored >= 150
    billing.process()
    assert billing.entitlement(account).allowance == 250
    assert len(granted_invoices(billing, account)) == 1


def test_the_reconciler_stores_nothing_from_the_other_mode(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    billing.fake.take_events()
    for event in billing.fake.events:
        event["livemode"] = True
    recovered = reconcile.reconcile_events(billing.db, billing.settings, billing.client, now=billing.now)
    assert recovered == 0


# --------------------------------------------------------------------------- resync under provider failure


def test_a_resync_that_fails_at_stripe_reports_it_and_leaves_the_mirror_alone(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    before = billing.all("SELECT * FROM subscriptions")
    before_periods = periods(billing, account)
    # Three: the one call plus the client's two retries, so the next run meets a healthy Stripe.
    billing.fake.fail_next("GET", "/v1/subscriptions", status=500, times=3)
    result = reconcile.resync_account(billing.db, billing.settings, billing.client, account, now=billing.now)
    assert result == {"ok": False, "reason": "stripe_error", "message": result.get("message")}
    assert "StripeAPIError" in result["message"]
    assert billing.all("SELECT * FROM subscriptions") == before
    assert periods(billing, account) == before_periods
    assert billing.entitlement(account).allowance == 250
    # The next run, with Stripe healthy again, still changes nothing.
    healthy = reconcile.resync_account(billing.db, billing.settings, billing.client, account, now=billing.now)
    assert healthy["ok"] and not healthy["entitlement_changed"]
    assert granted_invoices(billing, account) == [ids["invoice"]]


def test_a_failing_customer_does_not_stop_the_daily_resync(billing):
    first = make_account(billing.db)
    second = make_account(billing.db)
    billing.subscribe(first, "starter")
    billing.subscribe(second, "pro")
    billing.fake.fail_next("GET", "/v1/subscriptions", status=500, times=6)
    summary = reconcile.full_resync(billing.db, billing.settings, billing.client, now=billing.now)
    assert summary["customers"] == 2 and summary["errors"] >= 1
    assert billing.entitlement(first).allowance in (250, 25) or billing.entitlement(first).plan_code == "starter"
    assert billing.entitlement(second).plan_code in ("pro", "free")
    healthy = reconcile.full_resync(billing.db, billing.settings, billing.client, now=billing.now)
    assert healthy["errors"] == 0
    assert billing.entitlement(first).allowance == 250
    assert billing.entitlement(second).allowance == 800


def test_a_connection_failure_is_retried_and_then_raised_without_half_writing(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    before = billing.all("SELECT * FROM subscriptions")
    billing.fake.fail_next("GET", "/v1/invoices", status=503, times=50)
    result = reconcile.resync_account(billing.db, billing.settings, billing.client, account, now=billing.now)
    assert result["ok"] is False and result["reason"] == "stripe_error"
    assert billing.all("SELECT * FROM subscriptions") == before
    assert len(granted_invoices(billing, account)) == 1


def test_a_resync_never_resurrects_a_deleted_accounts_subscription(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    service.cancel_for_deletion(billing.db, billing.settings, account, client=billing.client, now=billing.now)
    billing.db.write(lambda conn: mirror.tombstone_customer(conn, account, ids["customer"], billing.now))
    billing.sync()
    assert billing.entitlement(account).plan_code == "free"
    # Stripe still lists the (now canceled) subscription; a resync may not bring it back.
    result = reconcile.resync_account(billing.db, billing.settings, billing.client, account, now=billing.now)
    assert result == {"ok": False, "reason": "no_customer"}
    summary = reconcile.full_resync(billing.db, billing.settings, billing.client, now=billing.now)
    assert summary["customers"] == 0
    assert billing.entitlement(account).plan_code == "free"
    assert billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])["status"] == "canceled"


def test_a_resync_recovers_a_missed_grant_exactly_once_when_run_concurrently(billing):
    account = make_account(billing.db)
    result = billing.checkout(account)
    ids = billing.fake.complete_checkout(result.session_id)
    billing.fake.take_events()  # the grant was never delivered
    assert billing.entitlement(account).plan_code == "free"
    outcomes: list[dict] = []
    barrier = threading.Barrier(5)

    def run():
        barrier.wait()
        outcomes.append(reconcile.resync_account(billing.db, billing.settings, billing.client, account, now=billing.now))

    threads = [threading.Thread(target=run) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(result["ok"] for result in outcomes)
    assert granted_invoices(billing, account) == [ids["invoice"]]
    assert billing.entitlement(account).allowance == 250
    assert sum(result["periods_granted"] for result in outcomes) == 1


# --------------------------------------------------------------------------- deletion


def test_deleting_an_account_cancels_the_subscription_so_nothing_can_be_charged_again(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    canceled = service.cancel_for_deletion(billing.db, billing.settings, account, client=billing.client, now=billing.now)
    assert canceled == [ids["subscription"]]
    assert billing.fake.subscriptions[ids["subscription"]]["status"] == "canceled"
    assert billing.one("SELECT status FROM subscriptions")["status"] == "canceled"
    assert billing.entitlement(account).plan_code == "free"
    # Cancelled without proration and without a final invoice, so no further charge follows.
    assert billing.fake.subscriptions[ids["subscription"]]["_cancel"] == {"prorate": False, "invoice_now": False}
    assert billing.fake.subscriptions[ids["subscription"]]["ended_at"] is not None


def test_deletion_raises_when_stripe_fails_so_the_account_is_not_finished(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    billing.fake.fail_next("DELETE", "/v1/subscriptions", status=500, times=3)  # one call plus its retries
    with pytest.raises(StripeError):
        service.cancel_for_deletion(billing.db, billing.settings, account, client=billing.client, now=billing.now)
    assert billing.fake.subscriptions[ids["subscription"]]["status"] == "active", "Stripe still has a live subscription"
    # The retry that the deletion resume job performs must finish the job.
    again = service.cancel_for_deletion(billing.db, billing.settings, account, client=billing.client, now=billing.now)
    assert again == [ids["subscription"]]
    assert billing.fake.subscriptions[ids["subscription"]]["status"] == "canceled"


def test_deleting_twice_is_harmless(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    first = service.cancel_for_deletion(billing.db, billing.settings, account, client=billing.client, now=billing.now)
    second = service.cancel_for_deletion(billing.db, billing.settings, account, client=billing.client, now=billing.now)
    assert first and second == []
    assert billing.entitlement(account).plan_code == "free"


def test_deletion_expires_an_open_checkout_session(billing):
    account = make_account(billing.db)
    result = billing.checkout(account)
    service.cancel_for_deletion(billing.db, billing.settings, account, client=billing.client, now=billing.now)
    assert billing.fake.sessions[result.session_id]["status"] == "expired"
    assert billing.one("SELECT status FROM checkout_sessions WHERE session_id = ?", result.session_id)["status"] == "expired"
    with pytest.raises(RuntimeError):
        billing.fake.complete_checkout(result.session_id)


def test_the_async_wrapper_cancels_too(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    from starling_server.billing.stripe_client import set_client_factory

    previous = set_client_factory(lambda settings: billing.client)
    try:
        canceled = anyio.run(service.cancel_for_deletion_async, billing.db, billing.settings, account)
    finally:
        set_client_factory(previous)
    assert canceled == [ids["subscription"]]
    assert billing.fake.subscriptions[ids["subscription"]]["status"] == "canceled"


# --------------------------------------------------------------------------- checkout construction


def test_checkout_urls_come_from_the_configured_origin_only(billing):
    account = make_account(billing.db)
    result = billing.checkout(account)
    params = billing.fake.sessions[result.session_id]["_params"]
    public = billing.settings.public_url
    assert params["success_url"].startswith(f"{public}/billing/return")
    assert params["cancel_url"] == f"{public}/pricing?checkout=cancelled"
    assert "{CHECKOUT_SESSION_ID}" in params["success_url"]
    assert params["client_reference_id"] == account


def test_a_forged_host_header_cannot_move_the_return_url(web_billing):
    """The success and cancel URLs must not follow the request's Host or X-Forwarded-Host."""
    web, token, account = web_billing
    response = web.http.post(
        "/billing/checkout",
        data={"csrf_token": token, "plan_code": "starter"},
        headers={"origin": web.settings.public_url, "host": "attacker.example",
                 "x-forwarded-host": "attacker.example", "x-forwarded-proto": "http"},
    )
    assert response.status_code in (303, 403)
    for session in web.fake.sessions.values():
        params = session["_params"]
        assert params["success_url"].startswith(web.settings.public_url), params["success_url"]
        assert "attacker.example" not in params["success_url"] + params["cancel_url"]


def test_the_portal_return_url_comes_from_the_configured_origin(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    url = service.create_portal(billing.db, billing.settings, billing.client, account, now=billing.now)
    session = next(s for s in billing.fake.portal_sessions.values() if s["url"] == url)
    # The way back also says where the customer went, so the page can refresh and say what changed.
    assert session["return_url"] == f"{billing.settings.public_url}/account/billing?portal=home"
    assert session["configuration"] == billing.settings.stripe_portal_configuration_id


def test_a_price_with_the_right_lookup_key_but_the_wrong_amount_is_never_sold(billing):
    account = make_account(billing.db)
    plan = billing.settings.plans.get("starter")
    # Someone edits Stripe by hand and the lookup key moves to a cheaper price.
    billing.db.write(lambda conn: conn.execute("DELETE FROM price_map"))
    for price in list(billing.fake.prices.values()):
        if price.get("lookup_key") == plan.stripe_lookup_key:
            price["lookup_key"] = None
    billing.fake.add_price(unit_amount=100, currency="usd", lookup_key=plan.stripe_lookup_key)
    with pytest.raises(service.BillingError) as raised:
        billing.checkout(account)
    assert raised.value.code == "plan_unavailable"
    assert billing.alerts("price_missing")
    assert billing.all("SELECT * FROM checkout_sessions") == []


def test_a_price_from_the_other_mode_is_not_accepted(billing):
    account = make_account(billing.db)
    billing.db.write(lambda conn: conn.execute("UPDATE price_map SET livemode = 1"))
    resolved = processor.resolve_plan(billing.db, billing.settings, billing.client, billing.price("starter"))
    assert resolved is None
    with billing.db.read() as conn:
        assert mirror.plan_for_price(conn, billing.settings, billing.price("starter")) is None
    assert entitlements.current_entitlement(billing.db, billing.settings, account, billing.now).plan_code == "free"


def test_the_customer_is_created_once_even_when_checkouts_race(billing):
    account = make_account(billing.db)
    errors: list[Exception] = []
    barrier = threading.Barrier(12)

    def run():
        barrier.wait()
        try:
            billing.checkout(account)
        except Exception as exc:  # collected so the assertion can name it
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert billing.fake.creates["customer"] == 1
    assert len(billing.all("SELECT * FROM stripe_customers WHERE account_id = ?", account)) == 1
    open_sessions = billing.all("SELECT * FROM checkout_sessions WHERE account_id = ? AND status = 'open'", account)
    assert len(open_sessions) == 1, "only the newest session may stay open"


def test_the_customer_is_created_with_a_stable_idempotency_key(billing, monkeypatch):
    account = make_account(billing.db)
    seen: list[str] = []
    original = billing.client.create_customer

    def spy(params, *, idempotency_key):
        seen.append(idempotency_key)
        return original(params, idempotency_key=idempotency_key)

    monkeypatch.setattr(billing.client, "create_customer", spy)
    service.ensure_customer(billing.db, billing.settings, billing.client, account, billing.now)
    billing.db.write(lambda conn: conn.execute("DELETE FROM stripe_customers WHERE account_id = ?", (account,)))
    service.ensure_customer(billing.db, billing.settings, billing.client, account, billing.now)
    assert seen == [f"cust-{account}", f"cust-{account}"]
    assert billing.fake.creates["customer"] == 1, "the same key must replay, not create a second customer"


def test_a_customer_deleted_at_stripe_is_replaced_even_when_the_webhook_is_lost(billing):
    """The owner deletes the customer in the Dashboard and the event never arrives.

    Without noticing, every later Checkout Session would be refused with "no such customer"
    and the account could never buy anything again.
    """
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    billing.fake.delete_customer(ids["customer"])
    billing.fake.take_events()  # the customer.deleted webhook is lost
    assert billing.one("SELECT deleted_at FROM stripe_customers WHERE account_id = ?", account)["deleted_at"] is None
    result = billing.checkout(account)
    assert result.kind == "checkout"
    fresh = billing.one("SELECT customer_id, deleted_at FROM stripe_customers WHERE account_id = ?", account)
    assert fresh["customer_id"] != ids["customer"] and fresh["deleted_at"] is None
    assert billing.fake.sessions[result.session_id]["customer"] == fresh["customer_id"]
    # The old customer is tombstoned, so its late events stay ignored.
    with billing.db.read() as conn:
        assert mirror.is_tombstoned(conn, ids["customer"])


def test_two_stripe_side_deletions_in_the_same_second_still_give_a_usable_customer(billing):
    """The replacement key must not collide when the clock has not moved on."""
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    for _ in range(3):
        current = billing.one("SELECT customer_id FROM stripe_customers WHERE account_id = ?", account)["customer_id"]
        billing.fake.delete_customer(current)
        billing.fake.take_events()  # every customer.deleted webhook is lost
        result = billing.checkout(account)
        assert result.kind == "checkout", result
        used = billing.fake.sessions[result.session_id]["customer"]
        assert not billing.fake.customers[used].get("_deleted"), "checkout was given a deleted customer"
        assert used != current


def test_a_second_purchase_is_sent_to_the_portal_instead(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    result = billing.checkout(account)
    assert result.kind == "portal" and result.url.startswith("https://billing.stripe.com/")
    assert len(billing.all("SELECT * FROM subscriptions WHERE account_id = ?", account)) == 1
    assert billing.entitlement(account).allowance == 250


@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "incomplete", "paused"])
def test_every_blocking_status_refuses_a_second_subscription(billing, status):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    billing.fake.subscriptions[ids["subscription"]]["status"] = status
    result = billing.checkout(account)
    assert result.kind != "checkout", f"{status} must not allow a second subscription"
    assert len(billing.all("SELECT * FROM subscriptions WHERE account_id = ?", account)) == 1
    assert billing.fake.creates["checkout.session"] == 1  # only the original one


def test_provider_failures_reach_the_customer_as_a_plain_message(billing):
    account = make_account(billing.db)
    billing.fake.fail_next("POST", "/v1/checkout/sessions", status=500, times=10)
    with pytest.raises(StripeError) as raised:
        billing.checkout(account)
    assert "injected failure" in str(raised.value)  # the raw text exists only inside the exception
    short = reconcile._short_error(raised.value)
    assert "injected failure" not in short and "StripeAPIError" in short


def test_a_declined_card_never_grants_and_a_later_payment_does(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter", outcome="declined")
    assert billing.entitlement(account).plan_code == "free"
    assert granted_invoices(billing, account) == []
    billing.fake.pay_invoice(ids["invoice"])
    billing.sync()
    assert billing.entitlement(account).allowance == 250
    assert granted_invoices(billing, account) == [ids["invoice"]]
