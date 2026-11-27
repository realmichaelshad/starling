"""SQLite operations for backups and the backup status.

They run in-process (development) or inside `ops.helper` under the file owner's user (production,
where the caller is root). Everything opens existing files only (`mode=ro` / `mode=rw` URIs), so
nothing here can create a database by accident.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..db.connection import BUSY_TIMEOUT_MS

UPSERT_STATE = (
    "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at"
)


def _uri(path: Path, mode: str) -> str:
    return f"{Path(path).resolve().as_uri()}?mode={mode}"


def _connect(path: Path, mode: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_uri(path, mode), uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def snapshot_sqlite(source: Path, destination: Path) -> str:
    """Copy a live database into `destination` and return SQLite's quick_check result.

    One backup step is one read transaction, so the copy is transaction-consistent while other
    connections keep writing. The source is opened read-only first; SQLite reports some read-only
    problems only when it reads (for example a WAL database whose shared-memory file it may not
    create), so the copy is retried read-write after an OperationalError.
    """
    try:
        return _copy(source, "ro", destination)
    except sqlite3.OperationalError:
        return _copy(source, "rw", destination)


def _copy(source: Path, mode: str, destination: Path) -> str:
    destination.unlink(missing_ok=True)
    src = _connect(source, mode)
    try:
        dst = sqlite3.connect(str(destination), isolation_level=None)
        try:
            src.backup(dst)  # pages=-1: a single step
            dst.execute("PRAGMA journal_mode=DELETE")  # self-contained file, no -wal needed
            return str(dst.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            dst.close()
    finally:
        src.close()


def schema_version(snapshot: Path) -> int:
    conn = sqlite3.connect(str(snapshot))
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def write_state(db_path: Path, key: str, value: dict, updated_at: int) -> None:
    """Upsert one `service_state` row in a single short IMMEDIATE transaction."""
    conn = _connect(db_path, "rw")
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(UPSERT_STATE, (key, json.dumps(value, sort_keys=True), int(updated_at)))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def read_state(db_path: Path, key: str) -> dict | None:
    conn = _connect(db_path, "ro")
    try:
        row = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    value = json.loads(row[0])
    return value if isinstance(value, dict) else None
