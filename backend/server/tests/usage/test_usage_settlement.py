"""Commit, release, exposure, double finish and expiry (CONTRACTS section 7)."""
from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from starling_server.usage import service
from starling_server.usage.service import UsageError

from .conftest import NOW, body_hash, counters, ledger, make_account, new_key, record_ai_call, reservation

RESULT = '{"ok": true, "answer": "A draft"}'


@pytest.fixture
def ctx(database, roomy_settings, fake_entitlements):
    account = make_account(database, roomy_settings)
    return database, roomy_settings, account, fake_entitlements.period_id(account)


def start(db, s, account, now=NOW, activate=True):
    attempt = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist/stream", "chat", now)
    if activate:
        service.activate(db, attempt.id, now)
    return attempt


def test_usable_result_with_ai_commits(ctx):
    db, s, account, period = ctx
    attempt = start(db, s, account)
    assert (reservation(db, attempt.id)["state"], reservation(db, attempt.id)["heartbeat_at"]) == ("active", NOW)
    record_ai_call(db, attempt.id)
    usage = service.finish(db, s, attempt.id, True, RESULT, NOW + 5)
    assert (usage["used"], usage["reserved"], usage["remaining"]) == (1, 0, 249)
    row = reservation(db, attempt.id)
    assert (row["state"], row["result_json"], row["result_expires_at"], row["finished_at"]) == (
        "committed", RESULT, NOW + 905, NOW + 5)
    [entry] = ledger(db, account, "commit")
    assert (entry["amount"], entry["reservation_id"], entry["period_id"]) == (1, attempt.id, period)
    assert (counters(db, account, period)["committed"], counters(db, account, period)["reserved"]) == (1, 0)


def test_usable_result_without_ai_is_released(ctx):
    db, s, account, period = ctx
    attempt = start(db, s, account)
    usage = service.finish(db, s, attempt.id, True, RESULT, NOW + 1)
    row = reservation(db, attempt.id)
    assert (row["state"], row["release_reason"], row["result_json"]) == ("released", "no_ai", None)
    assert ledger(db, account) == []
    assert (usage["used"], usage["reserved"], usage["remaining"]) == (0, 0, 250)


def test_usable_result_is_not_charged_when_every_ai_call_failed(ctx):
    """A provider outage must never cost the customer a credit, whatever the engine answered."""
    db, s, account, period = ctx
    attempt = start(db, s, account)
    record_ai_call(db, attempt.id, cost_micros=0, status="error")
    record_ai_call(db, attempt.id, cost_micros=0, status="refused")
    assert reservation(db, attempt.id)["ai_calls"] == 2  # the attempt counter cannot tell these from successes
    usage = service.finish(db, s, attempt.id, True, RESULT, NOW + 1)
    row = reservation(db, attempt.id)
    assert (row["state"], row["release_reason"], row["result_json"]) == ("released", "no_ai", None)
    assert ledger(db, account) == []
    assert (usage["used"], usage["reserved"], usage["remaining"]) == (0, 0, 250)

    # Negative control: one successful call among the failures is charged as before.
    second = start(db, s, account, now=NOW + 10)
    record_ai_call(db, second.id, cost_micros=0, status="error", now=NOW + 10)
    record_ai_call(db, second.id, now=NOW + 11)
    usage = service.finish(db, s, second.id, True, RESULT, NOW + 12)
    assert reservation(db, second.id)["state"] == "committed"
    assert (usage["used"], usage["remaining"]) == (1, 249)


def test_unusable_result_is_released_and_spend_is_recorded(ctx):
    db, s, account, period = ctx
    spent = start(db, s, account)
    record_ai_call(db, spent.id, cost_micros=2_500)
    service.finish(db, s, spent.id, False, RESULT, NOW + 1)
    assert (reservation(db, spent.id)["state"], reservation(db, spent.id)["release_reason"]) == ("released", "unusable")
    assert (counters(db, account, period)["released_with_spend"], counters(db, account, period)["cost_micros"]) == (1, 2_500)
    # Control: a release without spend does not count as released with spend.
    unspent = start(db, s, account)
    service.finish(db, s, unspent.id, False, None, NOW + 2)
    c = counters(db, account, period)
    assert (c["released_with_spend"], c["committed"], c["reserved"]) == (1, 0, 0)
    assert ledger(db, account) == []


