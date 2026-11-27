"""Adversarial entitlement resolution: every Stripe status, calendar edge and clock skew.

The invariant under test throughout: an account may only hold an allowance that a paid
invoice justifies (plus the documented renewal carry-over and upgrade uplift), and a
customer who has paid must not silently drop to free.
"""
from __future__ import annotations

import threading

import pytest

from starling_server.billing import entitlements, mirror
from starling_server.billing.entitlements import add_months, monthly_period

from .conftest import DAY, make_account
from .fake_stripe import START

GRACE_S = 7 * DAY


def subscription(db, account, **fields):
    row = {"subscription_id": "sub_manual", "account_id": account, "customer_id": "cus_manual", "status": "active",
           "price_id": "price_manual", "plan_code": "starter", "current_period_start": START - DAY,
           "current_period_end": START + 29 * DAY, "cancel_at_period_end": 0, "cancel_at": None, "ended_at": None,
           "first_payment_failed_at": None, "disputed_at": None, "livemode": 0, "synced_at": START}
    row.update(fields)
    columns = ", ".join(row)
    db.write(lambda conn: conn.execute(
        f"INSERT INTO subscriptions ({columns}) VALUES ({', '.join('?' * len(row))})", tuple(row.values())))
    return row["subscription_id"]


def paid_period(db, account, start, end, *, invoice="in_manual", allowance=250, plan="starter", sub="sub_manual"):
    period_id = f"period-{invoice}"
    db.write(lambda conn: conn.execute(
        "INSERT INTO entitlement_periods (id, account_id, source, subscription_id, plan_code, period_start, period_end, "
        "allowance, cost_ceiling_micros, granted_by_invoice_id, created_at) "
        "VALUES (?, ?, 'subscription', ?, ?, ?, ?, ?, 4500000, ?, ?)",
        (period_id, account, sub, plan, start, end, allowance, invoice, start)))
    return period_id


def used(db, account, period_id, committed=0, granted=0):
    db.write(lambda conn: conn.execute(
        "INSERT INTO usage_counters (account_id, period_id, committed, granted) VALUES (?, ?, ?, ?)",
        (account, period_id, committed, granted)))


# --------------------------------------------------------------------------- the status table of CONTRACTS 7.1


@pytest.mark.parametrize(
    ("status", "plan", "entitlement_status"),
    [
        ("active", "starter", "active"),
        ("past_due", "starter", "past_due"),  # inside the grace window
        ("trialing", "starter", "active"),
        ("unpaid", "free", "free"),
        ("canceled", "free", "free"),
        ("incomplete", "free", "free"),
        ("incomplete_expired", "free", "free"),
        ("paused", "free", "free"),
    ],
)
def test_every_stripe_status_resolves_the_way_the_contract_says(db, bsettings, status, plan, entitlement_status):
    account = make_account(db)
    subscription(db, account, status=status, first_payment_failed_at=START - 60)
    paid_period(db, account, START - DAY, START + 29 * DAY)
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.status) == (plan, entitlement_status)
    assert result.allowance == (250 if plan == "starter" else 25)


@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_an_entitling_status_without_a_paid_invoice_still_grants_nothing(db, bsettings, status):
    """A subscription Stripe calls live, but which no invoice ever paid for, stays free."""
    account = make_account(db)
    subscription(db, account, status=status, first_payment_failed_at=START - 60)
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.allowance) == ("free", 25)


# --------------------------------------------------------------------------- which payment needs the customer


def test_a_first_payment_that_never_finished_is_named_with_its_plan(db, bsettings):
    """On Free, never charged: the website must not tell this customer they "keep paid access"."""
    account = make_account(db)
    subscription(db, account, status="incomplete", plan_code="pro")
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.payment_problem) == ("free", True)
    assert result.payment_issue == {"kind": "first_payment", "plan_code": "pro", "plan_name": "Pro"}


def test_a_failed_renewal_inside_the_grace_window_is_a_renewal_issue(db, bsettings):
    account = make_account(db)
    subscription(db, account, status="past_due", first_payment_failed_at=START - 60)
    paid_period(db, account, START - DAY, START + 29 * DAY)
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.status, result.payment_problem) == ("starter", "past_due", True)
    assert result.payment_issue == {"kind": "renewal", "plan_code": "starter", "plan_name": "Starter"}


