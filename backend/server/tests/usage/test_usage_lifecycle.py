"""usage.lifecycle: the sweeper settles overdue reservations and cancels their engine requests."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from starling_server.usage import lifecycle, service

from .conftest import NOW, body_hash, make_account, new_key, reservation


class FakePool:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, int, str]] = []

    async def cancel(self, account_id: str, tenant_seq: int, request_id: str) -> None:
        self.cancelled.append((account_id, tenant_seq, request_id))


class BrokenPool:
    async def cancel(self, account_id: str, tenant_seq: int, request_id: str) -> None:
        raise RuntimeError("engine is gone")


def tenant_seq(db, account_id: str) -> int:
    with db.read() as conn:
        return int(conn.execute("SELECT tenant_seq FROM accounts WHERE id = ?", (account_id,)).fetchone()[0])


def test_sweeper_task_expires_overdue_reservations_and_cancels_engines(database, roomy_settings, fake_entitlements, monkeypatch):
    db, s = database, roomy_settings
    account = make_account(db, s)
    past = int(time.time()) - 1_000
    monkeypatch.setattr(lifecycle, "SWEEP_INTERVAL_S", 0.05)
    made: dict[str, service.Reservation] = {}
    requests_exist = threading.Event()
    sweep_once = lifecycle.sweep_once

    async def sweep_once_the_requests_exist(app, now=None):
        # These requests are born overdue, so a sweep that landed between `reserve` and `activate`
        # expired one before the test had finished making it (seen on a CI machine). The sweeper
        # still runs on its own clock; it finds nothing until all three requests are in place.
        return await sweep_once(app, now) if requests_exist.is_set() else []

    monkeypatch.setattr(lifecycle, "sweep_once", sweep_once_the_requests_exist)

    def begin_requests() -> None:
        # While the process is running (startup settles whatever was live BEFORE it: see
        # test_usage_restart.py).
        made["exposed"] = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist/stream", "chat", past)
        service.activate(db, made["exposed"].id, past)
        service.mark_exposed(db, made["exposed"].id, past)
        made["silent"] = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist/stream", "chat", past)
        service.activate(db, made["silent"].id, past)
        made["fresh"] = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", int(time.time()))
        requests_exist.set()

    async def scenario() -> list[tuple[str, int, str]]:
        pool = FakePool()
        app = SimpleNamespace(state=SimpleNamespace(db=db, settings=s, engine_pool=pool))
        await lifecycle.startup(app)
        await asyncio.to_thread(begin_requests)
        for _ in range(300):
            if len(pool.cancelled) >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)  # several more sweeps: nothing is cancelled twice
        task = app.state.usage_sweeper
        await lifecycle.shutdown(app)
        assert task.done() and app.state.usage_sweeper is None
        return pool.cancelled

    cancelled = asyncio.run(scenario())
    exposed, silent, fresh = made["exposed"], made["silent"], made["fresh"]
    seq = tenant_seq(db, account)
    assert sorted(cancelled) == sorted([(account, seq, exposed.id), (account, seq, silent.id)])
    assert (reservation(db, exposed.id)["state"], reservation(db, silent.id)["state"]) == ("expired", "expired")
    assert reservation(db, fresh.id)["state"] == "reserved"  # control: not overdue yet
    assert service.account_state(db, s, account)["usage"]["used"] == 0  # overdue: released, exposed or not
    assert service.verify_counters(db) == []


def test_sweep_without_a_pool_and_with_a_failing_cancel(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    first = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW)
    second = service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", NOW + 1_000)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace(db=db, settings=s))
        await lifecycle.shutdown(app)  # never started: nothing to stop
        assert await lifecycle.sweep_once(app, NOW + 300) == [first.id]  # no engine pool attached
        app.state.engine_pool = BrokenPool()
        assert await lifecycle.sweep_once(app, NOW + 1_300) == [second.id]  # a failed cancel is logged only

    asyncio.run(scenario())
    assert service.verify_counters(db) == []


def test_app_lifespan_starts_and_stops_the_sweeper(app):
    with TestClient(app):
        task = app.state.usage_sweeper
        assert isinstance(task, asyncio.Task) and not task.done()
    assert app.state.usage_sweeper is None and task.done()
