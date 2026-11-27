"""Journey 6: the whole subscription life, driven through the fake Stripe's own pages.

Every change starts where a customer would start it - a button on `/account/billing`, then a
button on the hosted Checkout or Portal page - and lands through signed webhooks. What the
journey checks is what the customer sees afterwards: the plan, the period, the allowance and
whether drafting still works.

Periods are the awkward part of testing billing against a real clock. The gateway resolves the
current entitlement against the real time, so a renewal is only visible when the period
boundary really passes. `POST /_control/clock` puts the fake's clock where a subscription
created now ends a few seconds from now, and the two journeys that need a rollover wait for it.
"""
from __future__ import annotations

import time
import uuid

import pytest

from e2e.tests import billing_flow
from e2e.tests.client import Control, stream_body

DRAFT_COMPOSER = {
    "action": "post",
    "draft_text": "Our deploy script grew a rollback flag this week and nobody noticed it working.",
    "intent": "enhance",
}
PERIOD_SECONDS = 12  # how far ahead a short period ends; the rollover journeys wait this long


def price_id(harness: Control, plan_code: str) -> str:
    row = harness.one("SELECT price_id FROM price_map WHERE plan_code = ? AND active = 1", (plan_code,))
    assert row is not None, f"no price is mapped to {plan_code}"
    return str(row["price_id"])


def latest_charge(harness: Control, customer_id: str) -> str:
    charges = harness.stripe_get(f"/state?customer={customer_id}")["charges"]
    assert charges, "the customer has no charge yet"
    return str(charges[-1]["id"])


def wait_for_state(harness: Control, account, predicate, what: str, timeout_s: float = 60.0) -> dict:
    harness.wait_for_events_processed(timeout_s)
    return harness.wait_for(lambda: _state_if(account, predicate), timeout_s, what)


def _state_if(account, predicate) -> dict | None:
    state = account.device.session()
    return state if predicate(state) else None


def short_period_subscription(account, harness: Control, plan_code: str = "starter") -> dict:
    """A paid subscription whose current period ends `PERIOD_SECONDS` from now.

    The customer is created the ordinary way (a checkout the customer then leaves), and the
    subscription is started on the fake's moved clock, which is the one thing a hosted Checkout
    page cannot do: it refuses a session whose expiry is a month away from Stripe's clock.
    """
    url = billing_flow.start_checkout(account, plan_code)
    billing_flow.press(url, "checkout-leave", client_ip=account.site.client_ip)
    customer = billing_flow.customer_of(harness, account)
    harness.stripe("/clock", {"period_end_in_s": PERIOD_SECONDS})
    started = time.time()
    try:
        harness.stripe("/start_subscription", {"customer": customer, "price": price_id(harness, plan_code)})
    finally:
        harness.stripe("/clock", {"offset_s": 0})
    state = wait_for_state(harness, account, lambda s: s["plan"]["code"] == plan_code, f"the {plan_code} plan")
    assert state["plan"]["period_end"] >= int(started), state["plan"]
    return state


# --------------------------------------------------------------------------- checkout


def test_checkout_grants_the_plans_allowance_and_duplicates_change_nothing(
    ready_account, harness: Control, plans: dict
):
    account = ready_account("billing")
    # Every event is delivered twice and in a shuffled order: nothing may depend on either.
    harness.stripe("/delivery", {"duplicate": True, "shuffle_seed": 7})
    try:
        url = billing_flow.start_checkout(account, "starter")
        answer = billing_flow.press(url, "checkout-pay", client_ip=account.site.client_ip)
        assert answer.status_code == 303 and "/billing/return" in answer.headers["location"]
        state = billing_flow.wait_for_plan(account, harness, "starter")
    finally:
        harness.stripe("/delivery", {"duplicate": False, "shuffle_seed": None})

    assert state["plan"]["status"] == "active" and state["plan"]["interval"] == "month"
    assert state["usage"]["allowance"] == plans["starter"]["monthly_credits"]
    assert state["plan"]["payment_problem"] is False
    assert account.site.account_usage()["usage"]["allowance"] == plans["starter"]["monthly_credits"]

    delivered = harness.stripe_get("/deliveries")["deliveries"]
    types = [item["type"] for item in delivered]
    assert types.count("checkout.session.completed") >= 2, types
    assert types.count("invoice.paid") >= 2, types
    assert all(item["status"] == 200 for item in delivered), delivered

    # One allowance for one paid invoice, however many times the events arrived.
    periods = harness.rows(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'subscription'", (account.id,)
    )
    assert len(periods) == 1, periods
    events = harness.rows("SELECT status FROM billing_events WHERE account_id = ?", (account.id,))
    assert events and all(row["status"] in ("processed", "ignored") for row in events)

    drafted = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
    assert drafted.final is not None, drafted.events