def test_a_failed_renewal_after_the_grace_window_has_lapsed(db, bsettings):
    """The customer HAD paid access and lost it: different words from a first payment."""
    account = make_account(db)
    subscription(db, account, status="past_due", first_payment_failed_at=START - GRACE_S - 1)
    paid_period(db, account, START - 31 * DAY, START - DAY)
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.payment_problem) == ("free", True)
    assert result.payment_issue == {"kind": "lapsed", "plan_code": "starter", "plan_name": "Starter"}


def test_a_lapsed_renewal_outranks_an_unfinished_first_payment(db, bsettings):
    account = make_account(db)
    subscription(db, account, subscription_id="sub_old", status="past_due", first_payment_failed_at=START - GRACE_S - 1)
    subscription(db, account, subscription_id="sub_new", status="incomplete", plan_code="pro")
    assert entitlements.current_entitlement(db, bsettings, account, START).payment_issue["kind"] == "lapsed"


def test_an_unfinished_payment_for_an_unknown_plan_names_no_plan(db, bsettings):
    account = make_account(db)
    subscription(db, account, status="incomplete", plan_code=None)
    issue = entitlements.current_entitlement(db, bsettings, account, START).payment_issue
    assert issue == {"kind": "first_payment", "plan_code": None, "plan_name": None}


@pytest.mark.parametrize("status", ["active", "trialing", "canceled", "incomplete_expired", "unpaid", "paused"])
def test_no_other_status_reports_a_payment_issue(db, bsettings, status):
    account = make_account(db)
    subscription(db, account, status=status)
    paid_period(db, account, START - DAY, START + 29 * DAY)
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert result.payment_issue is None and result.payment_problem is False


def test_a_healthy_paid_plan_ignores_a_stray_unfinished_purchase(db, bsettings):
    """`payment_problem` and `payment_issue` must agree: neither is raised for a customer whose plan is fine."""
    account = make_account(db)
    subscription(db, account)
    paid_period(db, account, START - DAY, START + 29 * DAY)
    subscription(db, account, subscription_id="sub_stray", status="incomplete", plan_code="pro")
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.payment_problem, result.payment_issue) == ("starter", False, None)


def test_past_due_without_a_recorded_failure_time_falls_back_to_the_sync_time(db, bsettings):
    account = make_account(db)
    subscription(db, account, status="past_due", first_payment_failed_at=None, synced_at=START)
    paid_period(db, account, START - 31 * DAY, START - DAY)
    assert entitlements.current_entitlement(db, bsettings, account, START + GRACE_S - 1).plan_code == "starter"
    assert entitlements.current_entitlement(db, bsettings, account, START + GRACE_S).plan_code == "free"


def test_an_unmapped_price_leaves_the_account_free_even_while_stripe_says_active(db, bsettings):
    account = make_account(db)
    subscription(db, account, status="active", plan_code=None)
    paid_period(db, account, START - DAY, START + 29 * DAY)
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.allowance) == ("free", 25)


def test_a_plan_removed_from_plans_toml_fails_closed(db, bsettings):
    account = make_account(db)
    subscription(db, account, status="active", plan_code="legacy_gold")
    paid_period(db, account, START - DAY, START + 29 * DAY, plan="legacy_gold", allowance=9999)
    assert entitlements.current_entitlement(db, bsettings, account, START).plan_code == "free"


# --------------------------------------------------------------------------- cancellation


def test_cancel_at_period_end_keeps_access_to_the_last_second_then_drops(db, bsettings):
    account = make_account(db)
    end = START + 29 * DAY
    subscription(db, account, cancel_at_period_end=1, current_period_end=end)
    paid_period(db, account, START - DAY, end)
    assert entitlements.current_entitlement(db, bsettings, account, end - 1).plan_code == "starter"
    assert entitlements.current_entitlement(db, bsettings, account, end).plan_code == "free"


