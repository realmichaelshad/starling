"""Adversarial event processing: ordering, duplicates, odd invoices, refunds and disputes.

Every test here drives the fake Stripe the way Stripe itself would and then checks the
money-safety invariants: one grant per paid invoice, no allowance without a payment, no
payment without either access or a visible owner alert, and no state that an older event
can roll back.
"""
from __future__ import annotations

import threading

import pytest

from starling_server.billing import entitlements, processor, reconcile, service, webhooks
from starling_server.billing.entitlements import add_months

from .conftest import DAY, WEBHOOK_SECRET, make_account
from .fake_stripe import START


def periods(billing, account):
    return billing.all(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'subscription' ORDER BY period_start, rowid",
        account,
    )


def granted_invoices(billing, account):
    return [row["granted_by_invoice_id"] for row in periods(billing, account) if row["granted_by_invoice_id"]]


def event_statuses(billing):
    return {row["event_id"]: row["status"] for row in billing.all("SELECT event_id, status FROM billing_events")}


def deliver_events(billing, events):
    for event in events:
        body, header = billing.fake.delivery(event, WEBHOOK_SECRET)
        assert webhooks.intake(billing.db, billing.settings, body, header, billing.now).status_code == 200


# --------------------------------------------------------------------------- ordering


def test_every_order_of_a_first_purchase_reaches_the_same_state(billing):
    """The five events of a purchase, delivered in reverse, must converge on one paid period."""
    account = make_account(billing.db)
    billing.fake.complete_checkout(billing.checkout(account).session_id)
    events = billing.fake.take_events()
    deliver_events(billing, list(reversed(events)))
    billing.process()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("starter", 250)
    assert len(granted_invoices(billing, account)) == 1


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 13])
def test_shuffled_and_duplicated_deliveries_converge(billing, seed):
    account = make_account(billing.db)
    billing.fake.complete_checkout(billing.checkout(account).session_id)
    billing.sync(shuffle_seed=seed, duplicate=True)
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("starter", 250)
    assert len(granted_invoices(billing, account)) == 1


