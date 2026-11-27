"""Billing actions: checkout (including 20 parallel attempts), portal, upgrade, downgrade, deletion."""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import replace

import pytest

from starling_server.billing import service, stripe_client, webhooks
from starling_server.billing.service import BillingError

from .conftest import DAY, WEBHOOK_SECRET, make_account, make_client


def entitling(fake, customer_id):
    return [s for s in fake.subscriptions.values() if s["customer"] == customer_id and s["status"] in ("active", "trialing", "past_due")]


def run_threads(count, target):
    threads = [threading.Thread(target=target) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_checkout_session_parameters_and_nothing_granted(billing):
    account = make_account(billing.db)
    result = billing.checkout(account, "pro")
    assert result.kind == "checkout" and result.url.startswith("https://checkout.stripe.com/")
    session = billing.fake.sessions[result.session_id]
    params = session["_params"]
    public = billing.settings.public_url
    assert params["mode"] == "subscription"
    assert params["line_items"] == [{"price": billing.price("pro"), "quantity": "1"}]
    assert params["client_reference_id"] == account and params["metadata"]["account_id"] == account
    assert params["subscription_data"] == {"metadata": {"account_id": account}}
    assert params["success_url"] == public + "/billing/return?session_id={CHECKOUT_SESSION_ID}"
    assert params["cancel_url"] == public + "/pricing?checkout=cancelled"  # the page then says nothing was charged
    assert 1800 <= int(params["expires_at"]) - billing.now <= 1800 + service.CHECKOUT_CLOCK_MARGIN_S
    assert params["payment_method_options"] == {"card": {"restrictions": {"funding_types_blocked": ["prepaid"]}}}
    assert params["allow_promotion_codes"] == "false" and "automatic_tax" not in params
    assert billing.fake.customers[session["customer"]]["metadata"] == {"account_id": account}
    assert billing.one("SELECT status FROM checkout_sessions WHERE session_id = ?", result.session_id)["status"] == "open"
    assert billing.all("SELECT * FROM entitlement_periods WHERE source = 'subscription'") == []


def test_checkout_options_follow_settings(db, configured, fake, stripe):
    settings = replace(configured, stripe_block_prepaid=False, stripe_automatic_tax=True, stripe_allow_promo_codes=True)
    result = service.create_checkout(db, settings, stripe, make_account(db), "starter", now=fake.now)
    params = fake.sessions[result.session_id]["_params"]
    assert "payment_method_options" not in params
    assert params["automatic_tax"] == {"enabled": "true"} and params["customer_update"] == {"address": "auto"}
    assert params["allow_promotion_codes"] == "true"


def test_checkout_refuses_unverified_accounts_and_unknown_plans(billing):
    with pytest.raises(BillingError) as caught:
        billing.checkout(make_account(billing.db, verified=False), "starter")
    assert caught.value.code == "email_unverified"
    account = make_account(billing.db)
    for plan_code in ("free", "enterprise", ""):
        with pytest.raises(BillingError):
            billing.checkout(account, plan_code)
    assert billing.fake.creates["checkout.session"] == 0 and billing.fake.creates["customer"] == 0


def test_twenty_parallel_checkouts_make_one_customer_and_one_subscription(billing):
    account = make_account(billing.db)
    completed: list[str] = []

    def pay_the_first(session):
        if not completed:
            completed.append(session["id"])
            billing.fake.complete_checkout(session["id"])

    billing.fake.on_session_created = pay_the_first
    results, errors = [], []

    def attempt():
        try:
            results.append(billing.checkout(account, "starter"))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    run_threads(20, attempt)
    assert errors == []
    assert Counter(result.kind for result in results) == {"checkout": 1, "portal": 19}
    assert billing.fake.creates["customer"] == 1
    customers = {session["customer"] for session in billing.fake.sessions.values()}
    assert len(customers) == 1 and len(entitling(billing.fake, customers.pop())) == 1
    assert all(r.url.startswith("https://billing.stripe.com/") for r in results if r.kind == "portal")


def test_parallel_unpaid_checkouts_leave_one_open_session(billing):
    account = make_account(billing.db)
    results = []
    run_threads(20, lambda: results.append(billing.checkout(account, "starter")))
    assert Counter(result.kind for result in results) == {"checkout": 20}
    assert billing.fake.creates["customer"] == 1
    assert Counter(s["status"] for s in billing.fake.sessions.values()) == {"open": 1, "expired": 19}
    assert Counter(row["status"] for row in billing.all("SELECT status FROM checkout_sessions")) == {"open": 1, "expired": 19}
    expired = next(s["id"] for s in billing.fake.sessions.values() if s["status"] == "expired")
    with pytest.raises(RuntimeError):
        billing.fake.complete_checkout(expired)


def test_existing_subscription_sends_checkout_to_the_management_portal(billing):
    account = make_account(billing.db)
    billing.subscribe(account)
    result = billing.checkout(account, "pro")
    portal = list(billing.fake.portal_sessions.values())[-1]
    assert result.kind == "portal" and result.url == portal["url"]
    assert portal["configuration"] == billing.settings.stripe_portal_configuration_id and portal["flow"] is None


def test_portal_flows_and_refusals(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    for flow in ("", "payment_method_update", "subscription_cancel"):
        service.create_portal(billing.db, billing.settings, billing.client, account, flow, now=billing.now)
    plain, payment, cancel = list(billing.fake.portal_sessions.values())[-3:]
    assert plain["flow"] is None
    assert payment["flow"]["type"] == "payment_method_update"
    assert cancel["flow"]["subscription_cancel"] == {"subscription": ids["subscription"]}
    assert {s["configuration"] for s in (plain, payment, cancel)} == {billing.settings.stripe_portal_configuration_id}
    with pytest.raises(BillingError):
        service.create_portal(billing.db, billing.settings, billing.client, account, "subscription_update", now=billing.now)
    with pytest.raises(BillingError) as caught:
        service.create_portal(billing.db, billing.settings, billing.client, make_account(billing.db), now=billing.now)
    assert caught.value.code == "no_billing_account"
    without_config = replace(billing.settings, stripe_portal_configuration_id="")
    with pytest.raises(BillingError) as caught:
        service.create_portal(billing.db, without_config, billing.client, account, now=billing.now)
    assert caught.value.code == "portal_unavailable"
    assert all(s["configuration"] != "bpc_dashboard_default" for s in billing.fake.portal_sessions.values())


def test_upgrade_waits_for_the_proration_payment_then_uplifts_once(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    item = billing.fake.subscriptions[ids["subscription"]]["items"]["data"][0]
    start, end = item["current_period_start"], item["current_period_end"]
    first_period = billing.entitlement(account).period_id
    billing.fake.advance(10 * DAY)
    url = service.start_upgrade(billing.db, billing.settings, billing.client, account, "pro", now=billing.now)
    portal = list(billing.fake.portal_sessions.values())[-1]
    assert url == portal["url"] and portal["configuration"] == billing.settings.stripe_portal_upgrade_configuration_id
    confirm = portal["flow"]["subscription_update_confirm"]
    assert confirm["items"] == [{"id": item["id"], "price": billing.price("pro"), "quantity": "1"}]

    upgrade = billing.fake.confirm_portal_upgrade(portal["id"], outcome="declined")
    billing.sync()
    unpaid = billing.entitlement(account)
    assert (unpaid.plan_code, unpaid.allowance, unpaid.status, unpaid.payment_problem) == ("starter", 250, "past_due", True)

    billing.fake.pay_invoice(upgrade["invoice"])
    billing.sync()
    remaining, length = end - (start + 10 * DAY), end - start
    expected = 250 + (800 - 250) * remaining // length
    paid = billing.entitlement(account)
    assert (paid.plan_code, paid.status, paid.allowance, paid.period_id) == ("pro", "active", expected, first_period)
    assert paid.cost_ceiling_micros == 4_500_000 + 4_500_000 * remaining // length

    # A second event for the same payment (Stripe can send one) changes nothing.
    original = next(e for e in billing.fake.events if e["type"] == "invoice.paid" and e["data"]["object"]["id"] == upgrade["invoice"])
    body, header = billing.fake.delivery({**original, "id": billing.fake.new_id("evt")}, WEBHOOK_SECRET)
    webhooks.intake(billing.db, billing.settings, body, header, billing.now)
    assert billing.process() == {"processed": 1}
    assert billing.entitlement(account).allowance == expected


def test_upgrade_with_a_pending_update_applies_on_payment(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    billing.fake.advance(3 * DAY)
    service.start_upgrade(billing.db, billing.settings, billing.client, account, "pro", now=billing.now)
    portal = list(billing.fake.portal_sessions.values())[-1]
    upgrade = billing.fake.confirm_portal_upgrade(portal["id"], outcome="declined", pending=True)
    billing.sync()
    assert billing.one("SELECT pending_update FROM subscriptions WHERE subscription_id = ?", ids["subscription"])["pending_update"] == 1
    assert (billing.entitlement(account).plan_code, billing.entitlement(account).status) == ("starter", "active")
    billing.fake.pay_invoice(upgrade["invoice"])
    billing.sync()
    assert billing.entitlement(account).plan_code == "pro"


def test_upgrade_refusals(billing):
    account = make_account(billing.db)
    with pytest.raises(BillingError) as caught:
        service.start_upgrade(billing.db, billing.settings, billing.client, account, "pro", now=billing.now)
    assert caught.value.code == "no_subscription"
    billing.subscribe(account, "pro")
    with pytest.raises(BillingError) as caught:
        service.start_upgrade(billing.db, billing.settings, billing.client, account, "starter", now=billing.now)
    assert caught.value.code == "not_an_upgrade"


def test_scheduled_downgrade_undo_and_period_end(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "pro")
    item = billing.fake.subscriptions[ids["subscription"]]["items"]["data"][0]
    scheduled = service.schedule_downgrade(billing.db, billing.settings, billing.client, account, "starter", now=billing.now)
    assert (scheduled["plan_code"], scheduled["effective_at"]) == ("starter", item["current_period_end"])
    schedule = billing.fake.schedules[scheduled["schedule_id"]]
    assert [phase["items"][0]["price"] for phase in schedule["phases"]] == [billing.price("pro"), billing.price("starter")]
    assert schedule["phases"][0]["end_date"] == item["current_period_end"] and schedule["end_behavior"] == "release"
    billing.sync()
    before = billing.entitlement(account)
    assert before.plan_code == "pro"
    assert before.scheduled_change == {"plan_code": "starter", "effective_at": item["current_period_end"]}

    service.undo_downgrade(billing.db, billing.settings, billing.client, account, now=billing.now)
    billing.sync()
    assert schedule["status"] == "released"
    undone = billing.entitlement(account)
    assert (undone.plan_code, undone.scheduled_change) == ("pro", None)
    with pytest.raises(BillingError) as caught:
        service.undo_downgrade(billing.db, billing.settings, billing.client, account, now=billing.now)
    assert caught.value.code == "no_scheduled_change"

    service.schedule_downgrade(billing.db, billing.settings, billing.client, account, "starter", now=billing.now)
    renewal = billing.fake.advance_period(ids["subscription"])
    billing.sync()
    after = billing.entitlement(account)
    assert (after.plan_code, after.allowance, after.scheduled_change) == ("starter", 250, None)
    granted = billing.one("SELECT plan_code FROM entitlement_periods WHERE granted_by_invoice_id = ?", renewal["invoice"])
    assert granted["plan_code"] == "starter"


def test_downgrade_refusals(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    for plan_code, code in (("pro", "not_a_downgrade"), ("starter", "not_a_downgrade"), ("free", "unknown_plan")):
        with pytest.raises(BillingError) as caught:
            service.schedule_downgrade(billing.db, billing.settings, billing.client, account, plan_code, now=billing.now)
        assert caught.value.code == code
    assert billing.fake.creates["subscription_schedule"] == 0


def test_cancel_for_deletion_cancels_immediately(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "pro")
    waiting = make_account(billing.db)
    open_session = billing.checkout(waiting, "starter").session_id
    previous = stripe_client.set_client_factory(lambda settings: make_client(settings, billing.fake))
    try:
        assert service.cancel_for_deletion(billing.db, billing.settings, account) == [ids["subscription"]]
        assert service.cancel_for_deletion(billing.db, billing.settings, waiting) == []
        requests = len(billing.fake.requests)
        assert service.cancel_for_deletion(billing.db, billing.settings, make_account(billing.db)) == []
        assert len(billing.fake.requests) == requests  # no customer: no Stripe call
    finally:
        stripe_client.set_client_factory(previous)
    subscription = billing.fake.subscriptions[ids["subscription"]]
    assert subscription["status"] == "canceled" and not subscription["cancel_at_period_end"]
    assert subscription["_cancel"] == {"prorate": False, "invoice_now": False}
    assert billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])["status"] == "canceled"
    assert billing.entitlement(account).plan_code == "free"
    assert billing.fake.sessions[open_session]["status"] == "expired"


def test_return_status_only_reads_the_accounts_own_session(billing):
    account, other = make_account(billing.db), make_account(billing.db)
    result = billing.checkout(account)

    def status(who, session_id, now=None):
        return service.return_session_status(billing.db, who, session_id, billing.now if now is None else now)

    assert status(account, result.session_id) == "pending"
    assert status(other, result.session_id) == "not_found"
    for forged in ("cs_test_forged", "../account", None, "x" * 300):
        assert status(account, forged) == "not_found"
    assert status(account, result.session_id, billing.now + 3 * 3600) == "expired"
    billing.fake.complete_checkout(result.session_id)
    billing.sync()
    assert status(account, result.session_id) == "complete"