def test_resuming_before_the_end_restores_paid_access(db, bsettings):
    account = make_account(db)
    end = START + 29 * DAY
    subscription(db, account, cancel_at_period_end=1, current_period_end=end)
    paid_period(db, account, START - DAY, end)
    assert entitlements.current_entitlement(db, bsettings, account, START).cancel_at == end
    db.write(lambda conn: conn.execute("UPDATE subscriptions SET cancel_at_period_end = 0, canceled_at = NULL"))
    resumed = entitlements.current_entitlement(db, bsettings, account, START)
    assert (resumed.plan_code, resumed.cancel_at) == ("starter", None)
    assert entitlements.current_entitlement(db, bsettings, account, end + 1).plan_code == "starter"


def test_a_standalone_cancel_at_ends_access_at_its_own_time(db, bsettings):
    account = make_account(db)
    cancel_at = START + 10 * DAY
    subscription(db, account, cancel_at_period_end=0, cancel_at=cancel_at)
    paid_period(db, account, START - DAY, START + 29 * DAY)
    assert entitlements.current_entitlement(db, bsettings, account, cancel_at - 1).plan_code == "starter"
    assert entitlements.current_entitlement(db, bsettings, account, cancel_at).plan_code == "free"


def test_ended_at_in_the_past_ends_access_whatever_the_status_says(db, bsettings):
    account = make_account(db)
    account_end = START - 60
    subscription(db, account, status="active", ended_at=account_end)
    paid_period(db, account, START - DAY, START + 29 * DAY)
    assert entitlements.current_entitlement(db, bsettings, account, START).plan_code == "free"


# --------------------------------------------------------------------------- calendar edges


@pytest.mark.parametrize(
    ("anchor_iso", "months", "expected_iso"),
    [
        ("2027-01-31", 1, "2027-02-28"),
        ("2028-01-31", 1, "2028-02-29"),  # leap year
        ("2028-01-31", 2, "2028-03-31"),  # returns to the anchor day, never drifts
        ("2027-08-31", 1, "2027-09-30"),
        ("2027-12-31", 1, "2028-01-31"),
        ("2028-02-29", 12, "2029-02-28"),
    ],
)
def test_month_end_anchors_clamp_and_return_to_the_anchor_day(anchor_iso, months, expected_iso):
    from datetime import datetime, timezone

    anchor = int(datetime.fromisoformat(anchor_iso).replace(tzinfo=timezone.utc).timestamp())
    expected = int(datetime.fromisoformat(expected_iso).replace(tzinfo=timezone.utc).timestamp())
    assert add_months(anchor, months) == expected


def test_monthly_periods_tile_a_year_without_gaps_or_overlaps_from_a_month_end_anchor():
    from datetime import datetime, timezone

    anchor = int(datetime.fromisoformat("2027-01-31").replace(tzinfo=timezone.utc).timestamp())
    cursor, seen = anchor, []
    for _ in range(14):
        start, end = monthly_period(anchor, cursor)
        assert start <= cursor < end
        if seen:
            assert start == seen[-1][1], "a period must begin exactly where the previous one ended"
        seen.append((start, end))
        cursor = end
    # Every instant in the year lands in exactly one period.
    for probe in range(anchor, anchor + 330 * DAY, 7 * DAY + 3607):
        matches = [pair for pair in seen if pair[0] <= probe < pair[1]]
        assert len(matches) == 1, probe


def test_a_free_period_that_starts_before_the_anchor_is_the_anchor_period(db, bsettings):
    account = make_account(db, created_at=START)
    before = entitlements.current_entitlement(db, bsettings, account, START - 10 * DAY)
    assert before.period_start == START + 60 and before.period_end == add_months(START + 60, 1)


# --------------------------------------------------------------------------- clock skew


def test_a_period_stripe_has_already_granted_wins_over_a_gateway_clock_that_lags(db, bsettings):
    """The gateway clock is a few seconds behind Stripe's when the renewal is paid."""
    account = make_account(db)
    rollover = START
    subscription(db, account, current_period_start=rollover, current_period_end=add_months(rollover, 1))
    paid_period(db, account, START - 31 * DAY, rollover, invoice="in_old")
    paid_period(db, account, rollover, add_months(rollover, 1), invoice="in_new")
    lagging = entitlements.current_entitlement(db, bsettings, account, rollover - 5)
    # Whatever row it lands on, the account may never hold more than one period's allowance.
    assert lagging.allowance <= 250
    assert entitlements.current_entitlement(db, bsettings, account, rollover).allowance == 250
    rows = db.read().__enter__().execute(
        "SELECT COUNT(*) FROM entitlement_periods WHERE account_id = ? AND source = 'subscription'", (account,)
    ).fetchone()[0]
    assert rows == 2, "no extra period row may be invented by a clock that lags"


