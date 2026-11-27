"""Control database access: one writer thread per process, cheap thread-local readers."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from . import migrations
from .connection import connect
from .writer import DatabaseWriter

T = TypeVar("T")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
        try:
            migrations.apply(conn)
        finally:
            conn.close()
        self.writer = DatabaseWriter(path)
        self._local = threading.local()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read-only view for the current thread. Never write through it."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
        yield conn

    def write(self, fn: Callable[[sqlite3.Connection], T], timeout: float = 30.0) -> T:
        return self.writer.run(fn, timeout)

    async def write_async(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await self.writer.run_async(fn)

    def close(self) -> None:
        self.writer.close()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


__all__ = ["Database", "connect", "migrations"]
