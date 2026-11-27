"""The single writer thread that owns every write to the control database.

SQLite allows one writer at a time. Funnelling all writes of a process through one
thread (and `BEGIN IMMEDIATE`) keeps transactions short, avoids "database is locked"
storms under concurrent requests, and never blocks the asyncio event loop: async code
awaits `run_async`, which waits on a future instead of on SQLite.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Callable, TypeVar

from .connection import connect

T = TypeVar("T")
log = logging.getLogger(__name__)

_STOP = object()


class DatabaseWriter:
    def __init__(self, path: Path, name: str = "control-db-writer") -> None:
        self._path = path
        self._queue: "queue.SimpleQueue[object]" = queue.SimpleQueue()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    def _run(self) -> None:
        try:
            conn = connect(self._path)
        except BaseException as exc:  # surface the failure to the constructor
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                fn, future = item  # type: ignore[misc]
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = fn(conn)
                    conn.execute("COMMIT")
                except BaseException as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        log.exception("rollback failed")
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        finally:
            conn.close()

    def submit(self, fn: Callable[[sqlite3.Connection], T]) -> "Future[T]":
        """Queue `fn(conn)` to run inside one write transaction."""
        if self._closed:
            raise RuntimeError("database writer is closed")
        future: "Future[T]" = Future()
        self._queue.put((fn, future))
        return future

    def run(self, fn: Callable[[sqlite3.Connection], T], timeout: float = 30.0) -> T:
        return self.submit(fn).result(timeout)

    async def run_async(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.wrap_future(self.submit(fn))

    def close(self, timeout: float = 10.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        self._thread.join(timeout)
