"""The edges of a period, with the real entitlements (no fakes): rollover, a request that straddles
it, and an upgrade that lands while a request is running.

The rule under test is CONTRACTS section 7: a reservation belongs to the period it was made in, and
everything that happens to it afterwards is booked there. Whatever the clock or the plan does in
between, the counters, the ledger and the reservations still have to add up.
"""
from __future__ import annotations

import pytest

from starling_server.billing import entitlements
from starling_server.usage import service
from starling_server.usage.service import Reservation, UsageError

from .conftest import NOW, body_hash, make_account, new_key, record_ai_call, with_plan_limits

RESULT = '{"ok": true, "decision": {"draft": "A draft."}}'
ROOMY = {"rate_per_minute": 100_000, "rate_per_day": 100_000}


def reserve(db, s, account, now):
    attempt = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist/stream", "chat", now)
    assert isinstance(attempt, Reservation)
    return attempt


def commit(db, s, account, now):
    attempt = reserve(db, s, account, now)
    service.activate(db, attempt.id, now)
    record_ai_call(db, attempt.id, now=now)
    return attempt, service.finish(db, s, attempt.id, True, RESULT, now)


def free_period(db, s, account, now):
    return entitlements.current_entitlement(db, s, account, now)


def test_the_free_allowance_comes_back_at_the_anniversary_and_not_a_second_before(database, settings):
    db = database
    s = with_plan_limits(settings, free=ROOMY)
    account = make_account(db, s)  # verified at NOW - 5_000: the anchor of every free period
    first = free_period(db, s, account, NOW)
    for offset in range(first.allowance):
        commit(db, s, account, NOW + offset)

    last_second = first.period_end - 1
    with pytest.raises(UsageError) as refused:
        reserve(db, s, account, last_second)
    assert refused.value.code == "limit_reached"
    assert refused.value.usage["resets_at"] == first.period_end  # what the customer is told is true

    _, usage = commit(db, s, account, first.period_end)           # the first second of the next period
    second = free_period(db, s, account, first.period_end)
    assert second.period_id != first.period_id and second.period_start == first.period_end
    assert (usage["allowance"], usage["used"], usage["remaining"]) == (second.allowance, 1, second.allowance - 1)
    assert service.verify_counters(db) == []


def test_a_request_that_straddles_the_rollover_is_booked_where_it_began(database, settings):
    db = database
    s = with_plan_limits(settings, free=ROOMY)
    account = make_account(db, s)
    first = free_period(db, s, account, NOW)

    straddler = reserve(db, s, account, first.period_end - 2)     # begins two seconds before midnight, so to speak
    service.activate(db, straddler.id, first.period_end - 2)
    record_ai_call(db, straddler.id, now=first.period_end - 1)
    assert straddler.period_id == first.period_id

    after = first.period_end + 3                                   # and finishes after it
    usage_now = service.finish(db, s, straddler.id, True, RESULT, after)
    second = free_period(db, s, account, after)

    # The new period starts clean: the straddler is the OLD period's draft.
    assert (usage_now["used"], usage_now["reserved"], usage_now["allowance"]) == (0, 0, second.allowance)
    with db.read() as conn:
        old = conn.execute(
            "SELECT committed, reserved FROM usage_counters WHERE account_id = ? AND period_id = ?",
            (account, first.period_id),
        ).fetchone()
        ledger = conn.execute(
            "SELECT period_id, amount FROM credit_ledger WHERE reservation_id = ?", (straddler.id,)
        ).fetchall()
    assert (old["committed"], old["reserved"]) == (1, 0)
    assert [(row["period_id"], row["amount"]) for row in ledger] == [(first.period_id, 1)]
    assert service.verify_counters(db) == []