def test_a_mirror_that_has_not_seen_the_rollover_bridges_with_the_capped_carry_over(db, bsettings):
    account = make_account(db)
    stale_end = START - 2 * DAY
    subscription(db, account, current_period_start=stale_end - 30 * DAY, current_period_end=stale_end)
    last = paid_period(db, account, stale_end - 30 * DAY, stale_end)
    used(db, account, last, committed=0)
    bridged = entitlements.current_entitlement(db, bsettings, account, START)
    assert bridged.plan_code == "starter" and bridged.allowance == 25  # 10% of 250, never a fresh period
    assert bridged.period_start == stale_end and bridged.period_end == add_months(stale_end, 1)


@pytest.mark.parametrize(("committed", "granted", "carry"), [(0, 0, 25), (240, 0, 10), (250, 0, 0), (300, 0, 0), (250, 40, 25)])
def test_the_carry_over_never_exceeds_a_tenth_of_the_plan_nor_what_was_left(db, bsettings, committed, granted, carry):
    account = make_account(db)
    rollover = START - DAY
    subscription(db, account, status="past_due", current_period_start=rollover,
                 current_period_end=add_months(rollover, 1), first_payment_failed_at=rollover)
    last = paid_period(db, account, rollover - 30 * DAY, rollover)
    used(db, account, last, committed=committed, granted=granted)
    assert entitlements.current_entitlement(db, bsettings, account, START).allowance == carry


# --------------------------------------------------------------------------- more than one subscription


def test_two_entitling_subscriptions_give_the_highest_plan_and_never_the_sum(db, bsettings):
    account = make_account(db)
    subscription(db, account, subscription_id="sub_a", plan_code="starter", current_period_start=START - 10 * DAY)
    subscription(db, account, subscription_id="sub_b", plan_code="pro", current_period_start=START - 5 * DAY)
    paid_period(db, account, START - 10 * DAY, START + 20 * DAY, invoice="in_a", sub="sub_a", allowance=250)
    paid_period(db, account, START - 5 * DAY, START + 25 * DAY, invoice="in_b", sub="sub_b", allowance=800, plan="pro")
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.allowance) == ("pro", 800)


def test_on_a_tie_the_older_subscription_wins(db, bsettings):
    account = make_account(db)
    subscription(db, account, subscription_id="sub_new", current_period_start=START - 2 * DAY)
    subscription(db, account, subscription_id="sub_old", current_period_start=START - 9 * DAY)
    paid_period(db, account, START - 2 * DAY, START + 28 * DAY, invoice="in_new", sub="sub_new")
    paid_period(db, account, START - 9 * DAY, START + 21 * DAY, invoice="in_old", sub="sub_old")
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert result.period_id == "period-in_old" and result.allowance == 250


def test_a_higher_unpaid_subscription_never_hides_the_one_the_customer_paid_for(db, bsettings):
    """Two entitling subscriptions, only the cheaper one paid for.

    The account must keep what it paid for. Choosing the higher plan and then finding nothing
    granted for it would drop a paying customer to the free plan.
    """
    account = make_account(db)
    subscription(db, account, subscription_id="sub_paid", plan_code="starter", current_period_start=START - DAY)
    subscription(db, account, subscription_id="sub_unpaid", plan_code="pro", current_period_start=START - 2 * DAY)
    paid_period(db, account, START - DAY, START + 29 * DAY, invoice="in_paid", sub="sub_paid")
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.allowance) == ("starter", 250)
    assert result.period_id == "period-in_paid"


def test_a_trialing_higher_plan_does_not_cancel_out_a_paid_one(db, bsettings):
    """A trial the owner started in the Dashboard never has a paid invoice of its own."""
    account = make_account(db)
    subscription(db, account, subscription_id="sub_paid", plan_code="starter")
    subscription(db, account, subscription_id="sub_trial", plan_code="pro", status="trialing")
    paid_period(db, account, START - DAY, START + 29 * DAY, invoice="in_paid", sub="sub_paid")
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert result.plan_code == "starter" and result.allowance == 250


