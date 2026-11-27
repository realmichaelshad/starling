"""verify_counters(): the nightly check finds counters that disagree with the ledger and reservations."""
from __future__ import annotations

import pytest

from starling_server.usage import grants, service

from .conftest import NOW, body_hash, make_account, new_key, record_ai_call


@pytest.fixture
def history(database, roomy_settings, fake_entitlements):
    """Two commits, one release, one request in flight and a grant."""
    db, s = database, roomy_settings
    account = make_account(db, s)
    ids = {}
    for name in ("commit_a", "commit_b", "released", "in_flight"):
        attempt = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW)
        ids[name] = attempt.id
        if name.startswith("commit"):
            service.activate(db, attempt.id, NOW)
            record_ai_call(db, attempt.id)
            service.finish(db, s, attempt.id, True, "{}", NOW)
        elif name == "released":
            service.release(db, s, attempt.id, "error", NOW)
    grants.grant_credits(db, s, account, 3, "admin-1", "Goodwill", NOW)
    return db, account, fake_entitlements.period_id(account), ids


def test_consistent_history_has_no_mismatches(history):
    db, *_ = history
    assert service.verify_counters(db) == []


@pytest.mark.parametrize("assignment, field, counter, expected", [
    ("committed = committed + 1", "committed", 3, 2),
    ("reserved = reserved - 1", "reserved", 0, 1),
    ("granted = granted + 2", "granted", 5, 3),
])
def test_counter_corruption_is_found(history, assignment, field, counter, expected):
    db, account, period, _ = history
    db.write(lambda c: c.execute(f"UPDATE usage_counters SET {assignment} WHERE account_id = ?", (account,)))
    assert service.verify_counters(db) == [
        {"account_id": account, "period_id": period, "field": field, "counter": counter, "expected": expected}
    ]


def test_deleted_commit_row_is_found(history):
    db, account, period, ids = history
    db.write(lambda c: c.execute("DELETE FROM credit_ledger WHERE reservation_id = ?", (ids["commit_a"],)))
    problems = service.verify_counters(db)
    assert {"account_id": account, "period_id": period, "field": "committed", "counter": 2, "expected": 1} in problems
    assert {"account_id": account, "period_id": period, "field": "commit_ledger", "reservation_id": ids["commit_a"],
            "counter": 0, "expected": 1} in problems
    assert len(problems) == 2


def test_commit_row_for_an_uncharged_reservation_is_found(history):
    db, account, period, ids = history

    def charge_released(conn):
        conn.execute(
            "INSERT INTO credit_ledger (account_id, period_id, kind, amount, reservation_id, actor, created_at) "
            "VALUES (?, ?, 'commit', 1, ?, 'system', ?)",
            (account, period, ids["released"], NOW),
        )
        conn.execute("UPDATE usage_counters SET committed = committed + 1 WHERE account_id = ?", (account,))

    db.write(charge_released)  # counters agree with the ledger, but the reservation was released
    assert service.verify_counters(db) == [
        {"account_id": account, "period_id": period, "field": "commit_ledger", "reservation_id": ids["released"],
         "counter": 1, "expected": 0}
    ]


def test_missing_counter_row_is_found(history):
    db, account, period, _ = history
    db.write(lambda c: c.execute("DELETE FROM usage_counters WHERE account_id = ?", (account,)))
    problems = service.verify_counters(db)
    assert sorted((p["field"], p["counter"], p["expected"]) for p in problems) == [
        ("committed", 0, 2), ("granted", 0, 3), ("reserved", 0, 1)]
