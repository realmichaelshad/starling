"""Serial processor: refetching, out-of-order delivery, duplicates, failures, queue and reconciler."""
from __future__ import annotations

import threading
import time

from starling_server.billing import processor, reconcile, service, webhooks

from .conftest import DAY, WEBHOOK_SECRET, make_account


def event_rows(billing):
    return billing.all("SELECT event_id, type, status, attempts, last_error FROM billing_events ORDER BY rowid")


def paid_periods(billing, account):
    return billing.all(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'subscription' ORDER BY period_start", account
    )


def deliver_and_process(billing, event):
    body, header = billing.fake.delivery(event, WEBHOOK_SECRET)
    result = webhooks.intake(billing.db, billing.settings, body, header, billing.now)
    assert result.status_code == 200
    return processor.process_event(billing.db, billing.settings, billing.client, event["id"], billing.now)


def test_paid_checkout_grants_the_plan(billing):
    account = make_account(billing.db)
    assert billing.entitlement(account).plan_code == "free"
    ids = billing.subscribe(account, "starter")
    entitlement = billing.entitlement(account)
    item = billing.fake.subscriptions[ids["subscription"]]["items"]["data"][0]
    assert (entitlement.plan_code, entitlement.status, entitlement.allowance, entitlement.interval) == ("starter", "active", 250, "month")
    assert (entitlement.period_start, entitlement.period_end) == (item["current_period_start"], item["current_period_end"])
    assert billing.one("SELECT status FROM checkout_sessions WHERE session_id = ?", ids["session"])["status"] == "complete"
    assert {(row["status"], row["attempts"]) for row in event_rows(billing)} == {("processed", 1)}
    assert len(paid_periods(billing, account)) == 1


def test_duplicate_deliveries_are_processed_once(billing):
    account = make_account(billing.db)
    billing.fake.complete_checkout(billing.checkout(account).session_id)
    events = billing.fake.take_events()
    for event in events + events:
        body, header = billing.fake.delivery(event, WEBHOOK_SECRET)
        assert webhooks.intake(billing.db, billing.settings, body, header, billing.now).status_code == 200
    assert len(event_rows(billing)) == len(events)
    billing.process()
    for event in events:
        body, header = billing.fake.delivery(event, WEBHOOK_SECRET)
        assert not webhooks.intake(billing.db, billing.settings, body, header, billing.now).enqueue
    assert billing.process() == {}
    assert [row["attempts"] for row in event_rows(billing)] == [1] * len(events)
    assert len(paid_periods(billing, account)) == 1 and billing.entitlement(account).allowance == 250


def test_concurrent_deliveries_through_the_queue_worker_process_once(billing):
    account = make_account(billing.db)
    billing.fake.complete_checkout(billing.checkout(account).session_id)
    deliveries = [billing.fake.delivery(event, WEBHOOK_SECRET) for event in billing.fake.take_events()]
    queue = processor.BillingQueue(billing.db, billing.settings, billing.client, clock=lambda: billing.now)
    queue.start()

    def deliver_all():
        for body, header in deliveries:
            result = webhooks.intake(billing.db, billing.settings, body, header, billing.now)
            if result.enqueue:
                queue.enqueue(result.event_id)

    try:
        threads = [threading.Thread(target=deliver_all) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert queue.drain(15)
    finally:
        queue.stop()
    rows = event_rows(billing)
    assert len(rows) == len(deliveries)
    assert {(row["status"], row["attempts"]) for row in rows} == {("processed", 1)}
    assert len(paid_periods(billing, account)) == 1


def test_updated_before_created_converges(billing):
    account = make_account(billing.db)
    ids = billing.fake.complete_checkout(billing.checkout(account).session_id)
    billing.fake.set_cancel_at_period_end(ids["subscription"])
    by_type = {event["type"]: event for event in billing.fake.take_events()}
    for name in ("customer.subscription.updated", "invoice.paid", "checkout.session.completed", "customer.subscription.created"):
        assert deliver_and_process(billing, by_type[name]) == "processed"
    row = billing.one("SELECT cancel_at_period_end, status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])
    assert (row["cancel_at_period_end"], row["status"]) == (1, "active")  # the stale `created` payload did not win
    entitlement = billing.entitlement(account)
    period_end = billing.fake.subscriptions[ids["subscription"]]["items"]["data"][0]["current_period_end"]
    assert (entitlement.plan_code, entitlement.cancel_at) == ("starter", period_end)


def test_paid_before_created_grants_the_period(billing):
    account = make_account(billing.db)
    ids = billing.fake.complete_checkout(billing.checkout(account).session_id)
    events = billing.fake.take_events()
    paid = next(event for event in events if event["type"] == "invoice.paid")
    assert deliver_and_process(billing, paid) == "processed"
    assert billing.entitlement(account).plan_code == "starter"
    for event in events:
        if event is not paid:
            deliver_and_process(billing, event)
    assert len(paid_periods(billing, account)) == 1
    assert billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])["status"] == "active"