def test_a_stale_update_after_a_cancellation_never_revives_the_subscription(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    assert billing.entitlement(account).plan_code == "starter"
    # Capture an "active" update, then cancel at Stripe, then deliver the old event last.
    billing.fake.set_cancel_at_period_end(ids["subscription"], False)
    stale = billing.fake.take_events()
    billing.fake.cancel_now(ids["subscription"])
    billing.sync()
    assert billing.entitlement(account).plan_code == "free"
    deliver_events(billing, stale)
    billing.process()
    row = billing.one("SELECT status, ended_at FROM subscriptions WHERE subscription_id = ?", ids["subscription"])
    assert row["status"] == "canceled" and row["ended_at"] is not None
    assert billing.entitlement(account).plan_code == "free"


def test_an_old_payment_failure_replayed_after_recovery_does_not_reopen_the_grace(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    failed = billing.fake.advance_period(ids["subscription"], outcome="failed")
    billing.sync()
    assert billing.entitlement(account).status == "past_due"
    stale = [event for event in billing.fake.events if event["type"] == "invoice.payment_failed"]
    billing.fake.pay_invoice(failed["invoice"])
    billing.sync()
    assert billing.entitlement(account).status == "active"
    # Re-deliver the old failure event under a new id, as a redrive would.
    replay = [{**event, "id": billing.fake.new_id("evt")} for event in stale]
    deliver_events(billing, replay)
    billing.process()
    after = billing.entitlement(account)
    assert (after.status, after.allowance) == ("active", 250)
    assert billing.one("SELECT first_payment_failed_at FROM subscriptions")["first_payment_failed_at"] is None


def test_the_grace_window_starts_at_the_first_failure_this_service_learned_of(billing):
    """Dunning sends several failures; the one processed first fixes the grace window.

    Delivered in order this is the true first failure. Out of order (an early event dropped
    and recovered later) the window starts at the later one, so the customer keeps paid
    access a little longer than the contract's seven days from the first failure. It errs
    towards the customer and never grants an allowance, so it is pinned here, not changed.
    """
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    first = billing.fake.advance_period(ids["subscription"], outcome="failed")
    early = [event for event in billing.fake.take_events() if event["type"] == "invoice.payment_failed"]
    assert early
    billing.fake.advance(4 * DAY)
    billing.fake.emit("invoice.payment_failed", billing.fake.invoices[first["invoice"]])
    later_at = billing.fake.now
    billing.sync()  # the later failure arrives first
    assert billing.one("SELECT first_payment_failed_at FROM subscriptions")["first_payment_failed_at"] == later_at
    deliver_events(billing, early)  # the dropped early one is recovered afterwards
    billing.process()
    recorded = billing.one("SELECT first_payment_failed_at FROM subscriptions")["first_payment_failed_at"]
    assert recorded == later_at, "the first failure processed keeps the window"
    grace = billing.settings.past_due_grace_days * DAY
    assert billing.entitlement(account, now=later_at + grace - 1).status == "past_due"
    assert billing.entitlement(account, now=later_at + grace).plan_code == "free"


def test_a_renewal_paid_late_carries_the_gap_usage_into_the_paid_period(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    failed = billing.fake.advance_period(ids["subscription"], outcome="failed")
    billing.sync()
    gap = billing.entitlement(account)
    assert (gap.status, gap.allowance) == ("past_due", 25)
    billing.fake.advance(3 * DAY)
    billing.fake.pay_invoice(failed["invoice"])
    billing.sync()
    paid = billing.entitlement(account)
    assert (paid.status, paid.plan_code, paid.allowance) == ("active", "starter", 250)
    assert paid.period_id == gap.period_id, "the provisional row must become the paid period"
    assert len(granted_invoices(billing, account)) == 2


# --------------------------------------------------------------------------- invoices that are not ordinary


def test_a_paid_invoice_for_a_subscription_that_ended_first_is_visible_to_the_owner(billing):
    """The customer pays an open invoice after the subscription was cancelled.

    Stripe keeps the money and never revives the subscription, so the account stays free.
    Nothing may be granted silently: the owner has to be told so they can refund or reinstate.
    """
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    renewal = billing.fake.advance_period(ids["subscription"], outcome="failed")
    billing.sync()
    billing.fake.cancel_now(ids["subscription"])
    billing.sync()
    assert billing.entitlement(account).plan_code == "free"
    billing.fake.pay_invoice(renewal["invoice"])
    billing.sync()
    assert billing.entitlement(account).plan_code == "free", "a cancelled subscription entitles nothing"
    # The money arrived and a period row was written, but nothing can ever use it.
    assert renewal["invoice"] in granted_invoices(billing, account)
    assert billing.alerts("paid_invoice_without_access"), "the owner must see that money arrived without access"


def test_a_zero_amount_cycle_invoice_grants_the_period_once(billing):
    """A fully discounted or balance-covered renewal is still a paid period, and only one."""
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    renewal = billing.fake.advance_period(ids["subscription"], outcome="failed")
    invoice = billing.fake.invoices[renewal["invoice"]]
    for line in invoice["lines"]["data"]:
        line["amount"] = 0
    invoice["amount_due"] = 0
    billing.fake.pay_invoice(renewal["invoice"])
    billing.sync(duplicate=True)
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("starter", 250)
    assert len(granted_invoices(billing, account)) == 2


def test_an_invoice_whose_billing_reason_is_not_a_subscription_period_grants_nothing(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    before = granted_invoices(billing, account)
    renewal = billing.fake.advance_period(ids["subscription"], outcome="failed")
    billing.fake.invoices[renewal["invoice"]]["billing_reason"] = "manual"
    billing.fake.pay_invoice(renewal["invoice"])
    billing.sync()
    assert granted_invoices(billing, account) == before, "only subscription period invoices may grant"


def test_an_invoice_line_priced_in_another_currency_is_refused(billing):
    """A price the mirror does not know (wrong currency) may never grant an allowance."""
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    foreign = billing.fake.add_price(unit_amount=900, currency="eur", lookup_key=None)
    renewal = billing.fake.advance_period(ids["subscription"], outcome="failed")
    invoice = billing.fake.invoices[renewal["invoice"]]
    for line in invoice["lines"]["data"]:
        line["pricing"]["price_details"]["price"] = foreign["id"]
    billing.fake.pay_invoice(renewal["invoice"])
    billing.sync()
    assert len(granted_invoices(billing, account)) == 1, "the unknown price must grant nothing"
    assert billing.alerts("unknown_price")


def test_an_invoice_with_no_period_line_alerts_and_grants_nothing(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    renewal = billing.fake.advance_period(ids["subscription"], outcome="failed")
    billing.fake.invoices[renewal["invoice"]]["lines"]["data"] = []
    billing.fake.pay_invoice(renewal["invoice"])
    billing.sync()
    assert len(granted_invoices(billing, account)) == 1
    assert billing.alerts("invoice_without_period")


# --------------------------------------------------------------------------- upgrades


def upgrade(billing, account, target="pro", outcome="paid"):
    url = service.start_upgrade(billing.db, billing.settings, billing.client, account, target, now=billing.now)
    session_id = url.rsplit("test_", 1)[1]
    result = billing.fake.confirm_portal_upgrade(session_id, outcome=outcome)
    billing.sync()
    return result


def test_an_upgrade_uplifts_only_once_however_often_its_invoice_is_replayed(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    billing.fake.advance(10 * DAY)
    before = billing.entitlement(account).allowance
    upgrade(billing, account)
    uplifted = billing.entitlement(account)
    assert uplifted.plan_code == "pro" and before < uplifted.allowance <= 800
    for _ in range(3):
        billing.sync(duplicate=True)
    assert billing.entitlement(account).allowance == uplifted.allowance


def test_an_unpaid_upgrade_invoice_grants_no_uplift(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    billing.fake.advance(5 * DAY)
    upgrade(billing, account, outcome="failed")
    entitlement = billing.entitlement(account)
    assert entitlement.allowance == 250, "the uplift must wait for the proration payment"


def test_a_second_upgrade_in_the_same_period_adds_nothing(billing):
    """plans.toml sells two paid plans, so the second upgrade can only be a repeat of the first."""
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    billing.fake.advance(2 * DAY)
    upgrade(billing, account, "pro")
    first = billing.entitlement(account).allowance
    assert 250 < first <= 800
    billing.fake.advance(2 * DAY)
    with pytest.raises(service.BillingError) as raised:
        service.start_upgrade(billing.db, billing.settings, billing.client, account, "pro", now=billing.now)
    assert raised.value.code == "not_an_upgrade"
    # Even applying the uplift again by hand leaves the period where it is.
    again = billing.db.write(lambda conn: entitlements.apply_upgrade(
        conn, billing.settings, account_id=account, subscription_id=billing.one(
            "SELECT subscription_id FROM subscriptions WHERE account_id = ?", account)["subscription_id"],
        new_plan_code="pro", effective_at=billing.now))
    assert again == "already"
    assert billing.entitlement(account).allowance == first


def test_an_upgrade_then_a_full_refund_leaves_the_account_free(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    billing.fake.advance(5 * DAY)
    result = upgrade(billing, account)
    assert billing.entitlement(account).plan_code == "pro"
    billing.fake.refund(result["charge"])
    billing.sync()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.status) == ("free", "free")
    assert billing.one("SELECT status FROM subscriptions")["status"] == "canceled"


# --------------------------------------------------------------------------- refunds and disputes


def test_a_partial_refund_changes_nothing_but_is_reported(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    billing.fake.refund(ids["charge"], amount=300)
    billing.sync()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("starter", 250)
    assert billing.alerts("partial_refund")
    assert billing.one("SELECT status FROM subscriptions")["status"] == "active"


def test_partial_refunds_that_add_up_to_the_whole_charge_cancel_the_subscription(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    billing.fake.refund(ids["charge"], amount=400)
    billing.sync()
    assert billing.entitlement(account).plan_code == "starter"
    billing.fake.refund(ids["charge"], amount=500)
    billing.sync()
    assert billing.entitlement(account).plan_code == "free"
    assert billing.one("SELECT status FROM subscriptions")["status"] == "canceled"


def test_a_dispute_suspends_before_it_is_closed_and_a_replay_keeps_the_first_time(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    dispute = billing.fake.open_dispute(ids["charge"])
    billing.sync()
    suspended = billing.entitlement(account)
    assert (suspended.status, suspended.allowance) == ("suspended", 25)
    opened_at = billing.one("SELECT disputed_at FROM subscriptions")["disputed_at"]
    billing.fake.advance(DAY)
    billing.sync(duplicate=True)
    assert billing.one("SELECT disputed_at FROM subscriptions")["disputed_at"] == opened_at
    billing.fake.close_dispute(dispute, "won")
    billing.sync()
    restored = billing.entitlement(account)
    assert (restored.status, restored.plan_code, restored.allowance) == ("active", "starter", 250)


@pytest.mark.parametrize("closing", ["won", "warning_closed", "prevented"])
def test_every_won_outcome_restores_access(billing, closing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    dispute = billing.fake.open_dispute(ids["charge"])
    billing.sync()
    assert billing.entitlement(account).status == "suspended"
    billing.fake.close_dispute(dispute, closing)
    billing.sync()
    assert billing.entitlement(account).status == "active"


def test_closing_one_dispute_does_not_lift_a_suspension_another_still_justifies(billing):
    """A customer who charges back several invoices must stay suspended until all are settled.

    Winning the first one back must not hand access to an account whose other chargebacks are
    still open: that is exactly the case suspension exists for.
    """
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    renewal = billing.fake.advance_period(ids["subscription"])
    billing.sync()
    first = billing.fake.open_dispute(ids["charge"])
    second = billing.fake.open_dispute(renewal["charge"])
    billing.sync()
    assert billing.entitlement(account).status == "suspended"
    billing.fake.close_dispute(first, "won")
    billing.sync()
    assert billing.entitlement(account).status == "suspended", "one chargeback is still open"
    billing.fake.close_dispute(second, "won")
    billing.sync()
    restored = billing.entitlement(account)
    assert (restored.status, restored.plan_code) == ("active", "starter")


def test_a_lost_dispute_keeps_the_suspension_even_if_another_is_won(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    renewal = billing.fake.advance_period(ids["subscription"])
    billing.sync()
    lost = billing.fake.open_dispute(ids["charge"])
    won = billing.fake.open_dispute(renewal["charge"])
    billing.sync()
    billing.fake.close_dispute(lost, "lost")
    billing.sync()
    billing.fake.close_dispute(won, "won")
    billing.sync()
    assert billing.entitlement(account).status == "suspended"


def test_a_lost_dispute_cancels_and_leaves_the_account_suspended_for_good(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    dispute = billing.fake.open_dispute(ids["charge"])
    billing.sync()
    billing.fake.close_dispute(dispute, "lost")
    billing.sync()
    entitlement = billing.entitlement(account)
    assert (entitlement.status, entitlement.plan_code, entitlement.allowance) == ("suspended", "free", 25)
    assert billing.one("SELECT status FROM subscriptions")["status"] == "canceled"
    assert billing.alerts("dispute_lost")
    # Even a month later the account stays suspended: nothing silently forgives a lost dispute.
    billing.fake.advance(40 * DAY)
    assert billing.entitlement(account).status == "suspended"


# --------------------------------------------------------------------------- customers and accounts


def test_a_checkout_session_naming_another_account_is_refused(billing):
    """The account comes from the local session row; forged metadata may not redirect a grant."""
    buyer = make_account(billing.db)
    victim = make_account(billing.db)
    result = billing.checkout(buyer)
    session = billing.fake.sessions[result.session_id]
    session["client_reference_id"] = victim
    session["metadata"] = {"account_id": victim}
    session["_subscription_metadata"] = {"account_id": victim}
    billing.fake.complete_checkout(result.session_id)
    billing.sync()
    assert billing.entitlement(victim).plan_code == "free", "the named account must not be entitled"
    assert billing.alerts("checkout_customer_mismatch")


def test_subscription_metadata_naming_another_account_is_reported(billing):
    buyer = make_account(billing.db)
    victim = make_account(billing.db)
    result = billing.checkout(buyer)
    billing.fake.sessions[result.session_id]["_subscription_metadata"] = {"account_id": victim}
    billing.fake.complete_checkout(result.session_id)
    billing.sync()
    assert billing.entitlement(victim).plan_code == "free"
    assert billing.entitlement(buyer).plan_code == "starter"  # the customer's own account decides
    assert billing.alerts("subscription_account_mismatch")


def test_a_deleted_stripe_customer_ends_access_and_later_events_are_ignored(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    billing.fake.delete_customer(ids["customer"])
    billing.sync()
    assert billing.entitlement(account).plan_code == "free"
    assert billing.one("SELECT deleted_at FROM stripe_customers WHERE account_id = ?", account)["deleted_at"] is not None
    # A late event for the tombstoned customer must change nothing.
    before = periods(billing, account)
    billing.fake.emit("customer.subscription.updated", billing.fake.subscriptions[ids["subscription"]])
    billing.sync()
    assert periods(billing, account) == before
    assert billing.entitlement(account).plan_code == "free"


def test_a_declined_first_payment_can_be_retried_and_paid_without_a_second_subscription(billing):
    """The dead end a declined card used to create: the portal cannot pay an open invoice.

    The customer must be able to finish the purchase straight away, and must still end up with
    exactly one subscription and exactly one granted period.
    """
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter", outcome="declined")
    assert billing.entitlement(account).plan_code == "free"
    retry = billing.checkout(account, "starter")
    assert retry.kind == "invoice", "a customer whose first payment failed must be able to pay it"
    assert retry.url == billing.fake.invoices[ids["invoice"]]["hosted_invoice_url"]
    assert billing.fake.creates["checkout.session"] == 1, "no second Checkout Session"
    assert len(billing.all("SELECT * FROM subscriptions WHERE account_id = ?", account)) == 1
    assert granted_invoices(billing, account) == [], "nothing may be granted before the invoice is paid"
    # Paying on that page is what Stripe does when the customer supplies a working card.
    billing.fake.pay_invoice(ids["invoice"])
    billing.sync()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("starter", 250)
    assert granted_invoices(billing, account) == [ids["invoice"]]
    assert len(billing.all("SELECT * FROM subscriptions WHERE account_id = ?", account)) == 1


def test_a_customer_awaiting_authentication_is_sent_to_the_same_page(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter", outcome="requires_action")
    retry = billing.checkout(account, "starter")
    assert retry.kind == "invoice" and retry.url == billing.fake.invoices[ids["invoice"]]["hosted_invoice_url"]


def test_an_account_with_a_live_plan_still_goes_to_the_portal(billing):
    """An entitling subscription must keep sending a second purchase to the portal."""
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    assert billing.checkout(account, "pro").kind == "portal"


def test_a_stray_incomplete_beside_a_live_plan_does_not_redirect_to_its_invoice(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    customer = billing.one("SELECT customer_id FROM stripe_customers WHERE account_id = ?", account)["customer_id"]
    stray = billing.fake._new_subscription(customer, billing.price("pro"), status="incomplete")
    billing.fake._new_invoice(stray, "subscription_create", [])
    billing.sync()
    assert billing.checkout(account, "pro").kind == "portal"


def test_late_events_of_the_declined_attempt_never_disturb_the_paid_subscription(billing):
    """The first attempt's webhooks arrive after the retry succeeded."""
    account = make_account(billing.db)
    first = billing.checkout(account, "starter")
    ids = billing.fake.complete_checkout(first.session_id, outcome="declined")
    stale = billing.fake.take_events()  # nothing about the failed attempt is delivered yet
    retry = billing.checkout(account, "starter")
    assert retry.kind == "invoice"
    billing.fake.pay_invoice(ids["invoice"])
    billing.sync()
    assert billing.entitlement(account).allowance == 250
    deliver_events(billing, stale)
    billing.process()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("starter", 250)
    assert granted_invoices(billing, account) == [ids["invoice"]]
    row = billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])
    assert row["status"] == "active", "the refetch decides, not the stale event"


def test_a_first_invoice_that_stripe_expired_lets_a_fresh_checkout_start(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter", outcome="declined")
    billing.fake.expire_incomplete(ids["subscription"])
    billing.sync()
    result = billing.checkout(account, "starter")
    assert result.kind == "checkout", "an expired attempt must not block a new one"
    assert billing.entitlement(account).plan_code == "free"


def test_a_customer_declined_on_one_plan_can_choose_another(billing):
    """Declined on Starter, then "Choose Pro": they must get a Pro checkout, never Starter's invoice.

    Found by walking the billing page as a customer: the second press led to the first plan's
    invoice, so somebody who had changed their mind would have paid for the plan they no longer
    wanted. The unfinished Starter purchase is ended instead, and exactly one subscription,
    the Pro one, is ever live or granted.
    """
    account = make_account(billing.db)
    first = billing.subscribe(account, "starter", outcome="declined")
    assert billing.checkout(account, "starter").kind == "invoice", "the same plan is still finished on its invoice"

    second = billing.checkout(account, "pro")
    assert second.kind == "checkout", "a different plan must start a checkout of its own"
    assert billing.fake.subscriptions[first["subscription"]]["status"] == "canceled"
    row = billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", first["subscription"])
    assert row["status"] == "canceled", "the mirror learns of it at once, without waiting for a webhook"
    assert billing.entitlement(account).payment_problem is False, "no unfinished payment is left to warn about"
    assert granted_invoices(billing, account) == []

    paid = billing.fake.complete_checkout(second.session_id)
    billing.sync()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("pro", billing.settings.plans.get("pro").monthly_credits)
    assert granted_invoices(billing, account) == [paid["invoice"]]
    live = billing.all(
        "SELECT plan_code FROM subscriptions WHERE account_id = ? AND status NOT IN ('canceled', 'incomplete_expired')", account)
    assert [r["plan_code"] for r in live] == ["pro"]


def test_the_abandoned_attempts_checkout_session_cannot_be_completed_later(billing):
    """An old tab must not be able to finish the purchase the customer walked away from."""
    account = make_account(billing.db)
    first = billing.checkout(account, "starter")
    assert first.kind == "checkout"  # left open: the customer never pressed Pay
    customer = billing.one("SELECT customer_id FROM stripe_customers WHERE account_id = ?", account)["customer_id"]
    stray = billing.fake._new_subscription(customer, billing.price("starter"), status="incomplete")
    billing.fake._new_invoice(stray, "subscription_create", [])
    billing.sync()

    assert billing.checkout(account, "pro").kind == "checkout"
    assert billing.fake.sessions[first.session_id]["status"] == "expired"
    with pytest.raises(RuntimeError):
        billing.fake.complete_checkout(first.session_id)


def test_choosing_another_plan_never_ends_a_purchase_stripe_already_shows_as_paid(billing):
    """The payment landed a moment ago; the subscription is about to become active. Leave it alone."""
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter", outcome="declined")
    billing.fake.invoices[ids["invoice"]]["status"] = "paid"  # Stripe has the money; the status has not caught up
    result = billing.checkout(account, "pro")
    assert result.kind == "portal"
    assert billing.fake.subscriptions[ids["subscription"]]["status"] == "incomplete", "nothing was cancelled"
    assert billing.fake.creates["checkout.session"] == 1


def test_choosing_another_plan_leaves_a_live_plan_and_its_stray_alone(billing):
    account = make_account(billing.db)
    billing.subscribe(account, "starter")
    customer = billing.one("SELECT customer_id FROM stripe_customers WHERE account_id = ?", account)["customer_id"]
    stray = billing.fake._new_subscription(customer, billing.price("pro"), status="incomplete")
    billing.fake._new_invoice(stray, "subscription_create", [])
    billing.sync()
    assert billing.checkout(account, "pro").kind == "portal"
    assert billing.fake.subscriptions[stray["id"]]["status"] == "incomplete"
    assert billing.entitlement(account).plan_code == "starter"


def test_an_unfinished_purchase_starling_did_not_sell_is_never_cancelled(billing):
    """An `incomplete` subscription on a price that is not one of Starling's plans is not ours to end."""
    account = make_account(billing.db)
    billing.subscribe(account, "starter", outcome="declined")
    customer = billing.one("SELECT customer_id FROM stripe_customers WHERE account_id = ?", account)["customer_id"]
    foreign_price = dict(billing.fake.prices[billing.price("starter")], id="price_foreign", lookup_key="something_else")
    billing.fake.prices["price_foreign"] = foreign_price
    foreign = billing.fake._new_subscription(customer, "price_foreign", status="incomplete")
    billing.fake._new_invoice(foreign, "subscription_create", [])
    billing.sync()
    result = billing.checkout(account, "pro")
    assert result.kind in ("invoice", "portal")
    assert billing.fake.subscriptions[foreign["id"]]["status"] == "incomplete"
    assert billing.fake.creates["checkout.session"] == 1, "no new checkout while something unknown is unfinished"


def test_paying_the_abandoned_invoice_afterwards_is_reported_and_grants_nothing_usable(billing):
    """The fake keeps a cancelled subscription's invoice payable, which is the worse case."""
    account = make_account(billing.db)
    first = billing.subscribe(account, "starter", outcome="declined")
    second = billing.checkout(account, "pro")
    billing.fake.complete_checkout(second.session_id)
    billing.sync()
    billing.fake.pay_invoice(first["invoice"])
    billing.sync()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("pro", billing.settings.plans.get("pro").monthly_credits)
    assert billing.alerts("paid_invoice_without_access"), "money for the abandoned plan must reach the owner's eyes"


def test_a_suspended_account_cannot_start_a_checkout(billing):
    account = make_account(billing.db)
    billing.db.write(lambda conn: conn.execute("UPDATE accounts SET status = 'suspended' WHERE id = ?", (account,)))
    with pytest.raises(service.BillingError) as raised:
        billing.checkout(account)
    assert raised.value.http_status == 403
    assert billing.all("SELECT * FROM checkout_sessions") == []


def test_an_unverified_account_cannot_start_a_checkout(billing):
    account = make_account(billing.db, verified=False)
    with pytest.raises(service.BillingError) as raised:
        billing.checkout(account)
    assert raised.value.code == "email_unverified"
    assert billing.all("SELECT * FROM stripe_customers") == []


# --------------------------------------------------------------------------- concurrency at the processor


def test_the_whole_purchase_processed_concurrently_grants_once(billing):
    account = make_account(billing.db)
    billing.fake.complete_checkout(billing.checkout(account).session_id)
    billing.deliver(duplicate=True)
    ids = [row["event_id"] for row in billing.all(
        "SELECT event_id FROM billing_events WHERE status = 'received' ORDER BY rowid")]
    barrier = threading.Barrier(len(ids))

    def run(event_id):
        barrier.wait()
        processor.process_event(billing.db, billing.settings, billing.client, event_id, billing.now)

    threads = [threading.Thread(target=run, args=(event_id,)) for event_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance) == ("starter", 250)
    assert len(granted_invoices(billing, account)) == 1
    assert len(set(granted_invoices(billing, account))) == 1
