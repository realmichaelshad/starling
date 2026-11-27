"""Subscription lifecycle against the fake: cancellation, renewal failure and grace, 3DS,
refunds, disputes and customer deletion."""
from __future__ import annotations

from .conftest import DAY, make_account


def item_of(billing, subscription_id):
    return billing.fake.subscriptions[subscription_id]["items"]["data"][0]


def test_cancel_at_period_end_keeps_access_until_the_period_ends(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    end = item_of(billing, ids["subscription"])["current_period_end"]
    billing.fake.set_cancel_at_period_end(ids["subscription"])
    billing.sync()
    during = billing.entitlement(account)
    assert (during.plan_code, during.status, during.cancel_at) == ("starter", "active", end)
    assert billing.entitlement(account, end - 1).plan_code == "starter"
    billing.fake.advance_period(ids["subscription"])
    billing.sync()
    assert billing.fake.subscriptions[ids["subscription"]]["status"] == "canceled"
    after = billing.entitlement(account)
    assert (after.plan_code, after.status, after.allowance, after.cancel_at) == ("free", "free", 25, None)


def test_renewal_failure_gives_grace_without_a_new_allowance_then_free_then_paid(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    first = billing.entitlement(account)
    billing.db.write(lambda conn: conn.execute(
        "INSERT INTO usage_counters (account_id, period_id, committed) VALUES (?, ?, 240)", (account, first.period_id)))
    renewal = billing.fake.advance_period(ids["subscription"], outcome="declined")
    billing.sync()
    failed_at = billing.now
    grace = billing.entitlement(account)
    assert (grace.plan_code, grace.status, grace.payment_problem) == ("starter", "past_due", True)
    assert grace.allowance == 10  # unused carry-over (250 - 240), capped at 10% of 250
    assert billing.one("SELECT 1 FROM entitlement_periods WHERE granted_by_invoice_id = ?", renewal["invoice"]) is None
    assert billing.entitlement(account, failed_at + 7 * DAY - 1).status == "past_due"
    lapsed = billing.entitlement(account, failed_at + 7 * DAY)
    assert (lapsed.plan_code, lapsed.status, lapsed.payment_problem) == ("free", "free", True)

    billing.fake.advance(8 * DAY)
    billing.fake.pay_invoice(renewal["invoice"])
    billing.sync()
    paid = billing.entitlement(account)
    assert (paid.plan_code, paid.status, paid.allowance, paid.payment_problem) == ("starter", "active", 250, False)
    assert paid.period_id == grace.period_id  # the grace row became the paid period
    row = billing.one("SELECT first_payment_failed_at FROM subscriptions WHERE subscription_id = ?", ids["subscription"])
    assert row["first_payment_failed_at"] is None


def test_incomplete_subscription_grants_nothing_until_paid(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter", outcome="requires_action")
    waiting = billing.entitlement(account)
    assert (waiting.plan_code, waiting.status, waiting.payment_problem) == ("free", "free", True)
    assert billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])["status"] == "incomplete"
    # No second subscription meanwhile: trying again leads to the unpaid first invoice's own
    # payment page, which finishes the subscription the customer already started.
    retry = billing.checkout(account, "starter")
    assert retry.kind == "invoice" and retry.url.startswith("https://")
    assert len(billing.all("SELECT * FROM subscriptions WHERE account_id = ?", account)) == 1
    billing.fake.pay_invoice(ids["invoice"])
    billing.sync()
    assert billing.entitlement(account).plan_code == "starter"


def test_incomplete_expired_is_free_and_allows_a_new_checkout(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter", outcome="declined")
    billing.fake.expire_incomplete(ids["subscription"])
    billing.sync()
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.payment_problem) == ("free", False)
    assert billing.checkout(account, "starter").kind == "checkout"


def test_full_refund_cancels_immediately_and_ends_the_entitlement(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "pro")
    billing.fake.advance(DAY)
    billing.fake.refund(ids["charge"])
    billing.sync()
    subscription = billing.fake.subscriptions[ids["subscription"]]
    assert subscription["status"] == "canceled" and subscription["_cancel"] == {"prorate": False, "invoice_now": False}
    assert billing.entitlement(account).plan_code == "free"
    period = billing.one("SELECT period_end FROM entitlement_periods WHERE granted_by_invoice_id = ?", ids["invoice"])
    assert period["period_end"] == billing.now


def test_partial_refund_changes_nothing_and_alerts_the_owner(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    billing.fake.refund(ids["charge"], amount=300)
    billing.sync()
    assert billing.fake.subscriptions[ids["subscription"]]["status"] == "active"
    assert billing.entitlement(account).plan_code == "starter"
    alerts = billing.alerts("partial_refund")
    assert len(alerts) == 1 and alerts[0]["details"]["subscriptions"] == [ids["subscription"]]


def test_open_dispute_suspends_and_a_won_dispute_restores(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    dispute = billing.fake.open_dispute(ids["charge"])
    billing.sync()
    suspended = billing.entitlement(account)
    assert (suspended.status, suspended.plan_code, suspended.allowance) == ("suspended", "free", 25)
    billing.fake.close_dispute(dispute, "won")
    billing.sync()
    restored = billing.entitlement(account)
    assert (restored.status, restored.plan_code, restored.allowance) == ("active", "starter", 250)


def test_lost_dispute_cancels_and_stays_suspended(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    dispute = billing.fake.open_dispute(ids["charge"])
    billing.sync()
    billing.fake.close_dispute(dispute, "lost")
    billing.sync()
    assert billing.fake.subscriptions[ids["subscription"]]["status"] == "canceled"
    assert billing.entitlement(account).status == "suspended"
    assert billing.alerts("dispute_lost")


def test_customer_deleted_in_stripe_then_subscribing_again(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    billing.fake.delete_customer(ids["customer"])
    billing.sync()
    assert billing.entitlement(account).plan_code == "free"
    assert billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])["status"] == "canceled"
    assert billing.one("SELECT deleted_at FROM stripe_customers WHERE account_id = ?", account)["deleted_at"] is not None
    assert billing.one("SELECT 1 FROM tombstones WHERE customer_id = ?", ids["customer"]) is not None

    result = billing.checkout(account)
    assert result.kind == "checkout"
    new_customer = billing.fake.sessions[result.session_id]["customer"]
    assert new_customer != ids["customer"]
    row = billing.one("SELECT customer_id, deleted_at FROM stripe_customers WHERE account_id = ?", account)
    assert (row["customer_id"], row["deleted_at"]) == (new_customer, None)
    billing.fake.complete_checkout(result.session_id)
    billing.sync()
    assert billing.entitlement(account).plan_code == "starter"
