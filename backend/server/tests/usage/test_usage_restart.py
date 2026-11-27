"""What a gateway restart leaves behind, and the daily check of the counters (CONTRACTS section 7).

A live reservation belongs to a request inside one gateway process. After a deploy, a crash or a
kill, nothing is running it any more, but the row would sit there until its five-minute wall and
its credits would count as in use: a customer one draft from the limit was refused by a request
that died with the old process.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from starling_server import cli
from starling_server.usage import lifecycle, service
from starling_server.usage.service import UsageError

from .conftest import NOW, body_hash, counters, make_account, new_key, record_ai_call, reservation, set_counter


def reserve(db, s, account, now=NOW, route="/api/v1/assist/stream", mode="chat"):
    return service.reserve(db, s, account, new_key(), body_hash(), route, mode, now)


def test_a_restart_settles_what_the_old_process_left_live(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    waiting = reserve(db, s, account)                       # reserved, never dispatched
    running = reserve(db, s, account)                       # dispatched, nothing shown yet
    service.activate(db, running.id, NOW)
    record_ai_call(db, running.id, cost_micros=700)
    shown = reserve(db, s, account)                         # text had reached the customer
    service.activate(db, shown.id, NOW)
    record_ai_call(db, shown.id)
    service.mark_exposed(db, shown.id, NOW)
    done = reserve(db, s, account)                          # finished normally: must not be touched
    service.activate(db, done.id, NOW)
    record_ai_call(db, done.id)
    service.finish(db, s, done.id, True, '{"ok": true, "draft": "x"}', NOW)

    settled = service.settle_orphans(db, s, NOW + 5)        # seconds later, far inside the five-minute wall

    assert sorted(settled) == sorted([waiting.id, running.id, shown.id])
    assert reservation(db, waiting.id)["state"] == "expired"
    assert reservation(db, waiting.id)["release_reason"] == "gateway_restart"
    assert reservation(db, running.id)["release_reason"] == "gateway_restart"
    assert reservation(db, shown.id)["state"] == "expired"  # died with the old process: released,
    assert reservation(db, shown.id)["release_reason"] == "gateway_restart"  # although it showed text
    assert reservation(db, done.id)["state"] == "committed"
    usage = service.account_state(db, s, account, NOW + 5)["usage"]
    assert (usage["used"], usage["reserved"]) == (1, 0)     # `done` only; nothing still "in progress"
    period = fake_entitlements.period_id(account)
    assert counters(db, account, period)["released_with_spend"] == 2  # `running` and `shown` had spent on AI
    assert service.verify_counters(db) == []
    assert service.settle_orphans(db, s, NOW + 6) == []     # nothing left: a second start changes nothing


def test_the_last_draft_is_not_held_hostage_by_a_dead_request(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    fake_entitlements.set(account, allowance=1)
    reserve(db, s, account)                                 # the old process dies holding the only credit
    with pytest.raises(UsageError) as refused:
        reserve(db, s, account, NOW + 10)
    assert refused.value.code == "limit_reached"

    service.settle_orphans(db, s, NOW + 20)

    again = reserve(db, s, account, NOW + 21)               # the customer can draft again at once
    assert again.state == "reserved"


def test_startup_settles_orphans_before_anything_else(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    left_behind = reserve(db, s, account)
    service.activate(db, left_behind.id, NOW)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace(db=db, settings=s))
        await lifecycle.startup(app)
        try:
            assert reservation(db, left_behind.id)["state"] == "expired"
        finally:
            await lifecycle.shutdown(app)

    asyncio.run(scenario())
    assert service.verify_counters(db) == []


def alert(db):
    with db.read() as conn:
        row = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (lifecycle.COUNTER_ALERT_KEY,)).fetchone()
    return None if row is None else json.loads(row["value_json"])


def test_a_counter_that_drifts_raises_the_owner_alert_and_a_clean_check_clears_it(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    finished = reserve(db, s, account)
    service.activate(db, finished.id, NOW)
    record_ai_call(db, finished.id)
    service.finish(db, s, finished.id, True, '{"ok": true, "draft": "x"}', NOW)
    period = fake_entitlements.period_id(account)
    app = SimpleNamespace(state=SimpleNamespace(db=db, settings=s))

    assert asyncio.run(lifecycle.verify_once(app, NOW)) == []
    assert alert(db) is None

    set_counter(db, account, period, committed=7)           # what a bad manual edit or a bug would do
    problems = asyncio.run(lifecycle.verify_once(app, NOW + 60))
    assert [p["field"] for p in problems] == ["committed"]
    raised = alert(db)
    assert raised["level"] == "error" and raised["count"] == 1
    assert (raised["first_at"], raised["last_at"]) == (NOW + 60, NOW + 60)
    assert "usage-verify" in raised["message"] and account not in raised["message"]  # ids stay in details
    assert raised["details"][0]["account_id"] == account

    asyncio.run(lifecycle.verify_once(app, NOW + 120))       # still wrong: the alert keeps its first time
    assert (alert(db)["first_at"], alert(db)["last_at"]) == (NOW + 60, NOW + 120)

    set_counter(db, account, period, committed=1)
    assert asyncio.run(lifecycle.verify_once(app, NOW + 180)) == []
    assert alert(db) is None


def test_the_owner_dashboard_shows_the_counter_alert(database, roomy_settings, fake_entitlements):
    from starling_server.admin import alerts

    db, s = database, roomy_settings
    account = make_account(db, s)
    set_counter(db, account, fake_entitlements.period_id(account), reserved=3)  # nothing is live: a ghost
    app = SimpleNamespace(state=SimpleNamespace(db=db, settings=s))
    asyncio.run(lifecycle.verify_once(app, NOW))

    with db.read() as conn:
        shown = [item for item in alerts.stored_alerts(conn) if item["key"] == lifecycle.COUNTER_ALERT_KEY]
    assert len(shown) == 1 and shown[0]["level"] == "error"
    assert "disagree with the ledger" in shown[0]["text"]


def test_the_verify_command_lists_every_problem(database, roomy_settings, fake_entitlements, monkeypatch, capsys):
    db, s = database, roomy_settings
    account = make_account(db, s)
    # Only this command's own lookup is replaced, and only after every command module is loaded:
    # patching `config.get_settings` itself would be captured for good by any module that is
    # imported for the first time while the patch is on (`from ..config import get_settings`).
    cli.build_parser()
    monkeypatch.setattr("starling_server.usage.commands.get_settings", lambda: s)
    assert s.control_db_path == db.path

    assert cli.main(["usage-verify"]) == 0
    assert "0 problem(s)" in capsys.readouterr().out

    set_counter(db, account, fake_entitlements.period_id(account), granted=5)
    assert cli.main(["usage-verify"]) == 1
    out = capsys.readouterr().out
    assert '"field": "granted"' in out and "1 problem(s)" in out
