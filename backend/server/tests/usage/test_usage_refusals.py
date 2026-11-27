"""Reserve refusals in the contract order (CONTRACTS sections 6.3 and 7), each with a negative control."""
from __future__ import annotations

import dataclasses

import pytest

from starling_server.usage import service
from starling_server.usage.service import Reservation, UsageError

from .conftest import (
    NOW,
    PERIOD_END,
    body_hash,
    counters,
    make_account,
    new_key,
    record_ai_call,
    set_counter,
    set_service_state,
    with_plan_limits,
)

UPGRADE = "/pricing?from=extension&reason=limit"


def attempt(db, settings, account_id, now=NOW, key=None):
    return service.reserve(db, settings, account_id, key or new_key(), body_hash(), "/api/v1/assist/stream", "chat", now)


def refusal(db, settings, account_id, now=NOW) -> UsageError:
    with pytest.raises(UsageError) as caught:
        attempt(db, settings, account_id, now)
    return caught.value


def set_account(db, account_id, **columns) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    db.write(lambda c: c.execute(f"UPDATE accounts SET {assignments} WHERE id = ?", (*columns.values(), account_id)))


def budget(db, scope, window_key, cost=0, limit=1_000, tripped_at=None) -> None:
    db.write(lambda c: c.execute(
        "INSERT INTO budget_counters (scope, window_key, cost_micros, limit_micros, tripped_at) VALUES (?, ?, ?, ?, ?)",
        (scope, window_key, cost, limit, tripped_at),
    ))


def commit_one(db, settings, account_id, now=NOW) -> None:
    reservation = attempt(db, settings, account_id, now)
    service.activate(db, reservation.id, now)
    record_ai_call(db, reservation.id, now=now)
    service.finish(db, settings, reservation.id, True, '{"ok": true}', now)


def test_refusals_follow_the_contract_order(database, roomy_settings, fake_entitlements):
    db, settings = database, roomy_settings
    account = make_account(db, settings, status="suspended", verified=False, consent=False)
    set_service_state(db, "ai_paused", True)
    fake_entitlements.set(account, status="suspended", allowance=0, cost_ceiling_micros=0)
    no_rate = with_plan_limits(settings, starter={"rate_per_minute": 0})

    err = refusal(db, no_rate, account)
    assert (err.http_status, err.code) == (403, "account_suspended")
    assert fake_entitlements.calls == []  # billing is not consulted for the earlier refusals
    set_account(db, account, status="active")
    assert (refusal(db, no_rate, account).http_status, refusal(db, no_rate, account).code) == (403, "email_unverified")
    set_account(db, account, email_verified_at=NOW - 10)
    assert refusal(db, no_rate, account).code == "consent_required"
    db.write(lambda c: c.execute(
        "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at) VALUES (?, 'extension_disclosure', ?, 0, ?)",
        (account, settings.consent_version, NOW - 5),
    ))
    err = refusal(db, no_rate, account)
    assert (err.http_status, err.code) == (503, "service_paused") and err.retry_after_s > 0
    assert fake_entitlements.calls == []
    set_service_state(db, "ai_paused", False)
    err = refusal(db, no_rate, account)
    assert (err.http_status, err.code, err.upgrade_path) == (402, "payment_required", UPGRADE)
    assert err.usage is not None and fake_entitlements.calls
    fake_entitlements.set(account, status="active")
    err = refusal(db, no_rate, account)
    assert (err.http_status, err.code) == (429, "rate_limited")
    err = refusal(db, settings, account)
    assert (err.http_status, err.code) == (402, "limit_reached")
    fake_entitlements.set(account, allowance=5)
    err = refusal(db, settings, account)
    assert (err.http_status, err.code) == (402, "fair_use_exceeded")
    fake_entitlements.set(account, cost_ceiling_micros=10_000)
    assert isinstance(attempt(db, settings, account), Reservation)