def test_the_highest_paid_subscription_still_wins_when_several_are_paid(db, bsettings):
    account = make_account(db)
    subscription(db, account, subscription_id="sub_low", plan_code="starter")
    subscription(db, account, subscription_id="sub_high", plan_code="pro")
    paid_period(db, account, START - DAY, START + 29 * DAY, invoice="in_low", sub="sub_low")
    paid_period(db, account, START - DAY - 1, START + 29 * DAY, invoice="in_high", sub="sub_high",
                plan="pro", allowance=800)
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.plan_code, result.allowance) == ("pro", 800)


def test_a_dispute_on_any_subscription_suspends_the_whole_account(db, bsettings):
    account = make_account(db)
    subscription(db, account, subscription_id="sub_live", status="active")
    subscription(db, account, subscription_id="sub_old", status="canceled", disputed_at=START - DAY)
    paid_period(db, account, START - DAY, START + 29 * DAY, sub="sub_live")
    result = entitlements.current_entitlement(db, bsettings, account, START)
    assert (result.status, result.plan_code, result.allowance) == ("suspended", "free", 25)


# --------------------------------------------------------------------------- grants


def test_one_grant_per_invoice_however_often_it_is_replayed(db, bsettings):
    account = make_account(db)
    subscription(db, account)
    outcomes = [
        db.write(lambda conn: entitlements.grant_paid_period(
            conn, bsettings, account_id=account, subscription_id="sub_manual", plan_code="starter",
            period_start=START, period_end=add_months(START, 1), invoice_id="in_replay", now=START))
        for _ in range(6)
    ]
    assert outcomes == ["granted"] + ["already"] * 5
    with db.read() as conn:
        rows = conn.execute(
            "SELECT allowance FROM entitlement_periods WHERE granted_by_invoice_id = 'in_replay'").fetchall()
    assert [row["allowance"] for row in rows] == [250]


def test_the_same_invoice_granted_from_many_threads_lands_once(db, bsettings):
    account = make_account(db)
    subscription(db, account)
    outcomes: list[str] = []
    barrier = threading.Barrier(10)

    def grant():
        barrier.wait()
        outcomes.append(db.write(lambda conn: entitlements.grant_paid_period(
            conn, bsettings, account_id=account, subscription_id="sub_manual", plan_code="starter",
            period_start=START, period_end=add_months(START, 1), invoice_id="in_race", now=START)))

    threads = [threading.Thread(target=grant) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("granted") == 1 and outcomes.count("already") == 9
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entitlement_periods WHERE account_id = ?", (account,)).fetchone()[0] == 1


def test_two_invoices_for_the_same_period_grant_only_the_first(db, bsettings):
    """A duplicate cycle invoice (a Stripe retry that issued a second one) must not stack."""
    account = make_account(db)
    subscription(db, account)
    first = db.write(lambda conn: entitlements.grant_paid_period(
        conn, bsettings, account_id=account, subscription_id="sub_manual", plan_code="starter",
        period_start=START, period_end=add_months(START, 1), invoice_id="in_one", now=START))
    second = db.write(lambda conn: entitlements.grant_paid_period(
        conn, bsettings, account_id=account, subscription_id="sub_manual", plan_code="pro",
        period_start=START, period_end=add_months(START, 1), invoice_id="in_two", now=START))
    assert (first, second) == ("granted", "already")
    assert entitlements.current_entitlement(db, bsettings, account, START).allowance == 250


def test_a_provisional_row_becomes_the_paid_period_and_keeps_its_usage(db, bsettings):
    account = make_account(db)
    rollover = START - DAY
    subscription(db, account, status="past_due", current_period_start=rollover,
                 current_period_end=add_months(rollover, 1), first_payment_failed_at=rollover)
    last = paid_period(db, account, rollover - 30 * DAY, rollover)
    used(db, account, last, committed=245)
    bridge = entitlements.current_entitlement(db, bsettings, account, START)
    assert bridge.allowance == 5 and bridge.period_id != last
    db.write(lambda conn: entitlements.grant_paid_period(
        conn, bsettings, account_id=account, subscription_id="sub_manual", plan_code="starter",
        period_start=rollover, period_end=add_months(rollover, 1), invoice_id="in_late", now=START))
    after = entitlements.current_entitlement(db, bsettings, account, START)
    assert after.period_id == bridge.period_id, "the same row must carry the gap's usage into the paid period"
    assert after.allowance == 250
    with db.read() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM entitlement_periods WHERE account_id = ? AND source = 'subscription'", (account,)
        ).fetchone()[0]
    assert count == 2  # the old paid period and the one that was provisional