def test_a_declined_card_leaves_the_account_on_free(ready_account, harness: Control):
    account = ready_account("declined")
    url = billing_flow.start_checkout(account, "starter")
    answer = billing_flow.press(url, "checkout-decline", client_ip=account.site.client_ip)
    assert answer.status_code == 303 and "/billing/return" in answer.headers["location"]
    harness.wait_for_events_processed(45.0)

    state = account.device.session()
    assert state["plan"]["code"] == "free", state["plan"]
    assert account.site.billing_page()["plan_code"] == "free"
    subscription = billing_flow.subscription_of(harness, account)
    assert subscription["status"] == "incomplete", subscription["status"]
    assert harness.rows(
        "SELECT id FROM entitlement_periods WHERE account_id = ? AND source = 'subscription'", (account.id,)
    ) == [], "an unpaid first period grants nothing"


def press_hosted_invoice(url: str, action: str, *, client_ip: str | None = None):
    """Press a button on the fake's hosted invoice page.

    The invoice page is not one of `billing_flow`'s Checkout or Portal buttons, so the press
    lives here. Like those, it is served over TLS with the run's self-signed certificate.
    """
    import httpx

    headers = {"X-Forwarded-For": client_ip} if client_ip else {}
    with httpx.Client(follow_redirects=False, timeout=60.0, trust_env=False, headers=headers, verify=False) as client:
        page = client.get(url)
        assert page.status_code == 200, page.text[:200]
        assert f'data-testid="{action}"' in page.text, f"the page has no {action} button"
        return client.post(f"{url.rstrip('/')}/{'pay' if action == 'invoice-pay' else 'decline'}")


def test_a_declined_card_can_be_retried_and_paid_without_a_second_subscription(
    ready_account, harness: Control, plans: dict
):
    """The customer whose first card was declined comes back and pays.

    Pressing "Choose Starter" again used to lead to the Customer Portal, which cannot pay an
    invoice that is already open, so the customer was stuck until Stripe expired the
    subscription about a day later. They are now sent to that invoice's own payment page.
    """
    account = ready_account("retry")
    checkout_url = billing_flow.start_checkout(account, "starter")
    billing_flow.press(checkout_url, "checkout-decline", client_ip=account.site.client_ip)
    harness.wait_for_events_processed(45.0)
    assert account.device.session()["plan"]["code"] == "free"

    again = account.site.post_form("/billing/checkout", {"plan_code": "starter"})
    assert again.status_code == 303, again.text[:300]
    invoice_url = again.headers["location"]
    assert "/i/" in invoice_url, f"a second attempt must lead to the unpaid invoice, not {invoice_url}"
    assert harness.rows(
        "SELECT id FROM entitlement_periods WHERE account_id = ? AND source = 'subscription'", (account.id,)
    ) == [], "nothing may be granted before the invoice is paid"

    answer = press_hosted_invoice(invoice_url, "invoice-pay", client_ip=account.site.client_ip)
    assert answer.status_code == 303
    state = billing_flow.wait_for_plan(account, harness, "starter")
    assert state["usage"]["allowance"] == plans["starter"]["monthly_credits"]
    assert state["plan"]["payment_problem"] is False

    subscriptions = harness.rows("SELECT subscription_id, status FROM subscriptions WHERE account_id = ?", (account.id,))
    assert len(subscriptions) == 1 and subscriptions[0]["status"] == "active", subscriptions
    periods = harness.rows(
        "SELECT granted_by_invoice_id FROM entitlement_periods WHERE account_id = ? AND source = 'subscription'",
        (account.id,),
    )
    assert len(periods) == 1 and periods[0]["granted_by_invoice_id"], periods
    sessions = harness.rows("SELECT session_id FROM checkout_sessions WHERE account_id = ?", (account.id,))
    assert len(sessions) == 1, "the retry must not open a second Checkout Session"

    drafted = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
    assert drafted.final is not None, drafted.events