def test_deleted_before_updated_converges_to_canceled(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    billing.fake.set_cancel_at_period_end(ids["subscription"])
    billing.fake.cancel_now(ids["subscription"])
    events = billing.fake.take_events()
    deleted = [event for event in events if event["type"] == "customer.subscription.deleted"]
    updated = [event for event in events if event["type"] == "customer.subscription.updated"]
    for event in deleted + updated:
        deliver_and_process(billing, event)
    assert billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])["status"] == "canceled"
    assert billing.entitlement(account).plan_code == "free"


def test_handler_failure_then_redelivery(billing):
    account = make_account(billing.db)
    ids = billing.fake.complete_checkout(billing.checkout(account).session_id)
    paid = next(event for event in billing.fake.take_events() if event["type"] == "invoice.paid")
    billing.fake.fail_next("GET", f"/v1/invoices/{ids['invoice']}", status=500, times=3)
    body, header = billing.fake.delivery(paid, WEBHOOK_SECRET)
    webhooks.intake(billing.db, billing.settings, body, header, billing.now)
    assert processor.process_event(billing.db, billing.settings, billing.client, paid["id"], billing.now) == "failed"
    row = billing.one("SELECT status, attempts, last_error FROM billing_events WHERE event_id = ?", paid["id"])
    assert row["status"] == "failed" and row["attempts"] == 1 and "StripeAPIError" in row["last_error"]
    assert billing.entitlement(account).plan_code == "free"
    redelivery = webhooks.intake(billing.db, billing.settings, body, header, billing.now)
    assert redelivery.status_code == 200 and redelivery.enqueue
    assert processor.process_event(billing.db, billing.settings, billing.client, paid["id"], billing.now) == "processed"
    assert billing.entitlement(account).plan_code == "starter"
    assert billing.one("SELECT attempts FROM billing_events WHERE event_id = ?", paid["id"])["attempts"] == 2


def test_queue_retries_a_failed_event_by_itself(billing):
    account = make_account(billing.db)
    ids = billing.fake.complete_checkout(billing.checkout(account).session_id)
    billing.deliver()
    billing.fake.fail_next("GET", f"/v1/invoices/{ids['invoice']}", status=503, times=3)
    queue = processor.BillingQueue(billing.db, billing.settings, billing.client, retry_base_s=0.05, clock=lambda: billing.now)
    queue.start()
    try:
        assert queue.recover() == 3
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and billing.entitlement(account).plan_code != "starter":
            time.sleep(0.05)
    finally:
        queue.stop()
    assert billing.entitlement(account).plan_code == "starter"
    paid = billing.one("SELECT status, attempts FROM billing_events WHERE type = 'invoice.paid'")
    assert (paid["status"], paid["attempts"]) == ("processed", 2)


def test_startup_recovery_requeues_orphaned_events(billing):
    account = make_account(billing.db)
    billing.fake.complete_checkout(billing.checkout(account).session_id)
    billing.deliver()
    billing.db.write(lambda conn: conn.execute(
        "UPDATE billing_events SET status = 'processing' WHERE type = 'invoice.paid'"))
    queue = processor.BillingQueue(billing.db, billing.settings, billing.client, clock=lambda: billing.now)
    assert queue.recover() == 3
    queue.start()
    try:
        assert queue.drain(15)
    finally:
        queue.stop()
    assert {row["status"] for row in event_rows(billing)} == {"processed"}
    assert billing.entitlement(account).plan_code == "starter"