def test_account_must_be_active(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    err = refusal(db, s, make_account(db, s, status="suspended"))
    assert (err.http_status, err.code) == (403, "account_suspended")
    assert refusal(db, s, make_account(db, s, status="deleting")).code == "signed_out"
    assert refusal(db, s, "00000000-0000-4000-8000-000000000000").http_status == 401
    assert isinstance(attempt(db, s, make_account(db, s)), Reservation)


def test_email_must_be_verified(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    err = refusal(db, s, make_account(db, s, verified=False))
    assert (err.http_status, err.code) == (403, "email_unverified")
    assert isinstance(attempt(db, s, make_account(db, s, verified=True)), Reservation)


@pytest.mark.parametrize("variant", ["missing", "outdated", "revoked", "other_kind"])
def test_consent_must_be_current(database, roomy_settings, fake_entitlements, variant):
    db, s = database, roomy_settings
    options = {
        "missing": {"consent": False},
        "outdated": {"consent_version": "2020-01-01.1"},
        "revoked": {"consent_revoked": True},
        "other_kind": {"consent": False},
    }[variant]
    account = make_account(db, s, **options)
    if variant == "other_kind":
        db.write(lambda c: c.execute(
            "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at) VALUES (?, 'tos', ?, 0, ?)",
            (account, s.consent_version, NOW - 5),
        ))
    err = refusal(db, s, account)
    assert (err.http_status, err.code) == (403, "consent_required")
    # Control: accepting the current disclosure version is enough.
    db.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO consents (account_id, kind, version, passive_learning, granted_at, revoked_at) "
        "VALUES (?, 'extension_disclosure', ?, 0, ?, NULL)",
        (account, s.consent_version, NOW - 1),
    ))
    assert isinstance(attempt(db, s, account), Reservation)


@pytest.mark.parametrize("value", [True, {"paused": True, "message": "Back at noon."}, "not json"])
def test_owner_pause_refuses(database, roomy_settings, fake_entitlements, value):
    db, s = database, roomy_settings
    account = make_account(db, s)
    set_service_state(db, "ai_paused", value)
    err = refusal(db, s, account)
    assert (err.http_status, err.code) == (503, "service_paused") and err.retry_after_s > 0
    if isinstance(value, dict):
        assert err.message == "Back at noon."


@pytest.mark.parametrize("value", [False, {"paused": False}, 0])
def test_unpaused_values_allow_requests(database, roomy_settings, fake_entitlements, value):
    db, s = database, roomy_settings
    set_service_state(db, "ai_paused", value)
    assert isinstance(attempt(db, s, make_account(db, s)), Reservation)


