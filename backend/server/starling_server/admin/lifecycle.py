"""Admin background work: owner alert emails and the daily `request_errors` prune.

`startup(app)` starts one task that runs `run_once` every 10 minutes, the first time 60 seconds after
startup: it emails new or re-fired owner alerts to ALERT_EMAIL (`alerts.notify`: at most once per alert
every 6 hours, nothing without ALERT_EMAIL) and, when a day has passed since the last prune, removes old
`request_errors` rows (`db.request_errors.prune`). Set `app.state.admin_background_jobs = False` before
startup to leave it off (tests). `shutdown(app)` lets a running pass finish, then stops the task, so the
database is never closed under it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from typing import Any

import anyio

from ..config import Settings
from ..db import Database, request_errors
from ..security import clock
from . import alerts
from .common import PRUNED_AT_KEY, as_int, read_state, write_state

log = logging.getLogger(__name__)

INTERVAL_S = 600.0
FIRST_RUN_S = 60.0
SHUTDOWN_GRACE_S = 10.0
PRUNE_EVERY_S = 86_400


def run_once(db: Database, settings: Settings, now: int) -> dict[str, Any]:
    """One pass: alert emails, then the prune when it is due. Failures are logged, never raised."""
    report: dict[str, Any] = {"notified": [], "pruned": None}
    try:
        report["notified"] = alerts.notify(db, settings, now)
    except Exception:
        log.exception("owner alert email failed")
    try:
        report["pruned"] = prune_if_due(db, now)
    except Exception:
        log.exception("request_errors prune failed")
    return report


def prune_if_due(db: Database, now: int) -> int | None:
    """Rows removed, or None when the last prune was less than a day ago."""

    def transaction(conn: sqlite3.Connection) -> int | None:
        last, _ = read_state(conn, PRUNED_AT_KEY)
        last_at = as_int(last)
        if last_at is not None and now - last_at < PRUNE_EVERY_S:
            return None
        removed = request_errors.prune(conn, now)
        write_state(conn, PRUNED_AT_KEY, int(now), now)
        return removed

    return db.write(transaction)


async def startup(app: Any) -> None:
    if not getattr(app.state, "admin_background_jobs", True):
        return
    stop = asyncio.Event()
    app.state.admin_jobs_stop = stop
    app.state.admin_jobs_task = asyncio.create_task(_run(app, stop), name="admin-jobs")


async def shutdown(app: Any) -> None:
    task: asyncio.Task | None = getattr(app.state, "admin_jobs_task", None)
    if task is None:
        return
    app.state.admin_jobs_stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_GRACE_S)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        app.state.admin_jobs_task = None


async def _run(app: Any, stop: asyncio.Event) -> None:
    delay = FIRST_RUN_S
    while not await _stopped(stop, delay):
        try:
            await anyio.to_thread.run_sync(run_once, app.state.db, app.state.settings, clock.now())
        except Exception:
            log.exception("admin background pass failed")
        delay = INTERVAL_S


async def _stopped(stop: asyncio.Event, seconds: float) -> bool:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)
    return stop.is_set()