def test_a_grant_for_an_unpaid_plan_is_refused(db, bsettings):
    account = make_account(db)
    subscription(db, account)
    for plan_code in ("free", "does_not_exist", ""):
        with pytest.raises(ValueError):
            db.write(lambda conn, code=plan_code: entitlements.grant_paid_period(
                conn, bsettings, account_id=account, subscription_id="sub_manual", plan_code=code,
                period_start=START, period_end=add_months(START, 1), invoice_id=f"in_{code}", now=START))


# --------------------------------------------------------------------------- upgrade arithmetic


@pytest.mark.parametrize(
    ("elapsed_fraction", "expected_uplift"),
    [(0.0, 550), (0.25, 412), (0.5, 275), (0.75, 137)],
)
def test_the_uplift_is_the_remaining_fraction_of_the_difference(db, bsettings, elapsed_fraction, expected_uplift):
    account = make_account(db)
    start, end = START, add_months(START, 1)
    length = end - start
    subscription(db, account)
    paid_period(db, account, start, end)
    effective_at = start + int(length * elapsed_fraction)
    outcome = db.write(lambda conn: entitlements.apply_upgrade(
        conn, bsettings, account_id=account, subscription_id="sub_manual", new_plan_code="pro",
        effective_at=effective_at))
    assert outcome == "uplifted"
    with db.read() as conn:
        row = conn.execute("SELECT plan_code, allowance FROM entitlement_periods WHERE account_id = ?", (account,)).fetchone()
    assert row["plan_code"] == "pro"
    assert abs(row["allowance"] - (250 + expected_uplift)) <= 1, row["allowance"]
    assert row["allowance"] <= 800, "an uplift may never exceed the plan it upgrades to"


def test_an_upgrade_at_the_very_end_of_a_period_adds_nothing(db, bsettings):
    account = make_account(db)
    start, end = START, add_months(START, 1)
    subscription(db, account)
    paid_period(db, account, start, end)
    db.write(lambda conn: entitlements.apply_upgrade(
        conn, bsettings, account_id=account, subscription_id="sub_manual", new_plan_code="pro", effective_at=end - 1))
    with db.read() as conn:
        row = conn.execute("SELECT allowance FROM entitlement_periods WHERE account_id = ?", (account,)).fetchone()
    assert row["allowance"] == 250


def test_replaying_an_upgrade_never_uplifts_twice(db, bsettings):
    account = make_account(db)
    start, end = START, add_months(START, 1)
    subscription(db, account)
    paid_period(db, account, start, end)
    outcomes = [
        db.write(lambda conn: entitlements.apply_upgrade(
            conn, bsettings, account_id=account, subscription_id="sub_manual", new_plan_code="pro",
            effective_at=start))
        for _ in range(4)
    ]
    assert outcomes == ["uplifted", "already", "already", "already"]
    with db.read() as conn:
        assert conn.execute("SELECT allowance FROM entitlement_periods WHERE account_id = ?", (account,)).fetchone()[0] == 800


def test_a_downgrade_disguised_as_an_upgrade_never_lowers_or_raises_the_allowance(db, bsettings):
    account = make_account(db)
    start, end = START, add_months(START, 1)
    subscription(db, account, plan_code="pro")
    paid_period(db, account, start, end, plan="pro", allowance=800)
    outcome = db.write(lambda conn: entitlements.apply_upgrade(
        conn, bsettings, account_id=account, subscription_id="sub_manual", new_plan_code="starter",
        effective_at=start + DAY))
    assert outcome == "already"
    with db.read() as conn:
        row = conn.execute("SELECT plan_code, allowance FROM entitlement_periods WHERE account_id = ?", (account,)).fetchone()
    assert (row["plan_code"], row["allowance"]) == ("pro", 800)


