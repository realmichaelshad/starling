"""Idempotency keys and replay (CONTRACTS section 6.5)."""
from __future__ import annotations

import pytest

from starling_server.usage import service
from starling_server.usage.service import Replay, Reservation, UsageError

from .conftest import (
    NOW,
    body_hash,
    counters,
    ledger,
    make_account,
    new_key,
    record_ai_call,
    reservation,
    with_plan_limits,
)

RESULT = '{"ok": true, "answer": "A draft"}'
ROUTE = "/api/v1/assist"


def start(db, s, account, key, body=None, now=NOW):
    return service.reserve(db, s, account, key, body_hash(body), ROUTE, "context", now)


def refused(db, s, account, key, body=None, now=NOW) -> UsageError:
    with pytest.raises(UsageError) as caught:
        start(db, s, account, key, body, now)
    return caught.value


def charge(db, s, account, key, now=NOW, result=RESULT) -> Reservation:
    attempt = start(db, s, account, key, now=now)
    assert isinstance(attempt, Reservation)
    service.activate(db, attempt.id, now)
    record_ai_call(db, attempt.id, now=now)
    service.finish(db, s, attempt.id, True, result, now)
    return attempt


def test_same_key_with_a_different_body_is_a_mismatch(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account, key = make_account(db, s), new_key()
    first = start(db, s, account, key, {"text": "one", "mode": "context"})
    err = refused(db, s, account, key, {"text": "two", "mode": "context"})
    assert (err.http_status, err.code) == (409, "idempotency_mismatch")
    # Control: the same body with its keys in another order is the same request.
    assert refused(db, s, account, key, {"mode": "context", "text": "one"}).code == "duplicate_request"
    service.release(db, s, first.id, "error", NOW)
    assert refused(db, s, account, key, {"text": "two", "mode": "context"}).code == "idempotency_mismatch"


def test_committed_request_replays_without_a_second_charge(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account, key = make_account(db, s), new_key()
    period = fake_entitlements.period_id(account)
    first = charge(db, s, account, key)
    before = counters(db, account, period)

    again = start(db, s, account, key, now=NOW + 5)
    assert isinstance(again, Replay)
    assert again.result_json == RESULT
    assert (again.usage["used"], again.usage["reserved"], again.usage["remaining"]) == (1, 0, 249)
    assert len(ledger(db, account, "commit")) == 1
    assert counters(db, account, period) == before
    assert reservation(db, first.id)["state"] == "committed"
    assert service.replay(db, account, key, NOW + 6) == Replay(RESULT, again.usage)

    # The idempotency lookup comes first: a stored result replays even for a suspended account.
    db.write(lambda c: c.execute("UPDATE accounts SET status = 'suspended' WHERE id = ?", (account,)))
    assert isinstance(start(db, s, account, key, now=NOW + 7), Replay)
    assert refused(db, s, account, new_key(), now=NOW + 7).code == "account_suspended"


def test_in_flight_duplicate_is_refused(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account, key = make_account(db, s), new_key()
    first = start(db, s, account, key)
    err = refused(db, s, account, key)
    assert (err.http_status, err.code) == (409, "duplicate_request")
    service.activate(db, first.id, NOW)
    assert refused(db, s, account, key).code == "duplicate_request"
    assert counters(db, account, fake_entitlements.period_id(account))["reserved"] == 1
    assert isinstance(start(db, s, account, new_key()), Reservation)  # control: another key is another request


def test_released_key_retries_as_a_new_attempt(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account, key = make_account(db, s), new_key()
    period = fake_entitlements.period_id(account)
    first = start(db, s, account, key)
    service.release(db, s, first.id, "timeout", NOW)

    retry = start(db, s, account, key, now=NOW + 1)
    assert isinstance(retry, Reservation) and retry.id != first.id
    archived = reservation(db, first.id)  # the earlier attempt is kept as history
    assert (archived["state"], archived["release_reason"]) == ("released", "timeout")
    assert archived["idempotency_key"] != key
    row = reservation(db, retry.id)
    assert (row["state"], row["created_at"], row["release_reason"]) == ("reserved", NOW + 1, None)
    assert counters(db, account, period)["reserved"] == 1
    service.activate(db, retry.id, NOW + 1)
    record_ai_call(db, retry.id)
    service.finish(db, s, retry.id, True, RESULT, NOW + 2)
    assert len(ledger(db, account, "commit")) == 1
    assert service.verify_counters(db) == []


def test_expired_key_retries_with_a_new_reservation_id(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account, key = make_account(db, s), new_key()
    first = start(db, s, account, key)
    service.activate(db, first.id, NOW)
    service.mark_exposed(db, first.id, NOW + 1)
    assert service.sweep_expired(db, s, NOW + 300) == [first.id]

    retry = start(db, s, account, key, now=NOW + 301)
    assert isinstance(retry, Reservation) and retry.id != first.id
    service.activate(db, retry.id, NOW + 301)
    record_ai_call(db, retry.id)
    usage = service.finish(db, s, retry.id, True, RESULT, NOW + 302)
    assert usage["used"] == 1  # the first attempt died past its wall: released although it showed text
    assert [row["reservation_id"] for row in ledger(db, account, "commit")] == [retry.id]
    assert service.verify_counters(db) == []


def test_charged_request_without_a_stored_result_retries(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account, key = make_account(db, s), new_key()
    first = start(db, s, account, key)
    service.activate(db, first.id, NOW)
    service.mark_exposed(db, first.id, NOW)
    service.release(db, s, first.id, "cancelled", NOW + 1)  # cancelled after text was shown: charged
    assert reservation(db, first.id)["state"] == "committed"
    assert service.replay(db, account, key, NOW + 2) is None
    retry = start(db, s, account, key, now=NOW + 2)
    assert isinstance(retry, Reservation) and retry.id != first.id


def test_replay_expires_after_fifteen_minutes(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account, key = make_account(db, s), new_key()
    charge(db, s, account, key)
    assert isinstance(service.replay(db, account, key, NOW + 899), Replay)
    assert isinstance(start(db, s, account, key, now=NOW + 899), Replay)
    assert service.replay(db, account, key, NOW + 900) is None
    retry = start(db, s, account, key, now=NOW + 900)
    assert isinstance(retry, Reservation)


def test_keys_are_scoped_per_account(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    owner, other, key = make_account(db, s), make_account(db, s), new_key()
    charge(db, s, owner, key)
    assert service.replay(db, other, key, NOW) is None
    assert isinstance(start(db, s, other, key), Reservation)
    assert service.replay(db, owner, new_key(), NOW) is None


def test_retries_keep_services_columns_and_count_toward_rate_limits(database, settings, fake_entitlements):
    db = database
    s = with_plan_limits(settings, starter={"rate_per_minute": 2, "rate_per_day": 100})
    account, key = make_account(db, s), new_key()
    first = start(db, s, account, key)
    service.activate(db, first.id, NOW)
    record_ai_call(db, first.id, cost_micros=900, now=NOW + 1)
    service.release(db, s, first.id, "error", NOW + 1)

    retry = start(db, s, account, key, now=NOW + 2)
    old = reservation(db, first.id)
    assert (old["state"], old["ai_calls"], old["cost_micros"], old["heartbeat_at"]) == ("released", 1, 900, NOW + 1)
    fresh = reservation(db, retry.id)
    assert (fresh["ai_calls"], fresh["cost_micros"], fresh["heartbeat_at"]) == (0, 0, None)
    service.release(db, s, retry.id, "error", NOW + 3)

    err = refused(db, s, account, key, now=NOW + 4)  # both attempts count in this minute
    assert (err.code, err.retry_after_s) == ("rate_limited", 56)
    # Archived keys are internal: a client cannot address one.
    with pytest.raises(UsageError) as caught:
        service.reserve(db, s, account, f"{key}#{first.id}", body_hash(), ROUTE, "context", NOW + 61)
    assert caught.value.http_status == 400
    assert service.verify_counters(db) == []