def test_the_return_page_reports_the_finished_session(ready_account, harness: Control):
    account = ready_account("billing")
    session_id = harness.scalar(
        "SELECT session_id FROM checkout_sessions WHERE account_id = ? ORDER BY created_at DESC LIMIT 1",
        (account.id,),
    )
    page = account.site.get("/billing/return", params={"session_id": session_id})
    assert page.status_code == 200
    assert "Starter" in page.text or "subscription" in page.text.lower()


# --------------------------------------------------------------------------- changing plan


def test_upgrading_to_pro_prorates_and_takes_effect_when_the_invoice_is_paid(
    ready_account, harness: Control, plans: dict
):
    account = ready_account("billing")
    before = account.device.session()
    assert before["plan"]["code"] == "starter", "this journey runs after the checkout one"

    url = billing_flow.upgrade(account, "pro")
    answer = billing_flow.press(url, "portal-confirm-update", client_ip=account.site.client_ip)
    assert answer.status_code == 303
    state = billing_flow.wait_for_plan(account, harness, "pro")

    assert state["usage"]["allowance"] > plans["starter"]["monthly_credits"]
    assert state["usage"]["allowance"] <= plans["pro"]["monthly_credits"], (
        "an upgrade adds the difference for the remaining days, not a whole new month"
    )
    period = harness.one(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'subscription' "
        "ORDER BY period_start DESC LIMIT 1",
        (account.id,),
    )
    assert period["plan_code"] == "pro"
    assert period["cost_ceiling_micros"] > plans["starter"]["cost_ceiling_micros"]


def test_a_downgrade_is_scheduled_for_the_period_end_and_can_be_undone(ready_account, harness: Control):
    account = ready_account("billing")
    assert account.device.session()["plan"]["code"] == "pro"

    scheduled = account.site.post_form("/billing/downgrade", {"plan_code": "starter"})
    assert scheduled.status_code == 303 and "downgrade_scheduled" in scheduled.headers["location"]
    state = wait_for_state(
        harness, account, lambda s: s["plan"]["scheduled_change"] is not None, "the scheduled change"
    )
    change = state["plan"]["scheduled_change"]
    assert change["plan_code"] == "starter"
    assert change["effective_at"] == state["plan"]["period_end"]
    assert state["plan"]["code"] == "pro", "the customer keeps Pro until the period ends"
    assert "Scheduled change" in account.site.billing_page()["html"]

    undone = account.site.post_form("/billing/downgrade/undo", {})
    assert undone.status_code == 303 and "downgrade_undone" in undone.headers["location"]
    after = wait_for_state(
        harness, account, lambda s: s["plan"]["scheduled_change"] is None, "the scheduled change to be dropped"
    )
    assert after["plan"]["code"] == "pro"


def test_the_portal_opens_for_a_payment_method_update_and_for_cancelling(ready_account, harness: Control):
    account = ready_account("billing")
    for flow, button in (("payment_method_update", "portal-payment-method"), ("subscription_cancel", "portal-cancel")):
        url = billing_flow.portal(account, flow)
        page_actions_before = len(harness.stripe_get("/state")["page_actions"])
        answer = billing_flow.press(url, button, client_ip=account.site.client_ip)
        assert answer.status_code == 303
        assert "/account/billing" in answer.headers["location"]
        assert len(harness.stripe_get("/state")["page_actions"]) > page_actions_before
        if button == "portal-cancel":
            state = wait_for_state(harness, account, lambda s: s["plan"]["cancel_at"], "the cancellation")
            assert state["plan"]["cancel_at"] == state["plan"]["period_end"]
            assert "moves to Free" in account.site.billing_page()["html"]
            resume = billing_flow.press(billing_flow.portal(account), "portal-resume",
                                        client_ip=account.site.client_ip)
            assert resume.status_code == 303
            wait_for_state(harness, account, lambda s: not s["plan"]["cancel_at"], "the cancellation to be undone")


