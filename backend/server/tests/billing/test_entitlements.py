"""Entitlement rules that need no Stripe: free periods, lazy rows, grace, carry-over, suspension."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from starling_server.billing import entitlements
from starling_server.billing.entitlements import add_months, monthly_period

from .conftest import DAY, make_account
from .fake_stripe import START


def ts(year, month, day, hour=0):
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


def free_rows(db, account):
    with db.read() as conn:
        return conn.execute(
            "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'free' ORDER BY period_start", (account,)
        ).fetchall()


def test_add_months_clamps_to_the_month_end_and_returns_to_the_anchor_day():
    assert add_months(ts(2026, 1, 31), 1) == ts(2026, 2, 28)
    assert add_months(ts(2026, 1, 31), 2) == ts(2026, 3, 31)
    assert add_months(ts(2028, 1, 31), 1) == ts(2028, 2, 29)
    assert add_months(ts(2026, 11, 15), 3) == ts(2027, 2, 15)


def test_monthly_period_contains_now():
    anchor = ts(2026, 1, 31, 12)
    assert monthly_period(anchor, ts(2026, 3, 1)) == (ts(2026, 2, 28, 12), ts(2026, 3, 31, 12))
    assert monthly_period(anchor, anchor) == (anchor, ts(2026, 2, 28, 12))
    assert monthly_period(anchor, anchor - 5) == (anchor, add_months(anchor, 1))
    start, end = monthly_period(anchor, ts(2027, 7, 4))
    assert start <= ts(2027, 7, 4) < end


def test_free_periods_follow_email_verification_and_are_created_lazily(db, bsettings):
    created = ts(2026, 6, 10)
    account = make_account(db, created_at=created)
    verified = created + 60
    now = ts(2026, 9, 21)
    entitlement = entitlements.current_entitlement(db, bsettings, account, now)
    assert (entitlement.plan_code, entitlement.status, entitlement.allowance, entitlement.interval) == ("free", "free", 25, None)
    assert entitlement.cost_ceiling_micros == 300_000 and not entitlement.payment_problem
    assert (entitlement.period_start, entitlement.period_end) == (add_months(verified, 3), add_months(verified, 4))
    assert len(free_rows(db, account)) == 1
    assert entitlements.current_entitlement(db, bsettings, account, now).period_id == entitlement.period_id
    following = entitlements.current_entitlement(db, bsettings, account, entitlement.period_end)
    assert following.period_id != entitlement.period_id and following.period_start == entitlement.period_end
    assert len(free_rows(db, account)) == 2


def test_unverified_accounts_are_anchored_to_creation(db, bsettings):
    created = ts(2026, 8, 5)
    account = make_account(db, verified=False, created_at=created)
    assert entitlements.current_entitlement(db, bsettings, account, ts(2026, 9, 21)).period_start == add_months(created, 1)


def test_concurrent_resolution_creates_one_free_row(db, bsettings):
    account = make_account(db)
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(entitlements.current_entitlement(db, bsettings, account, START).period_id))
               for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(seen)) == 1 and len(free_rows(db, account)) == 1


def test_unknown_account_is_an_error(db, bsettings):
    with pytest.raises(LookupError):
        entitlements.current_entitlement(db, bsettings, "00000000-0000-4000-8000-000000000000", START)


def test_inside_a_write_transaction_the_transaction_variant_is_required(db, bsettings):
    account = make_account(db)
    with pytest.raises(RuntimeError):
        db.write(lambda conn: entitlements.current_entitlement(db, bsettings, account, START))
    inside = db.write(lambda conn: entitlements.entitlement_in_transaction(conn, bsettings, account, START))
    assert inside.plan_code == "free" and inside.period_id


def _subscription(db, account, **fields):
    row = {"subscription_id": "sub_manual", "account_id": account, "customer_id": "cus_manual", "status": "active",
           "price_id": "price_manual", "plan_code": "starter", "current_period_start": START - DAY,
           "current_period_end": START + 29 * DAY, "first_payment_failed_at": None, "disputed_at": None,
           "livemode": 0, "synced_at": START}
    row.update(fields)
    columns = ", ".join(row)
    db.write(lambda conn: conn.execute(
        f"INSERT INTO subscriptions ({columns}) VALUES ({', '.join('?' * len(row))})", tuple(row.values())))


def _paid_period(db, account, start, end, invoice="in_manual", allowance=250):
    db.write(lambda conn: conn.execute(
        "INSERT INTO entitlement_periods (id, account_id, source, subscription_id, plan_code, period_start, period_end, "
        "allowance, cost_ceiling_micros, granted_by_invoice_id, created_at) "
        "VALUES (?, ?, 'subscription', 'sub_manual', 'starter', ?, ?, ?, 4500000, ?, ?)",
        (f"period-{invoice}", account, start, end, allowance, invoice, start)))
    return f"period-{invoice}"


def _committed(db, account, period_id, committed):
    db.write(lambda conn: conn.execute(
        "INSERT INTO usage_counters (account_id, period_id, committed) VALUES (?, ?, ?)", (account, period_id, committed)))


def test_a_subscription_without_a_paid_period_grants_nothing(db, bsettings):
    account = make_account(db)
    _subscription(db, account)
    assert entitlements.current_entitlement(db, bsettings, account, START).plan_code == "free"


@pytest.mark.parametrize(("committed", "carry"), [(240, 10), (100, 25), (250, 0)])
def test_past_due_keeps_access_with_a_capped_carry_over_until_the_grace_ends(db, bsettings, committed, carry):
    account = make_account(db)
    previous_start, rollover = START - 31 * DAY, START - DAY
    failed_at = START - DAY + 3600
    _subscription(db, account, status="past_due", current_period_start=rollover,
                  current_period_end=add_months(rollover, 1), first_payment_failed_at=failed_at)
    last = _paid_period(db, account, previous_start, rollover)
    _committed(db, account, last, committed)
    during = entitlements.current_entitlement(db, bsettings, account, failed_at + 7 * DAY - 1)
    assert (during.plan_code, during.status, during.payment_problem) == ("starter", "past_due", True)
    assert during.allowance == carry and during.period_id != last
    assert (during.period_start, during.period_end) == (rollover, add_months(rollover, 1))
    after = entitlements.current_entitlement(db, bsettings, account, failed_at + 7 * DAY)
    assert (after.plan_code, after.status, after.payment_problem) == ("free", "free", True)


def test_open_dispute_suspends_with_the_free_allowance(db, bsettings):
    account = make_account(db)
    _subscription(db, account, disputed_at=START - 60)
    _paid_period(db, account, START - DAY, START + 29 * DAY)
    entitlement = entitlements.current_entitlement(db, bsettings, account, START)
    assert (entitlement.status, entitlement.plan_code, entitlement.allowance) == ("suspended", "free", 25)


def test_cancellation_time_ends_paid_access_even_before_the_mirror_updates(db, bsettings):
    account = make_account(db)
    period_end = START + 29 * DAY
    _subscription(db, account, cancel_at_period_end=1, current_period_end=period_end)
    _paid_period(db, account, START - DAY, period_end)
    before = entitlements.current_entitlement(db, bsettings, account, period_end - 1)
    assert (before.plan_code, before.cancel_at) == ("starter", period_end)
    assert entitlements.current_entitlement(db, bsettings, account, period_end).plan_code == "free"