def test_cancel_after_exposure_commits_and_before_exposure_releases(ctx):
    db, s, account, period = ctx
    shown = start(db, s, account)
    service.mark_exposed(db, shown.id, NOW + 2)
    usage = service.release(db, s, shown.id, "cancelled", NOW + 3)
    assert (reservation(db, shown.id)["state"], usage["used"]) == ("committed", 1)
    assert reservation(db, shown.id)["result_json"] is None
    hidden = start(db, s, account)
    service.release(db, s, hidden.id, "cancelled", NOW + 4)
    assert (reservation(db, hidden.id)["state"], reservation(db, hidden.id)["release_reason"]) == ("released", "cancelled")
    assert [row["reservation_id"] for row in ledger(db, account, "commit")] == [shown.id]
    assert (counters(db, account, period)["committed"], counters(db, account, period)["reserved"]) == (1, 0)


def test_a_preview_that_ends_unusable_is_released(ctx):
    # A preview streamed, then Starling withheld the draft (or the engine failed): never charged.
    db, s, account, period = ctx
    attempt = start(db, s, account)
    service.mark_exposed(db, attempt.id, NOW + 1)
    record_ai_call(db, attempt.id)
    service.finish(db, s, attempt.id, False, '{"ok": true, "withheld": true}', NOW + 2)
    row = reservation(db, attempt.id)
    assert (row["state"], row["release_reason"]) == ("released", "unusable")
    assert ledger(db, account, "commit") == []
    assert counters(db, account, period)["released_with_spend"] == 1  # what the AI cost is still recorded
    assert service.verify_counters(db) == []


def test_a_customer_stop_after_text_commits_without_storing_the_result(ctx):
    db, s, account, _ = ctx
    attempt = start(db, s, account)
    service.mark_exposed(db, attempt.id, NOW + 1)
    service.finish(db, s, attempt.id, False, '{"ok": false, "cancelled": true}', NOW + 2, customer_stopped=True)
    row = reservation(db, attempt.id)
    assert (row["state"], row["result_json"]) == ("committed", None)
    assert len(ledger(db, account, "commit")) == 1
    assert service.settled_as(db, attempt.id) == "committed"


def test_a_customer_stop_before_any_text_is_released(ctx):
    db, s, account, _ = ctx
    attempt = start(db, s, account)
    service.finish(db, s, attempt.id, False, '{"ok": false, "cancelled": true}', NOW + 2, customer_stopped=True)
    assert reservation(db, attempt.id)["state"] == "released" and ledger(db, account, "commit") == []


@pytest.mark.parametrize("reason", ["engine_error", "timeout", "server_error", "engine_unavailable", "shutdown", "busy"])
def test_a_failure_on_our_side_after_text_is_released(ctx, reason):
    db, s, account, _ = ctx
    attempt = start(db, s, account)
    service.mark_exposed(db, attempt.id, NOW + 1)
    service.release(db, s, attempt.id, reason, NOW + 2)
    row = reservation(db, attempt.id)
    assert (row["state"], row["release_reason"]) == ("released", reason)
    assert ledger(db, account, "commit") == []


@pytest.mark.parametrize("reason", service.CUSTOMER_ENDED)
def test_the_customer_ending_it_after_text_commits(ctx, reason):
    db, s, account, _ = ctx
    attempt = start(db, s, account)
    service.mark_exposed(db, attempt.id, NOW + 1)
    service.release(db, s, attempt.id, reason, NOW + 2)
    assert reservation(db, attempt.id)["state"] == "committed" and len(ledger(db, account, "commit")) == 1