# --------------------------------------------------------------------------- renewals


def test_a_paid_renewal_opens_a_new_period_and_a_failed_one_goes_past_due(
    ready_account, harness: Control, plans: dict
):
    account = ready_account("renewal")
    state = short_period_subscription(account, harness)
    first_period_end = state["plan"]["period_end"]
    subscription = billing_flow.subscription_of(harness, account)["subscription_id"]

    # The period really has to end before the renewal means anything.
    time.sleep(max(0.0, first_period_end - time.time()) + 1.0)
    harness.stripe("/advance", {"subscription": subscription, "outcome": "paid"})
    renewed = wait_for_state(
        harness, account, lambda s: s["plan"]["period_end"] > first_period_end, "the next period"
    )
    assert renewed["plan"]["code"] == "starter" and renewed["plan"]["status"] == "active"
    assert renewed["usage"]["allowance"] == plans["starter"]["monthly_credits"]
    assert renewed["usage"]["used"] == 0, "a new period starts with the whole allowance"
    granted = harness.rows(
        "SELECT granted_by_invoice_id FROM entitlement_periods WHERE account_id = ? AND source = 'subscription'",
        (account.id,),
    )
    assert len(granted) == 2 and all(row["granted_by_invoice_id"] for row in granted)

    failed = harness.stripe("/advance", {"subscription": subscription, "outcome": "failed"})
    past_due = wait_for_state(harness, account, lambda s: s["plan"]["payment_problem"], "the payment problem")
    assert past_due["plan"]["status"] == "past_due"
    assert past_due["plan"]["code"] == "starter", "paid access continues during the grace period"
    assert "problem with your payment" in account.site.account_usage()["html"]
    still_drafting = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
    assert still_drafting.final is not None, still_drafting.events

    harness.stripe("/pay_invoice", {"invoice": failed["result"]["invoice"]})
    recovered = wait_for_state(harness, account, lambda s: not s["plan"]["payment_problem"], "the payment to recover")
    assert recovered["plan"]["status"] == "active"


def test_cancelling_at_the_period_end_keeps_the_plan_until_then_and_then_drops_to_free(
    ready_account, harness: Control
):
    account = ready_account("cancelled")
    state = short_period_subscription(account, harness)
    period_end = state["plan"]["period_end"]

    answer = billing_flow.press(billing_flow.portal(account, "subscription_cancel"), "portal-cancel",
                                client_ip=account.site.client_ip)
    assert answer.status_code == 303
    cancelling = wait_for_state(harness, account, lambda s: s["plan"]["cancel_at"], "the cancellation")
    assert cancelling["plan"]["cancel_at"] == period_end
    assert cancelling["plan"]["code"] == "starter", "the plan is kept until the period ends"

    time.sleep(max(0.0, period_end - time.time()) + 1.5)
    free = harness.wait_for(
        lambda: _state_if(account, lambda s: s["plan"]["code"] == "free"), 30.0, "the account to fall back to free"
    )
    assert free["plan"]["status"] == "free"
    assert free["usage"]["allowance"] == 25
    assert account.site.billing_page()["plan_code"] == "free"


# --------------------------------------------------------------------------- disputes and refunds


