"""Races on the reservation transaction: parallel requests never over-admit (CONTRACTS section 7)."""
from __future__ import annotations

import threading

import pytest

from starling_server.usage import service
from starling_server.usage.service import Reservation, UsageError

from .conftest import NOW, body_hash, counters, make_account, new_key, record_ai_call

THREADS = 50


def run_in_parallel(count, target):
    barrier = threading.Barrier(count)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            outcome: object = target(index)
        except Exception as exc:  # UsageError is expected; anything else fails the assertions below
            outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(outcomes) == count
    return outcomes


@pytest.mark.parametrize("credits_left", [1, 3])
def test_parallel_reservations_admit_exactly_the_credits_left(database, roomy_settings, fake_entitlements, credits_left):
    db, s = database, roomy_settings
    account = make_account(db, s)
    period = fake_entitlements.period_id(account)
    fake_entitlements.set(account, allowance=10)
    for _ in range(10 - credits_left):  # spend the rest through the real commit path
        spent = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW)
        service.activate(db, spent.id, NOW)
        record_ai_call(db, spent.id)
        service.finish(db, s, spent.id, True, "{}", NOW)

    outcomes = run_in_parallel(THREADS, lambda i: service.reserve(
        db, s, account, new_key(), body_hash({"n": i}), "/api/v1/assist/stream", "chat", NOW + 1))

    wins = [o for o in outcomes if isinstance(o, Reservation)]
    losses = [o for o in outcomes if isinstance(o, UsageError)]
    assert len(wins) == credits_left
    assert len(losses) == THREADS - credits_left
    assert {error.code for error in losses} == {"limit_reached"}
    c = counters(db, account, period)
    assert (c["committed"], c["reserved"]) == (10 - credits_left, credits_left)
    with db.read() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM usage_reservations WHERE account_id = ? AND state = 'reserved'", (account,)
        ).fetchone()[0]
    assert live == credits_left
    assert service.verify_counters(db) == []


def test_parallel_duplicates_of_one_key_reserve_once(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account, key = make_account(db, s), new_key()
    outcomes = run_in_parallel(20, lambda i: service.reserve(
        db, s, account, key, body_hash(), "/api/v1/assist/stream", "chat", NOW))
    assert len([o for o in outcomes if isinstance(o, Reservation)]) == 1
    assert sorted(o.code for o in outcomes if isinstance(o, UsageError)) == ["duplicate_request"] * 19
    assert counters(db, account, fake_entitlements.period_id(account))["reserved"] == 1
    assert service.verify_counters(db) == []