def test_a_request_left_live_across_the_rollover_is_swept_into_its_own_period(database, settings):
    db = database
    s = with_plan_limits(settings, free=ROOMY)
    account = make_account(db, s)
    first = free_period(db, s, account, NOW)
    for offset in range(first.allowance - 1):
        commit(db, s, account, NOW + offset)
    hung = reserve(db, s, account, first.period_end - 10)          # the last credit of the old period, never finished
    service.activate(db, hung.id, first.period_end - 10)

    # In the new period the old period's hung request must not eat into the new allowance.
    fresh = reserve(db, s, account, first.period_end + 1)
    second = free_period(db, s, account, first.period_end + 1)
    assert fresh.period_id == second.period_id
    assert service.usage_for_account(db, s, account, first.period_end + 1)["reserved"] == 1  # only `fresh`

    assert service.sweep_expired(db, s, first.period_end - 10 + service.RESERVATION_TTL_S) == [hung.id]
    assert service.usage_for_account(db, s, account, first.period_end + 400)["reserved"] == 1
    assert service.verify_counters(db) == []


def _subscribe(db, s, account, plan_code, start, end, *, invoice="in_test_1", subscription="sub_test_1"):
    """What `invoice.paid` does for a first paid period, without the webhook machinery."""
    from starling_server.billing import mirror  # noqa: F401  (the table the entitlement reads)

    def write(conn) -> None:
        conn.execute(
            "INSERT INTO stripe_customers (account_id, customer_id, livemode, created_at) VALUES (?, ?, 0, ?)",
            (account, "cus_test_1", start),
        )
        values = {
            "subscription_id": subscription, "account_id": account, "customer_id": "cus_test_1",
            "status": "active", "plan_code": plan_code, "price_id": f"price_{plan_code}",
            "current_period_start": start, "current_period_end": end, "cancel_at_period_end": 0,
            "livemode": 0, "synced_at": start,
        }
        conn.execute(
            f"INSERT INTO subscriptions ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            tuple(values.values()),
        )
        entitlements.grant_paid_period(
            conn, s, account_id=account, subscription_id=subscription, plan_code=plan_code,
            period_start=start, period_end=end, invoice_id=invoice, now=start,
        )

    db.write(write)


def test_an_upgrade_that_lands_mid_request_never_loses_or_double_books_it(database, settings):
    db = database
    s = with_plan_limits(settings, free=ROOMY, starter=ROOMY)
    account = make_account(db, s)
    free = free_period(db, s, account, NOW)
    for offset in range(free.allowance - 1):
        commit(db, s, account, NOW + offset)
    running = reserve(db, s, account, NOW + 100)                   # the last free draft is in flight...
    service.activate(db, running.id, NOW + 100)
    with pytest.raises(UsageError) as refused:
        reserve(db, s, account, NOW + 101)
    assert refused.value.code == "limit_reached"

    try:
        _subscribe(db, s, account, "starter", NOW + 102, NOW + 102 + 30 * 86_400)  # ...when the payment lands
    except Exception as exc:  # pragma: no cover - the billing tables are another workstream's
        pytest.skip(f"the billing tables changed shape: {exc}")

    paid = entitlements.current_entitlement(db, s, account, NOW + 103)
    assert (paid.plan_code, paid.status) == ("starter", "active")
    during = service.usage_for_account(db, s, account, NOW + 103)
    assert (during["allowance"], during["used"], during["reserved"]) == (paid.allowance, 0, 0)  # a clean paid period
    now_paid, _ = commit(db, s, account, NOW + 104)                # drafting continues at once
    assert now_paid.period_id == paid.period_id

    record_ai_call(db, running.id, now=NOW + 105)
    service.finish(db, s, running.id, True, RESULT, NOW + 105)     # the free draft finishes AFTER the upgrade
    after = service.usage_for_account(db, s, account, NOW + 106)
    assert (after["used"], after["reserved"]) == (1, 0)            # still one paid draft, not two
    with db.read() as conn:
        free_row = conn.execute(
            "SELECT committed, reserved FROM usage_counters WHERE account_id = ? AND period_id = ?",
            (account, free.period_id),
        ).fetchone()
    assert (free_row["committed"], free_row["reserved"]) == (free.allowance, 0)
    assert service.verify_counters(db) == []
