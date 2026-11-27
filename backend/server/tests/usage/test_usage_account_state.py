"""account_state(): the session JSON of CONTRACTS section 6.2, shared by the extension and the website."""
from __future__ import annotations

import json

import pytest

from starling_server.config import REPO_ROOT
from starling_server.usage import grants, service
from starling_server.usage.service import UsageError

from .conftest import (
    NOW,
    PERIOD_END,
    PERIOD_START,
    body_hash,
    make_account,
    new_key,
    record_ai_call,
    set_counter,
    set_service_state,
)

# Value types per key; a tuple containing None marks a nullable value.
SCHEMA = {
    "account": {"id": str, "email_masked": str, "email_verified": bool, "status": str},
    "plan": {"code": str, "name": str, "status": str, "interval": (str, None), "period_start": int, "period_end": int,
             "cancel_at": (int, None), "scheduled_change": (dict, None), "payment_problem": bool,
             "payment_issue": (dict, None), "upgradable": bool},
    "usage": {"unit": str, "unit_label": str, "allowance": int, "used": int, "reserved": int, "remaining": int,
              "resets_at": int, "near_limit": bool, "fair_use_blocked": bool, "per_request": int},
    "consent": {"required_version": str, "accepted_version": (str, None), "passive_learning": bool},
    "service": {"status": str, "scope": (str, None), "message": (str, None), "retry_at": (int, None)},
    "extension": {"min_version": str},
}


def contract_example() -> dict:
    text = (REPO_ROOT / "docs" / "architecture" / "CONTRACTS.md").read_text(encoding="utf-8")
    block = text.split("### 6.2", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


def allowed_types(section: str, name: str) -> tuple:
    expected = SCHEMA[section][name]
    return expected if isinstance(expected, tuple) else (expected,)


def assert_matches_schema(state: dict) -> None:
    assert {section: set(fields) for section, fields in state.items()} == {s: set(f) for s, f in SCHEMA.items()}
    for section, fields in SCHEMA.items():
        for name in fields:
            value = state[section][name]
            ok = any(value is None if kind is None else type(value) is kind for kind in allowed_types(section, name))
            assert ok, f"{section}.{name} has the wrong type: {value!r}"


def commit(db, s, account, now=NOW) -> dict:
    attempt = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", now)
    service.activate(db, attempt.id, now)
    record_ai_call(db, attempt.id, now=now)
    return service.finish(db, s, attempt.id, True, "{}", now)


def test_schema_table_is_the_contract_example():
    example = contract_example()
    assert {section: set(fields) for section, fields in example.items()} == {s: set(f) for s, f in SCHEMA.items()}
    for section, fields in example.items():
        for name, value in fields.items():
            if value is not None:
                assert type(value) in allowed_types(section, name), f"{section}.{name}"


def test_state_matches_the_contract_schema(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s, email="Maria.Lopez@example.com", passive_learning=True)
    state = service.account_state(db, s, account, NOW)
    assert_matches_schema(state)
    assert state == {
        "account": {"id": account, "email_masked": "M***@example.com", "email_verified": True, "status": "active"},
        "plan": {"code": "starter", "name": "Starter", "status": "active", "interval": "month",
                 "period_start": PERIOD_START, "period_end": PERIOD_END, "cancel_at": None,
                 "scheduled_change": None, "payment_problem": False, "payment_issue": None, "upgradable": True},
        "usage": {"unit": "draft", "unit_label": "drafts", "allowance": 250, "used": 0, "reserved": 0,
                  "remaining": 250, "resets_at": PERIOD_END, "near_limit": False, "fair_use_blocked": False,
                  "per_request": 1},
        "consent": {"required_version": s.consent_version, "accepted_version": s.consent_version,
                    "passive_learning": True},
        "service": {"status": "ok", "scope": None, "message": None, "retry_at": None},
        "extension": {"min_version": s.extension_min_version},
    }
    # The optional values keep the schema when they are set.
    issue = {"kind": "renewal", "plan_code": "starter", "plan_name": "Starter"}
    fake_entitlements.set(account, status="past_due", cancel_at=PERIOD_END, payment_problem=True, payment_issue=issue,
                          scheduled_change={"plan_code": "free", "effective_at": PERIOD_END})
    changed = service.account_state(db, s, account, NOW)
    assert_matches_schema(changed)
    assert changed["plan"]["scheduled_change"] == {"plan_code": "free", "effective_at": PERIOD_END}
    assert changed["plan"]["payment_issue"] == issue
    free = make_account(db, s, verified=False, consent=False)
    fake_entitlements.set(free, plan_code="free", plan_name="Free", status="free", interval=None, allowance=25)
    free_state = service.account_state(db, s, free, NOW)
    assert_matches_schema(free_state)
    assert (free_state["plan"]["interval"], free_state["account"]["email_verified"]) == (None, False)
    assert (free_state["consent"]["accepted_version"], free_state["consent"]["passive_learning"]) == (None, False)


def test_numbers_are_the_ones_implied_by_the_ledger(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    period = fake_entitlements.period_id(account)
    commit(db, s, account)
    commit(db, s, account)
    shown = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist/stream", "chat", NOW)
    service.activate(db, shown.id, NOW)
    service.mark_exposed(db, shown.id, NOW)
    service.release(db, s, shown.id, "cancelled", NOW)  # charged
    dropped = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW)
    service.release(db, s, dropped.id, "error", NOW)  # not charged
    for _ in range(2):  # in flight
        service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW)
    grants.grant_credits(db, s, account, 7, "admin-1", "Goodwill", NOW)
    grants.debit_credits(db, s, account, 2, "admin-1", "Correction", NOW)

    with db.read() as conn:
        used = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM credit_ledger WHERE account_id = ? AND period_id = ? AND kind = 'commit'", (account, period)).fetchone()[0]
        granted = conn.execute("SELECT COALESCE(SUM(CASE kind WHEN 'admin_grant' THEN amount ELSE -amount END), 0) FROM credit_ledger WHERE account_id = ? AND period_id = ? AND kind IN ('admin_grant', 'admin_debit')", (account, period)).fetchone()[0]
        reserved = conn.execute("SELECT COALESCE(SUM(credits), 0) FROM usage_reservations WHERE account_id = ? AND period_id = ? AND state IN ('reserved', 'active')", (account, period)).fetchone()[0]
    assert (used, granted, reserved) == (3, 5, 2)
    usage = service.account_state(db, s, account, NOW + 1)["usage"]
    assert (usage["used"], usage["reserved"], usage["remaining"]) == (used, reserved, 250 + granted - used - reserved)
    assert usage["resets_at"] == PERIOD_END
    assert service.usage_for_account(db, s, account, NOW + 1) == usage
    assert service.verify_counters(db) == []


