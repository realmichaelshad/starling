"""Integration with the billing workstream's real entitlements (no fakes) for a free account."""
from __future__ import annotations

import pytest

from starling_server.usage import grants, service
from starling_server.usage.service import Reservation, UsageError

from .conftest import NOW, body_hash, make_account, new_key, record_ai_call, with_plan_limits


def commit(db, s, account, now):
    attempt = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist/stream", "chat", now)
    assert isinstance(attempt, Reservation)
    service.activate(db, attempt.id, now)
    record_ai_call(db, attempt.id, now=now)
    return attempt, service.finish(db, s, attempt.id, True, '{"ok": true}', now)


def test_free_account_with_real_entitlements(database, settings):
    db = database
    s = with_plan_limits(settings, free={"rate_per_minute": 1_000, "rate_per_day": 1_000})
    account = make_account(db, s)  # email verified at NOW - 5_000: the free period's anchor

    first, usage = commit(db, s, account, NOW)
    with db.read() as conn:
        [period] = conn.execute("SELECT * FROM entitlement_periods WHERE account_id = ?", (account,)).fetchall()
    assert (period["source"], period["plan_code"], period["allowance"], period["period_start"]) == ("free", "free", 25, NOW - 5_000)
    assert first.period_id == period["id"]
    assert (usage["allowance"], usage["used"], usage["remaining"], usage["resets_at"]) == (25, 1, 24, period["period_end"])

    state = service.account_state(db, s, account, NOW + 1)
    assert state["plan"] == {
        "code": "free", "name": "Free", "status": "free", "interval": None, "period_start": NOW - 5_000,
        "period_end": period["period_end"], "cancel_at": None, "scheduled_change": None, "payment_problem": False,
        "payment_issue": None, "upgradable": True,
    }
    assert state["usage"] == usage

    for offset in range(24):
        _, usage = commit(db, s, account, NOW + 2 + offset)
    assert (usage["used"], usage["remaining"]) == (25, 0)
    with pytest.raises(UsageError) as caught:
        service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW + 30)
    assert (caught.value.code, caught.value.usage["allowance"], caught.value.usage["remaining"]) == ("limit_reached", 25, 0)

    assert grants.grant_credits(db, s, account, 2, "admin-1", "Goodwill", NOW + 31)["usage"]["remaining"] == 2
    assert isinstance(service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW + 32), Reservation)

    day, _ = service.budget_window_keys(NOW + 33)
    db.write(lambda c: c.execute(
        "INSERT INTO budget_counters (scope, window_key, cost_micros, limit_micros, tripped_at) VALUES ('free_day', ?, 1, 1, ?)",
        (day, NOW + 33),
    ))
    with pytest.raises(UsageError) as caught:
        service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW + 34)
    assert (caught.value.http_status, caught.value.code) == (503, "service_paused")
    assert service.account_state(db, s, account, NOW + 34)["service"]["status"] == "paused"
    assert service.verify_counters(db) == []
