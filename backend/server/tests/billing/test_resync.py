"""Owner dashboard "Resync billing": `reconcile.resync_account` for one account."""
from __future__ import annotations

import threading

from starling_server.billing import processor, reconcile, webhooks

from .conftest import SECRET_KEY, WEBHOOK_SECRET, make_account


def resync(billing, account_id, now=None):
    return reconcile.resync_account(billing.db, billing.settings, billing.client, account_id, billing.now if now is None else now)


def mirror_rows(billing, account_id, *, without_sync_time=False):
    rows = [dict(row) for row in billing.all("SELECT * FROM subscriptions WHERE account_id = ? ORDER BY subscription_id", account_id)]
    if without_sync_time:
        for row in rows:
            row.pop("synced_at")
    return rows


def period_rows(billing, account_id):
    return [dict(row) for row in billing.all(
        "SELECT * FROM entitlement_periods WHERE account_id = ? ORDER BY period_start, id", account_id)]


def test_resync_corrects_a_stale_mirror_and_recovers_a_missed_grant(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    renewal = billing.fake.advance_period(ids["subscription"])
    billing.fake.set_cancel_at_period_end(ids["subscription"])
    billing.fake.take_events()  # the renewal and cancellation webhooks never arrived
    billing.db.write(lambda conn: conn.execute(
        "UPDATE subscriptions SET status = 'past_due' WHERE subscription_id = ?", (ids["subscription"],)))
    item = billing.fake.subscriptions[ids["subscription"]]["items"]["data"][0]

    result = resync(billing, account)
    assert result["ok"] is True and result["customer_id"] == ids["customer"]
    counts = (result["subscriptions"], result["mirror_changed"], result["missing"], result["periods_granted"],
              result["upgrades_applied"], result["canceled_duplicates"])
    assert counts == (1, 1, 0, 1, 0, 0)
    assert result["entitlement_changed"] is True
    row = billing.one(
        "SELECT status, current_period_start, cancel_at_period_end, latest_invoice_id, latest_invoice_status "
        "FROM subscriptions WHERE subscription_id = ?", ids["subscription"])
    assert dict(row) == {"status": "active", "current_period_start": item["current_period_start"], "cancel_at_period_end": 1,
                         "latest_invoice_id": renewal["invoice"], "latest_invoice_status": "paid"}
    granted = billing.one("SELECT plan_code, period_start FROM entitlement_periods WHERE granted_by_invoice_id = ?", renewal["invoice"])
    assert (granted["plan_code"], granted["period_start"]) == ("starter", item["current_period_start"])
    entitlement = result["entitlement"]
    assert (entitlement["plan_code"], entitlement["status"], entitlement["allowance"], entitlement["cancel_at"]) == (
        "starter", "active", 250, item["current_period_end"])


def test_repeat_resync_changes_nothing(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    billing.fake.advance_period(ids["subscription"])
    billing.fake.take_events()
    first = resync(billing, account)
    assert first["ok"] and first["periods_granted"] == 1
    mirror, periods = mirror_rows(billing, account, without_sync_time=True), period_rows(billing, account)
    writes = billing.fake.calls("POST") + billing.fake.calls("DELETE")
    second = resync(billing, account)
    assert second == {**first, **{key: 0 for key in reconcile.COUNTERS}, "entitlement_changed": False}
    assert billing.fake.calls("POST") + billing.fake.calls("DELETE") == writes  # no Stripe writes
    assert period_rows(billing, account) == periods  # history kept, nothing added
    assert mirror_rows(billing, account, without_sync_time=True) == mirror


def test_unknown_account_and_account_without_a_customer(billing):
    requests = len(billing.fake.requests)
    assert resync(billing, "00000000-0000-4000-8000-000000000000") == {"ok": False, "reason": "no_customer"}
    assert resync(billing, make_account(billing.db)) == {"ok": False, "reason": "no_customer"}
    assert len(billing.fake.requests) == requests


def test_stripe_errors_are_reported_without_raising(billing):
    account = make_account(billing.db)
    billing.subscribe(account)
    mirror, periods = mirror_rows(billing, account), period_rows(billing, account)
    billing.fake.fail_next("GET", "/v1/subscriptions", status=500, times=3)
    failed = resync(billing, account)
    assert failed == {"ok": False, "reason": "stripe_error", "message": "StripeAPIError HTTP 500"}
    billing.fake.fail_next("GET", "/v1/subscriptions", status=401, times=1, error_type="invalid_request_error")
    refused = resync(billing, account)
    assert refused == {"ok": False, "reason": "stripe_error", "message": "StripeAuthenticationError HTTP 401"}
    for result in (failed, refused):
        assert SECRET_KEY not in result["message"] and "injected" not in result["message"]
    assert mirror_rows(billing, account) == mirror and period_rows(billing, account) == periods
    assert resync(billing, account)["ok"] is True  # control: works again once Stripe answers


def test_resync_is_safe_while_webhooks_are_processed(billing):
    account = make_account(billing.db)
    ids = billing.subscribe(account)
    renewal = billing.fake.advance_period(ids["subscription"])
    billing.fake.set_cancel_at_period_end(ids["subscription"])
    deliveries = [billing.fake.delivery(event, WEBHOOK_SECRET) for event in billing.fake.take_events()]
    queue = processor.BillingQueue(billing.db, billing.settings, billing.client, clock=lambda: billing.now)
    queue.start()
    results, errors = [], []

    def resync_repeatedly():
        try:
            for _ in range(5):
                results.append(resync(billing, account))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def deliver():
        for body, header in deliveries:
            outcome = webhooks.intake(billing.db, billing.settings, body, header, billing.now)
            if outcome.enqueue:
                queue.enqueue(outcome.event_id)

    try:
        threads = [threading.Thread(target=resync_repeatedly), threading.Thread(target=deliver)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert queue.drain(15)
    finally:
        queue.stop()
    assert errors == [] and all(result["ok"] for result in results)
    assert {row["status"] for row in billing.all("SELECT status FROM billing_events")} == {"processed"}
    assert len(billing.all("SELECT id FROM entitlement_periods WHERE granted_by_invoice_id = ?", renewal["invoice"])) == 1
    settled = resync(billing, account)
    assert (settled["mirror_changed"], settled["periods_granted"], settled["periods_created"]) == (0, 0, 0)
    item = billing.fake.subscriptions[ids["subscription"]]["items"]["data"][0]
    entitlement = billing.entitlement(account)
    assert (entitlement.plan_code, entitlement.allowance, entitlement.cancel_at) == ("starter", 250, item["current_period_end"])