def test_near_limit_boundary(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    for _ in range(199):
        usage = commit(db, s, account)
    assert (usage["used"], usage["near_limit"]) == (199, False)  # 199 / 250
    in_flight = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW)
    assert service.account_state(db, s, account, NOW)["usage"]["near_limit"] is True  # 199 used + 1 reserved
    service.release(db, s, in_flight.id, "error", NOW)
    assert commit(db, s, account)["near_limit"] is True  # 200 / 250
    for _ in range(50):
        usage = commit(db, s, account)
    assert (usage["used"], usage["remaining"], usage["near_limit"]) == (250, 0, False)  # nothing left: not "near"
    assert grants.grant_credits(db, s, account, 1, "admin-1", "One more", NOW)["usage"]["near_limit"] is True


def test_fair_use_and_service_status(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    period = fake_entitlements.period_id(account)
    set_counter(db, account, period, cost_micros=4_499_999)
    assert service.account_state(db, s, account, NOW)["usage"]["fair_use_blocked"] is False
    set_counter(db, account, period, cost_micros=4_500_000)
    assert service.account_state(db, s, account, NOW)["usage"]["fair_use_blocked"] is True

    set_service_state(db, "ai_paused", {"paused": True, "message": "Maintenance until 14:00 UTC."})
    assert service.account_state(db, s, account, NOW)["service"] == {
        "status": "paused", "scope": "all", "message": "Maintenance until 14:00 UTC.",
        "retry_at": NOW + service.DEFAULT_PAUSE_RETRY_S}
    set_service_state(db, "ai_paused", False)
    assert service.account_state(db, s, account, NOW)["service"] == {
        "status": "ok", "scope": None, "message": None, "retry_at": None}

    day, _ = service.budget_window_keys(NOW)
    db.write(lambda c: c.execute(
        "INSERT INTO budget_counters (scope, window_key, cost_micros, limit_micros, tripped_at) VALUES ('free_day', ?, 1, 1, ?)",
        (day, NOW),
    ))
    free = make_account(db, s)
    fake_entitlements.set(free, plan_code="free", plan_name="Free", status="free", interval=None, allowance=25)
    assert service.account_state(db, s, free, NOW)["service"] == {
        "status": "paused", "scope": "free", "message": service.FREE_PAUSED_MESSAGE,
        "retry_at": NOW - NOW % 86_400 + 86_400}  # the next UTC day, when the Free budget window resets
    assert service.account_state(db, s, account, NOW)["service"]["status"] == "ok"  # paid plans continue


def test_upgradable_is_false_only_on_the_top_public_plan(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    plans = {}
    for code in ("free", "starter", "pro"):
        account = make_account(db, s)
        plan = s.plans.get(code)
        fake_entitlements.set(account, plan_code=code, plan_name=plan.name)
        plans[code] = service.account_state(db, s, account, NOW)["plan"]["upgradable"]
    assert plans == {"free": True, "starter": True, "pro": False}


def test_consent_and_account_status(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    outdated = make_account(db, s, consent_version="2020-01-01.1", passive_learning=True)
    consent = service.account_state(db, s, outdated, NOW)["consent"]
    assert consent == {"required_version": s.consent_version, "accepted_version": "2020-01-01.1", "passive_learning": False}
    revoked = make_account(db, s, consent_revoked=True)
    assert service.account_state(db, s, revoked, NOW)["consent"]["accepted_version"] is None
    assert service.account_state(db, s, make_account(db, s, status="suspended"), NOW)["account"]["status"] == "suspended"
    assert service.account_state(db, s, make_account(db, s, status="deleting"), NOW)["account"]["status"] == "deleting"
    with pytest.raises(UsageError) as caught:
        service.account_state(db, s, "00000000-0000-4000-8000-000000000000", NOW)
    assert caught.value.http_status == 404


@pytest.mark.parametrize("email, masked", [
    ("maria@example.com", "m***@example.com"),
    ("x@y.z", "x***@y.z"),
    ("first.last+tag@mail.example.org", "f***@mail.example.org"),
    ("no-at-sign", "***"),
    ("@example.com", "***"),
    ("", "***"),
])
def test_email_masking(email, masked):
    assert service.mask_email(email) == masked