def test_a_usable_stream_whose_ai_call_is_still_being_recorded_commits(ctx):
    # The services process records a streamed call just after its last line; the final can win.
    db, s, account, _ = ctx
    attempt = start(db, s, account)
    db.write(lambda conn: conn.execute(
        "INSERT INTO ai_calls (account_id, reservation_id, bucket, lane, model, status, started_at) "
        "VALUES (?, ?, 'press', 'quality', 'test-model', 'cancelled', ?)", (account, attempt.id, NOW)))
    service.mark_exposed(db, attempt.id, NOW + 1)
    assert service.finish(db, s, attempt.id, True, RESULT, NOW + 2)["used"] == 1
    # ...but an unrecorded call never turns an unusable ending into a charge.
    other = start(db, s, account)
    db.write(lambda conn: conn.execute(
        "INSERT INTO ai_calls (account_id, reservation_id, bucket, lane, model, status, started_at) "
        "VALUES (?, ?, 'press', 'quality', 'test-model', 'cancelled', ?)", (account, other.id, NOW)))
    service.mark_exposed(db, other.id, NOW + 1)
    assert service.finish(db, s, other.id, False, None, NOW + 2)["used"] == 1


def test_double_finish_changes_nothing(ctx):
    db, s, account, period = ctx
    attempt = start(db, s, account)
    record_ai_call(db, attempt.id)
    first = service.finish(db, s, attempt.id, True, RESULT, NOW + 1)
    snapshot = (reservation(db, attempt.id), ledger(db, account), counters(db, account, period))
    assert service.finish(db, s, attempt.id, False, None, NOW + 50) == first
    assert service.release(db, s, attempt.id, "error", NOW + 60) == first
    service.mark_exposed(db, attempt.id, NOW + 70)
    assert (reservation(db, attempt.id), ledger(db, account), counters(db, account, period)) == snapshot

    released = start(db, s, account)
    service.release(db, s, released.id, "error", NOW + 2)
    record_ai_call(db, released.id)  # a late metering write
    service.mark_exposed(db, released.id, NOW + 3)
    assert service.finish(db, s, released.id, True, RESULT, NOW + 4)["used"] == 1
    row = reservation(db, released.id)
    assert (row["state"], row["exposure_at"]) == ("released", None)
    assert len(ledger(db, account, "commit")) == 1


