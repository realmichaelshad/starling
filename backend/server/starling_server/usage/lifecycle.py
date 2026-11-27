"""Usage background work: the reservation sweeper and the counter check (CONTRACTS section 7).

`startup(app)` binds the app's settings for `service.replay`, settles the reservations a previous
gateway process left behind (`service.settle_orphans`), and starts a task that sweeps every
`SWEEP_INTERVAL_S` seconds. Overdue reservations are settled by `service.sweep_expired` and, when
an engine pool is attached (`app.state.engine_pool`), their engine requests are cancelled.

The same task runs `service.verify_counters` once at startup and then every `VERIFY_INTERVAL_S`.
The counters are what every limit is decided from, and the ledger and the reservations are what
they must add up to. A disagreement raises the owner alert `alert:usage:counters` (dashboard and
alert email); a clean run clears it. `starling usage-verify` prints the same check on demand.

`shutdown(app)` lets a running sweep finish, then stops the task, so the database is never closed
under a sweep.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from ..db import Database
from . import service

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 30.0
VERIFY_INTERVAL_S = 86_400.0
COUNTER_ALERT_KEY = "alert:usage:counters"
ALERT_SAMPLE = 10  # problems kept in the alert's details; the command lists all of them
SHUTDOWN_GRACE_S = 10.0
CANCEL_TIMEOUT_S = 10.0
_ID_CHUNK = 500


async def startup(app: Any) -> None:
    state = app.state
    service.bind_settings(state.db, state.settings)
    try:
        orphans = await asyncio.to_thread(service.settle_orphans, state.db, state.settings)
    except Exception:
        # Starting without this costs at most the five-minute wall; not starting costs everything.
        log.exception("could not settle reservations left by a previous gateway process")
    else:
        if orphans:
            log.warning("settled %d reservation(s) left live by a previous gateway process", len(orphans))
    stop = asyncio.Event()
    state.usage_sweeper_stop = stop
    state.usage_sweeper = asyncio.create_task(_run(app, stop), name="usage-sweeper")


async def shutdown(app: Any) -> None:
    task: asyncio.Task | None = getattr(app.state, "usage_sweeper", None)
    stop: asyncio.Event | None = getattr(app.state, "usage_sweeper_stop", None)
    if task is None:
        return
    if stop is not None:
        stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_GRACE_S)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        app.state.usage_sweeper = None


async def _run(app: Any, stop: asyncio.Event) -> None:
    next_verify = 0.0  # the first pass checks the counters too
    while not stop.is_set():
        try:
            await sweep_once(app)
        except Exception:
            log.exception("usage sweep failed")
        if time.monotonic() >= next_verify:
            next_verify = time.monotonic() + VERIFY_INTERVAL_S
            try:
                await verify_once(app)
            except Exception:
                log.exception("usage counter check failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), SWEEP_INTERVAL_S)


async def verify_once(app: Any, now: int | None = None) -> list[dict[str, Any]]:
    """Check the counters against the ledger; raise or clear the owner alert. Returns the problems."""
    db: Database = app.state.db
    problems = await asyncio.to_thread(service.verify_counters, db)
    await asyncio.to_thread(record_verification, db, problems, int(time.time()) if now is None else int(now))
    return problems


def record_verification(db: Database, problems: list[dict[str, Any]], now: int) -> None:
    """Store the alert for a failed check (`admin.alerts` shows and emails it) or remove it."""

    def transaction(conn) -> None:
        if not problems:
            conn.execute("DELETE FROM service_state WHERE key = ?", (COUNTER_ALERT_KEY,))
            return
        previous = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (COUNTER_ALERT_KEY,)).fetchone()
        first_at = now
        if previous is not None:
            with contextlib.suppress(ValueError, TypeError):
                first_at = int(json.loads(previous["value_json"]).get("first_at") or now)
        periods = {(problem["account_id"], problem["period_id"]) for problem in problems}
        value = {
            "source": "usage",
            "level": "error",
            # Metadata only: counts here, ids in `details`, never customer content.
            "message": (
                f"Usage counters disagree with the ledger in {len(problems)} place(s) across "
                f"{len(periods)} account period(s). Limits are decided from these counters. "
                "Run `starling usage-verify` for the list."
            ),
            "details": problems[:ALERT_SAMPLE],
            "count": len(problems),
            "first_at": first_at,
            "last_at": now,
        }
        conn.execute(
            "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (COUNTER_ALERT_KEY, json.dumps(value, sort_keys=True), now),
        )

    db.write(transaction)
    if problems:
        log.error("usage counters disagree with the ledger in %d place(s)", len(problems))


async def sweep_once(app: Any, now: int | None = None) -> list[str]:
    """Settle overdue reservations and cancel their engine requests; returns the expired ids."""
    db: Database = app.state.db
    expired = await asyncio.to_thread(service.sweep_expired, db, app.state.settings, now)
    pool = getattr(app.state, "engine_pool", None)
    if not expired or pool is None:
        return expired
    for reservation_id, account_id, tenant_seq in await asyncio.to_thread(_engine_targets, db, expired):
        try:
            await asyncio.wait_for(pool.cancel(account_id, tenant_seq, reservation_id), CANCEL_TIMEOUT_S)
        except Exception:
            log.warning("engine cancel failed for expired reservation %s", reservation_id, exc_info=True)
    return expired


def _engine_targets(db: Database, reservation_ids: list[str]) -> list[tuple[str, str, int]]:
    targets: list[tuple[str, str, int]] = []
    with db.read() as conn:
        for start in range(0, len(reservation_ids), _ID_CHUNK):
            chunk = reservation_ids[start:start + _ID_CHUNK]
            rows = conn.execute(
                "SELECT r.id, r.account_id, a.tenant_seq FROM usage_reservations r "
                f"JOIN accounts a ON a.id = r.account_id WHERE r.id IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
            targets.extend((row["id"], row["account_id"], int(row["tenant_seq"])) for row in rows)
    return targets