def test_global_daily_budget_trip_pauses(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    day, month = service.budget_window_keys(NOW)
    account = make_account(db, s)
    budget(db, "global_day", "2020-01-01", cost=5_000, tripped_at=1)  # another day: ignored
    budget(db, "global_month", month, cost=10, limit=1_000)  # below its limit
    assert isinstance(attempt(db, s, account), Reservation)
    budget(db, "global_day", day, cost=1_000, limit=1_000, tripped_at=NOW - 60)
    err = refusal(db, s, account)
    assert (err.http_status, err.code) == (503, "service_paused")
    assert err.retry_after_s == 86_400 - NOW % 86_400


def test_global_monthly_budget_trip_pauses(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    _, month = service.budget_window_keys(NOW)
    account = make_account(db, s)
    budget(db, "global_month", month, cost=1, limit=1_000, tripped_at=NOW - 60)
    err = refusal(db, s, account)
    assert err.code == "service_paused" and 0 < err.retry_after_s <= 31 * 86_400


def test_free_daily_budget_pauses_only_free_plans(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    day, _ = service.budget_window_keys(NOW)
    budget(db, "free_day", day, tripped_at=NOW - 5)
    free = make_account(db, s)
    fake_entitlements.set(free, plan_code="free", plan_name="Free", status="free", interval=None, allowance=25,
                          cost_ceiling_micros=300_000)
    err = refusal(db, s, free)
    assert (err.code, err.message) == ("service_paused", service.FREE_PAUSED_MESSAGE)  # says paid plans continue
    assert isinstance(attempt(db, s, make_account(db, s)), Reservation)


def test_suspended_entitlement_requires_payment(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    fake_entitlements.set(account, status="suspended")
    err = refusal(db, s, account)
    assert (err.http_status, err.code) == (402, "payment_required")
    body = err.body()["error"]
    assert body["upgrade_path"] == UPGRADE and body["usage"]["allowance"] == 250
    grace = make_account(db, s)
    fake_entitlements.set(grace, status="past_due", payment_problem=True)
    assert isinstance(attempt(db, s, grace), Reservation)


def test_rate_per_minute(database, settings, fake_entitlements):
    db = database
    s = with_plan_limits(settings, starter={"rate_per_minute": 3, "rate_per_day": 100})
    account = make_account(db, s)
    first = attempt(db, s, account, NOW)
    service.release(db, s, first.id, "error", NOW + 1)  # released attempts still count
    attempt(db, s, account, NOW + 10)
    attempt(db, s, account, NOW + 20)
    err = refusal(db, s, account, NOW + 30)
    assert (err.http_status, err.code, err.retry_after_s) == (429, "rate_limited", 30)
    assert err.body()["error"]["retry_after_s"] == 30
    assert isinstance(attempt(db, s, make_account(db, s), NOW + 30), Reservation)  # limits are per account
    assert isinstance(attempt(db, s, account, NOW + 60), Reservation)  # the first attempt left the window


def test_rate_per_day(database, settings, fake_entitlements):
    db = database
    s = with_plan_limits(settings, starter={"rate_per_minute": 100, "rate_per_day": 3})
    account = make_account(db, s)
    for offset in (0, 1_000, 2_000):
        attempt(db, s, account, NOW + offset)
    err = refusal(db, s, account, NOW + 3_000)
    assert (err.http_status, err.code, err.retry_after_s) == (429, "rate_limited", 83_400)
    assert isinstance(attempt(db, s, account, NOW + 86_400), Reservation)


def test_limit_reached_carries_the_usage_block(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    fake_entitlements.set(account, allowance=2)
    commit_one(db, s, account)
    commit_one(db, s, account)
    err = refusal(db, s, account)
    assert (err.http_status, err.code) == (402, "limit_reached")
    assert err.usage == {
        "unit": "draft", "unit_label": "drafts", "allowance": 2, "used": 2, "reserved": 0, "remaining": 0,
        "resets_at": PERIOD_END, "near_limit": False, "fair_use_blocked": False, "per_request": 1,
    }
    assert err.body()["error"]["upgrade_path"] == UPGRADE
    fake_entitlements.set(account, allowance=3)
    assert isinstance(attempt(db, s, account), Reservation)


def test_in_flight_credits_are_unavailable(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    fake_entitlements.set(account, allowance=1)
    first = attempt(db, s, account)
    assert refusal(db, s, account).code == "limit_reached"
    service.release(db, s, first.id, "error", NOW)
    assert isinstance(attempt(db, s, account), Reservation)


def test_fair_use_ceiling(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    fake_entitlements.set(account, cost_ceiling_micros=50_000)
    period = fake_entitlements.period_id(account)
    set_counter(db, account, period, cost_micros=49_999)
    assert isinstance(attempt(db, s, account), Reservation)
    set_counter(db, account, period, cost_micros=50_000)
    err = refusal(db, s, account)
    assert (err.http_status, err.code) == (402, "fair_use_exceeded")
    assert err.usage["fair_use_blocked"] is True and err.upgrade_path == UPGRADE


def test_credit_multiplier_reserves_more_credits(database, roomy_settings, fake_entitlements):
    db = database
    s = dataclasses.replace(roomy_settings, ai_credit_multiplier=2)
    account = make_account(db, s)
    fake_entitlements.set(account, allowance=3)
    reservation = attempt(db, s, account)
    assert reservation.credits == 2
    assert counters(db, account, fake_entitlements.period_id(account))["reserved"] == 2
    assert refusal(db, s, account).code == "limit_reached"  # 1 credit left, 2 needed


def test_idempotency_key_is_required(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    for bad in ("", "   ", "k" * 201):
        with pytest.raises(UsageError) as caught:
            service.reserve(db, s, account, bad, body_hash(), "/api/v1/assist", "context", NOW)
        assert caught.value.http_status == 400
    assert isinstance(attempt(db, s, account, key="k" * 200), Reservation)
