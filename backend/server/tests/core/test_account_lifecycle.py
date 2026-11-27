"""The gateway finishes interrupted account deletions in the background (account.lifecycle)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from starling_server.account import lifecycle


def _app(pool):
    return SimpleNamespace(state=SimpleNamespace(db="db", settings="settings", engine_pool=pool))


def test_resumes_pending_deletions_and_stops_on_shutdown(monkeypatch):
    calls = []

    async def fake_resume(db, settings, pool):
        calls.append((db, settings, pool))
        return ["account-1"]

    monkeypatch.setattr(lifecycle, "resume_pending_deletions", fake_resume)
    monkeypatch.setattr(lifecycle, "FIRST_RUN_DELAY_S", 0)
    app = _app("pool")

    async def scenario():
        await lifecycle.startup(app)
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        await lifecycle.shutdown(app)
        return getattr(app.state, lifecycle.TASK_ATTR)

    task = asyncio.run(scenario())
    assert calls == [("db", "settings", "pool")]
    assert task.cancelled()


def test_does_nothing_without_an_engine_pool_and_survives_failures(monkeypatch):
    calls = []

    async def failing_resume(db, settings, pool):
        calls.append(pool)
        raise RuntimeError("engine unavailable")

    monkeypatch.setattr(lifecycle, "resume_pending_deletions", failing_resume)
    monkeypatch.setattr(lifecycle, "FIRST_RUN_DELAY_S", 0)
    monkeypatch.setattr(lifecycle, "INTERVAL_S", 0.01)

    async def scenario(app):
        await lifecycle.startup(app)
        await asyncio.sleep(0.1)
        task = getattr(app.state, lifecycle.TASK_ATTR)
        still_running = not task.done()
        await lifecycle.shutdown(app)
        return still_running

    assert asyncio.run(scenario(_app(None))) is True
    assert calls == []
    assert asyncio.run(scenario(_app("pool"))) is True  # a failing run is retried, never kills the task
    assert len(calls) >= 2