def test_unknown_price_fails_closed_with_an_owner_alert(billing):
    account = make_account(billing.db)
    customer = service.ensure_customer(billing.db, billing.settings, billing.client, account, billing.now)
    rogue = billing.fake.add_price(unit_amount=500)
    billing.fake.start_subscription(customer, rogue["id"], metadata={"account_id": account})
    billing.sync()
    assert billing.entitlement(account).plan_code == "free"
    assert billing.one("SELECT plan_code FROM subscriptions WHERE customer_id = ?", customer)["plan_code"] is None
    assert paid_periods(billing, account) == []
    assert [alert["ref"] for alert in billing.alerts("unknown_price")] == [rogue["id"]]

    # Control: a known plan price for another account entitles and raises no new alert.
    other = make_account(billing.db)
    other_customer = service.ensure_customer(billing.db, billing.settings, billing.client, other, billing.now)
    billing.fake.start_subscription(other_customer, billing.price("starter"), metadata={"account_id": other})
    billing.sync()
    assert billing.entitlement(other).plan_code == "starter"
    assert len(billing.alerts("unknown_price")) == 1


def test_late_events_for_a_deleted_customer_are_ignored(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    renewal = billing.fake.advance_period(ids["subscription"])
    late = billing.fake.take_events()  # the renewal's events arrive only after the deletion
    billing.fake.delete_customer(ids["customer"])
    billing.sync()
    for event in late:
        assert deliver_and_process(billing, event) == "ignored"
    assert billing.entitlement(account).plan_code == "free"
    assert billing.one("SELECT 1 FROM entitlement_periods WHERE granted_by_invoice_id = ?", renewal["invoice"]) is None


def test_reconciler_recovers_an_event_whose_webhook_never_arrived(billing):
    account = make_account(billing.db)
    billing.fake.complete_checkout(billing.checkout(account).session_id)
    billing.fake.take_events()  # the webhooks were lost
    assert billing.entitlement(account).plan_code == "free"
    assert reconcile.reconcile_events(billing.db, billing.settings, billing.client, now=billing.now) == 3
    billing.process()
    assert billing.entitlement(account).plan_code == "starter"
    assert reconcile.reconcile_events(billing.db, billing.settings, billing.client, now=billing.now) == 0


def test_full_resync_cancels_the_extra_subscription_and_keeps_the_highest_plan(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account, "starter")
    billing.fake.advance(3600)
    extra = billing.fake.start_subscription(ids["customer"], billing.price("pro"), metadata={"account_id": account})
    billing.sync()
    assert billing.entitlement(account).plan_code == "pro"  # the highest plan applies meanwhile
    assert billing.alerts("duplicate_subscriptions")
    summary = reconcile.full_resync(billing.db, billing.settings, billing.client, now=billing.now)
    assert summary["canceled_duplicates"] == 1
    starter = billing.fake.subscriptions[ids["subscription"]]
    assert starter["status"] == "canceled" and starter["_cancel"] == {"prorate": True, "invoice_now": False}
    assert billing.fake.subscriptions[extra["subscription"]]["status"] == "active"
    assert billing.alerts("duplicate_subscriptions_canceled")
    billing.sync()
    assert billing.entitlement(account).plan_code == "pro"


def test_full_resync_mirrors_a_subscription_stripe_no_longer_has(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    del billing.fake.subscriptions[ids["subscription"]]  # 404 from now on
    summary = reconcile.full_resync(billing.db, billing.settings, billing.client, now=billing.now + DAY)
    assert summary["missing"] == 1
    assert billing.one("SELECT status FROM subscriptions WHERE subscription_id = ?", ids["subscription"])["status"] == "canceled"
    assert billing.entitlement(account, billing.now + DAY).plan_code == "free"