def test_an_open_dispute_suspends_the_account_and_closing_it_restores_access(ready_account, harness: Control):
    account = ready_account("disputed")
    billing_flow.subscribe(account, harness, "starter")
    customer = billing_flow.customer_of(harness, account)
    charge = latest_charge(harness, customer)

    opened = harness.stripe("/dispute", {"action": "open", "charge": charge})
    suspended = wait_for_state(harness, account, lambda s: s["plan"]["status"] == "suspended", "the suspension")
    assert suspended["plan"]["status"] == "suspended"

    refused = account.device.post("/assist/stream", stream_body("enhance_composer", composer=DRAFT_COMPOSER))
    refused.read()
    assert refused.status_code == 402
    assert refused.json()["error"]["code"] == "payment_required"
    assert "suspended" in account.site.account_usage()["html"].lower()

    harness.stripe("/dispute", {"action": "close", "dispute": opened["dispute"], "status": "won"})
    restored = wait_for_state(harness, account, lambda s: s["plan"]["status"] == "active", "access to be restored")
    assert restored["plan"]["code"] == "starter"
    drafted = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
    assert drafted.final is not None, drafted.events


def test_a_dispute_lost_cancels_the_subscription_and_keeps_the_account_suspended(
    ready_account, harness: Control
):
    account = ready_account("chargeback")
    billing_flow.subscribe(account, harness, "starter")
    customer = billing_flow.customer_of(harness, account)
    subscription = billing_flow.subscription_of(harness, account)["subscription_id"]
    charge = latest_charge(harness, customer)

    opened = harness.stripe("/dispute", {"action": "open", "charge": charge})
    wait_for_state(harness, account, lambda s: s["plan"]["status"] == "suspended", "the suspension")
    harness.stripe("/dispute", {"action": "close", "dispute": opened["dispute"], "status": "lost"})
    harness.wait_for_events_processed(45.0)

    mirrored = harness.wait_for(
        lambda: _cancelled(harness, subscription), 45.0, "the subscription to be cancelled"
    )
    assert mirrored["status"] in ("canceled", "incomplete_expired")
    assert account.device.session()["plan"]["status"] == "suspended", "a lost dispute keeps the suspension"
    alerts = harness.rows("SELECT key FROM service_state WHERE key LIKE 'alert%'")
    assert alerts, "a lost dispute raises an owner alert"


def _cancelled(harness: Control, subscription_id: str) -> dict | None:
    row = harness.one("SELECT status FROM subscriptions WHERE subscription_id = ?", (subscription_id,))
    return row if row and row["status"] in ("canceled", "incomplete_expired") else None


def test_a_full_refund_cancels_the_subscription(ready_account, harness: Control):
    account = ready_account("refunded")
    billing_flow.subscribe(account, harness, "starter")
    customer = billing_flow.customer_of(harness, account)
    charge = latest_charge(harness, customer)

    harness.stripe("/refund", {"charge": charge})
    free = wait_for_state(harness, account, lambda s: s["plan"]["code"] == "free", "the fall back to free")
    assert free["plan"]["status"] == "free"
    subscription = billing_flow.subscription_of(harness, account)
    assert subscription["status"] in ("canceled", "incomplete_expired"), subscription["status"]
    assert free["usage"]["allowance"] == 25


# --------------------------------------------------------------------------- negative control


def test_negative_control_an_undelivered_checkout_grants_nothing(ready_account, harness: Control):
    """With webhook delivery switched off, the same purchase leaves the account on free.

    This is the harness flag the billing assertions rest on: the plan only changes because the
    events really arrive and are really processed.
    """
    account = ready_account("undelivered")
    harness.stripe("/delivery", {"auto": False})
    try:
        url = billing_flow.start_checkout(account, "starter")
        answer = billing_flow.press(url, "checkout-pay", client_ip=account.site.client_ip)
        assert answer.status_code == 303
        pending = harness.stripe_get("/pending")["events"]
        assert [event["type"] for event in pending], "the fake still produced the events"

        with pytest.raises(AssertionError):
            billing_flow.wait_for_plan(account, harness, "starter", timeout_s=6)
        assert account.device.session()["plan"]["code"] == "free"
        assert account.site.billing_page()["plan_code"] == "free"
    finally:
        harness.stripe("/delivery", {"auto": True})

    # Delivering them now is the positive half of the control: the plan appears.
    harness.stripe("/deliver", {})
    state = billing_flow.wait_for_plan(account, harness, "starter")
    assert state["plan"]["status"] == "active"