def test_an_upgrade_before_the_period_is_granted_says_so_rather_than_inventing_one(db, bsettings):
    account = make_account(db)
    subscription(db, account)
    outcome = db.write(lambda conn: entitlements.apply_upgrade(
        conn, bsettings, account_id=account, subscription_id="sub_manual", new_plan_code="pro", effective_at=START))
    assert outcome == "no_period"
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entitlement_periods WHERE account_id = ?", (account,)).fetchone()[0] == 0


def test_an_upgrade_never_touches_a_period_that_is_only_provisional(db, bsettings):
    """Only a granted (paid) period may be uplifted: the renewal gap row must be left alone."""
    account = make_account(db)
    rollover = START - DAY
    subscription(db, account, status="past_due", current_period_start=rollover,
                 current_period_end=add_months(rollover, 1), first_payment_failed_at=rollover)
    paid_period(db, account, rollover - 30 * DAY, rollover)
    provisional = entitlements.current_entitlement(db, bsettings, account, START)
    assert provisional.allowance == 25
    outcome = db.write(lambda conn: entitlements.apply_upgrade(
        conn, bsettings, account_id=account, subscription_id="sub_manual", new_plan_code="pro", effective_at=START))
    assert outcome == "no_period"
    assert entitlements.current_entitlement(db, bsettings, account, START).allowance == 25


def test_a_full_refund_ends_the_period_at_once(db, bsettings):
    account = make_account(db)
    start, end = START - DAY, add_months(START, 1)
    subscription(db, account)
    paid_period(db, account, start, end)
    assert entitlements.current_entitlement(db, bsettings, account, START).plan_code == "starter"
    db.write(lambda conn: entitlements.end_subscription_periods(conn, "sub_manual", START))
    db.write(lambda conn: conn.execute("UPDATE subscriptions SET status = 'canceled', ended_at = ?", (START,)))
    assert entitlements.current_entitlement(db, bsettings, account, START).plan_code == "free"
    with db.read() as conn:
        row = conn.execute("SELECT period_end FROM entitlement_periods WHERE granted_by_invoice_id IS NOT NULL").fetchone()
    assert row["period_end"] == START


# --------------------------------------------------------------------------- free plan edges


def test_going_free_then_paid_then_free_in_one_month_gives_no_second_free_allowance(db, bsettings):
    account = make_account(db, created_at=START - 5 * DAY)
    first = entitlements.current_entitlement(db, bsettings, account, START)
    assert first.plan_code == "free"
    used(db, account, first.period_id, committed=25)
    subscription(db, account, current_period_start=START, current_period_end=add_months(START, 1))
    paid_period(db, account, START, add_months(START, 1))
    assert entitlements.current_entitlement(db, bsettings, account, START + 60).plan_code == "starter"
    db.write(lambda conn: conn.execute("UPDATE subscriptions SET status = 'canceled', ended_at = ?", (START + 120,)))
    back = entitlements.current_entitlement(db, bsettings, account, START + 180)
    assert back.period_id == first.period_id, "the same free period must be resumed, not a new one"
    with db.read() as conn:
        counters = conn.execute(
            "SELECT committed FROM usage_counters WHERE account_id = ? AND period_id = ?", (account, first.period_id)
        ).fetchone()
    assert counters["committed"] == 25  # the free allowance stays spent


def test_a_free_period_row_is_created_only_once_under_concurrency(db, bsettings):
    account = make_account(db)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def resolve():
        barrier.wait()
        results.append(entitlements.current_entitlement(db, bsettings, account, START).period_id)

    threads = [threading.Thread(target=resolve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 1
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entitlement_periods WHERE account_id = ?", (account,)).fetchone()[0] == 1


def test_preview_never_writes_a_row(db, bsettings):
    account = make_account(db)
    preview = entitlements.preview_entitlement(db, bsettings, account, START)
    assert preview.plan_code == "free" and preview.period_id == ""
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entitlement_periods").fetchone()[0] == 0


def test_plan_rank_orders_by_price_then_allowance(bsettings):
    free, starter, pro = (bsettings.plans.get(code) for code in ("free", "starter", "pro"))
    assert mirror.plan_rank(free) < mirror.plan_rank(starter) < mirror.plan_rank(pro)
