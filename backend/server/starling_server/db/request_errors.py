"""Refusals and failures of the extension API, aggregated for the owner dashboard.

Table `request_errors` (migration 3, CONTRACTS section 11) keeps one row per minute for each distinct
(account, route, mode, code, HTTP status, reserved) combination, with a `count`, so a burst of identical
refusals costs one row.

- `route` is the route template (`/api/v1/assist/stream`), never a concrete URL.
- `account_id` is `""` when the caller was not identified (for example a missing device token).
- `mode` is the validated assist mode, or `""`.
- `reserved` is 1 when the request already held a usage reservation (it failed while running) and 0
  when it was refused before reserving.

Failure rate over a window, for the metered routes in `METERED_ROUTES`:
`failed = SUM(count)` and `requests = reservations created in the window + SUM(count) WHERE reserved = 0`.

Recording never blocks the caller and never raises: the row is queued on the writer thread and a
failed write is only logged. Rows older than `RETENTION_S` are removed by `prune`.

Refusals are COALESCED before they reach the writer. Anybody can cause a refusal (a request with no
token is one), and the control database has a single writer that every sign-in, reservation and
webhook also waits for, with a synced commit per transaction. One write per refusal let an
unauthenticated flood stall the whole product. Now refusals are counted in memory and at most ONE
flush is ever waiting on the writer: a quiet server writes each refusal at once, exactly as before,
and a flood of any size costs one transaction per turn of the writer.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import weakref
from concurrent.futures import Future
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import Database

log = logging.getLogger(__name__)

RETENTION_S = 30 * 24 * 3600
# `/write/draft` is the website's writing page (web/write.py), metered exactly like `/api/v1/assist`.
METERED_ROUTES = ("/api/v1/assist", "/api/v1/assist/stream", "/write/draft")

_UPSERT = (
    "INSERT INTO request_errors "
    "(minute, account_id, route, mode, code, http_status, reserved, count, first_at, last_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (minute, account_id, route, mode, code, http_status, reserved) "
    "DO UPDATE SET count = count + excluded.count, last_at = MAX(last_at, excluded.last_at)"
)
# Distinct (minute, account, route, mode, code, status, reserved) rows waiting for one flush. Routes and
# codes are fixed sets, so only the number of accounts makes this grow; the cap is a backstop.
MAX_PENDING_ROWS = 5000


class _Buffer:
    """Refusals counted in memory for one database, and whether a flush is already queued."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: dict[tuple, list[int]] = {}
        self.flush_queued = False
        self.dropped = 0


_buffers: "weakref.WeakKeyDictionary[Database, _Buffer]" = weakref.WeakKeyDictionary()
_buffers_lock = threading.Lock()


def _buffer_for(db: "Database") -> _Buffer:
    with _buffers_lock:
        buffer = _buffers.get(db)
        if buffer is None:
            buffer = _buffers[db] = _Buffer()
        return buffer


def _flush(conn: sqlite3.Connection, buffer: _Buffer) -> int:
    """Runs on the writer thread: everything counted since the flush was queued, in one transaction."""
    with buffer.lock:
        batch, buffer.pending = buffer.pending, {}
        buffer.flush_queued = False  # refusals that arrive from now on queue the next flush
        dropped, buffer.dropped = buffer.dropped, 0
    if dropped:
        log.warning("request errors: %d refusals were not recorded (more than %d distinct rows were waiting)",
                    dropped, MAX_PENDING_ROWS)
    conn.executemany(_UPSERT, [(*key, count, first_at, last_at) for key, (count, first_at, last_at) in batch.items()])
    return len(batch)


def _log_failure(future: "Future[object]") -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        log.warning("could not record a request error: %s", type(exc).__name__)


def record(
    db: "Database",
    *,
    route: str,
    code: str,
    http_status: int,
    now: int,
    account_id: str | None = None,
    mode: str | None = None,
    reserved: bool = False,
) -> None:
    """Count one refusal or failure (fire and forget); at most one flush waits on the writer."""
    now = int(now)
    key = (now - now % 60, account_id or "", route, mode or "", code, int(http_status), 1 if reserved else 0)
    buffer = _buffer_for(db)
    with buffer.lock:
        slot = buffer.pending.get(key)
        if slot is not None:
            slot[0] += 1
            slot[2] = max(slot[2], now)
        elif len(buffer.pending) >= MAX_PENDING_ROWS:
            buffer.dropped += 1
        else:
            buffer.pending[key] = [1, now, now]
        if buffer.flush_queued:
            return
        buffer.flush_queued = True
    try:
        future = db.writer.submit(lambda conn: _flush(conn, buffer))
    except Exception:  # the writer is closed during shutdown
        with buffer.lock:
            buffer.flush_queued = False
        log.debug("request error not recorded: database writer unavailable")
        return
    future.add_done_callback(_log_failure)


def prune(conn: sqlite3.Connection, now: int) -> int:
    """Delete rows older than the retention window. Run inside a write transaction."""
    return conn.execute("DELETE FROM request_errors WHERE last_at < ?", (int(now) - RETENTION_S,)).rowcount