def test_a_second_commit_row_is_impossible(ctx):
    db, s, account, period = ctx
    attempt = start(db, s, account)
    record_ai_call(db, attempt.id)
    service.finish(db, s, attempt.id, True, RESULT, NOW + 1)
    insert = (
        "INSERT INTO credit_ledger (account_id, period_id, kind, amount, reservation_id, actor, reason, created_at) "
        "VALUES (?, ?, ?, 1, ?, 'test', 'test', ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.write(lambda c: c.execute(insert, (account, period, "commit", attempt.id, NOW)))
    # Control: the unique index covers commits only.
    db.write(lambda c: c.execute(insert, (account, period, "refund_adjust", attempt.id, NOW)))
    assert len(ledger(db, account, "commit")) == 1


def test_exposure_is_recorded_once(ctx):
    db, s, account, _ = ctx
    attempt = start(db, s, account)
    service.mark_exposed(db, attempt.id, NOW + 5)
    service.mark_exposed(db, attempt.id, NOW + 9)
    assert reservation(db, attempt.id)["exposure_at"] == NOW + 5


def test_activation_and_unknown_reservations(ctx):
    db, s, account, _ = ctx
    queued = start(db, s, account, activate=False)
    assert reservation(db, queued.id)["state"] == "reserved"
    service.activate(db, queued.id, NOW + 1)
    service.activate(db, queued.id, NOW + 2)  # repeat is harmless
    assert reservation(db, queued.id)["state"] == "active"
    released = start(db, s, account)
    service.release(db, s, released.id, "error", NOW)
    with pytest.raises(UsageError) as caught:
        service.activate(db, released.id, NOW + 1)
    assert (caught.value.http_status, caught.value.code) == (409, "reservation_inactive")
    for call in (
        lambda: service.activate(db, "missing", NOW),
        lambda: service.mark_exposed(db, "missing", NOW),
        lambda: service.finish(db, s, "missing", True, None, NOW),
        lambda: service.release(db, s, "missing", "error", NOW),
    ):
        with pytest.raises(UsageError) as caught:
            call()
        assert caught.value.http_status == 404


def test_expiry_releases_exposed_and_unexposed(ctx):
    db, s, account, period = ctx
    exposed = start(db, s, account)
    service.mark_exposed(db, exposed.id, NOW + 1)
    record_ai_call(db, exposed.id)
    silent = start(db, s, account)
    record_ai_call(db, silent.id, cost_micros=700)
    queued = start(db, s, account, activate=False)

    assert service.sweep_expired(db, s, NOW + 299) == []
    assert sorted(service.sweep_expired(db, s, NOW + 300)) == sorted([exposed.id, silent.id, queued.id])
    rows = {rid: reservation(db, rid) for rid in (exposed.id, silent.id, queued.id)}
    assert {row["state"] for row in rows.values()} == {"expired"}
    # A request past its wall died on Starling's side: released, even the one that showed text.
    assert (rows[exposed.id]["release_reason"], rows[silent.id]["release_reason"]) == ("expired", "expired")
    assert ledger(db, account, "commit") == []
    c = counters(db, account, period)
    assert (c["committed"], c["reserved"], c["released_with_spend"]) == (0, 0, 2)
    assert service.sweep_expired(db, s, NOW + 400) == []
    assert service.finish(db, s, silent.id, True, RESULT, NOW + 401)["used"] == 0
    assert service.verify_counters(db) == []


def test_rows_an_older_sweep_charged_still_reconcile(ctx):
    # Before the rule changed, a sweep committed exposed rows as `expired` with no release reason.
    db, s, account, period = ctx
    attempt = start(db, s, account)
    service.mark_exposed(db, attempt.id, NOW + 1)
    db.write(lambda conn: service._commit(conn, service._load(conn, attempt.id), NOW + 300, None, state="expired"))
    assert reservation(db, attempt.id)["release_reason"] is None
    assert counters(db, account, period)["committed"] == 1
    assert service.verify_counters(db) == []


def test_sweep_purges_stored_results(ctx):
    db, s, account, _ = ctx
    attempt = start(db, s, account)
    record_ai_call(db, attempt.id)
    service.finish(db, s, attempt.id, True, RESULT, NOW)
    service.sweep_expired(db, s, NOW + 899)
    assert reservation(db, attempt.id)["result_json"] == RESULT
    service.sweep_expired(db, s, NOW + 900)
    assert reservation(db, attempt.id)["result_json"] is None


def test_sweeper_skips_an_inconsistent_row(ctx):
    db, s, account, period = ctx
    broken, healthy = start(db, s, account), start(db, s, account)
    for attempt in (broken, healthy):
        service.mark_exposed(db, attempt.id, NOW + 1)
    db.write(lambda c: c.execute(
        "INSERT INTO credit_ledger (account_id, period_id, kind, amount, reservation_id, actor, created_at) "
        "VALUES (?, ?, 'commit', 1, ?, 'system', ?)",
        (account, period, broken.id, NOW),
    ))
    # Settling the broken row fails inside its savepoint (as a corrupt row would).
    db.write(lambda c: c.execute(
        f"CREATE TRIGGER broken_row BEFORE UPDATE ON usage_reservations WHEN OLD.id = '{broken.id}' "
        "BEGIN SELECT RAISE(ABORT, 'broken row'); END"
    ))
    assert service.sweep_expired(db, s, NOW + 300) == [healthy.id]
    assert reservation(db, broken.id)["state"] == "active"
    assert any(problem.get("reservation_id") == broken.id for problem in service.verify_counters(db))


def test_credit_multiplier_commits_multiple_credits(database, roomy_settings, fake_entitlements):
    db, s = database, dataclasses.replace(roomy_settings, ai_credit_multiplier=3)
    account = make_account(db, s)
    attempt = start(db, s, account)
    record_ai_call(db, attempt.id)
    usage = service.finish(db, s, attempt.id, True, RESULT, NOW + 1)
    assert (usage["used"], usage["remaining"]) == (3, 247)
    assert ledger(db, account, "commit")[0]["amount"] == 3
