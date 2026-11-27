"""Last scheduled backup run for the owner dashboard: `service_state` key `backup:last`.

    {"at": <unix seconds>, "ok": <bool>, "archive": <file name or null>,
     "size_bytes": <int or null>, "verified": <bool>, "error": <short text or null>}

Written after every scheduled run in one short transaction. With OS isolation the supervisor is
root, so the write runs in `ops.helper` as the gateway user (runas.py): root never opens the
control database, and any -wal or -shm file SQLite creates belongs to starling-gw in the
starling-db group. Owners and modes of control.db, -wal and -shm are then checked against
CONTRACTS 2.1 (reported, never changed here).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Callable

from ..config import Settings
from . import runas
from .common import BackupError
from .sqlite_io import read_state, write_state

log = logging.getLogger(__name__)

STATUS_KEY = "backup:last"
MAX_ERROR_CHARS = 200
CONTROL_DB_OWNER = (runas.GATEWAY_UID, runas.DB_GROUP)
CONTROL_DB_MODE = 0o660


def short_error(error: str | None) -> str | None:
    text = str(error or "").strip()
    return text.splitlines()[0][:MAX_ERROR_CHARS] if text else None


def build_status(
    *,
    at: float,
    ok: bool,
    archive: Path | str | None = None,
    size_bytes: int | None = None,
    verified: bool = False,
    error: str | None = None,
) -> dict:
    return {
        "at": int(at),
        "ok": bool(ok),
        "archive": Path(archive).name if archive else None,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
        "verified": bool(verified),
        "error": short_error(error),
    }


def record_status(settings: Settings, status: dict) -> bool:
    """Store the status; returns False (and logs why) when it could not be written."""
    path = settings.control_db_path
    if not path.is_file():
        log.warning("backup status not recorded: no control database at %s yet", path)
        return False
    try:
        if runas.privileged(settings):
            code, _, message = runas.run_helper(
                runas.control_identity(), ["state-write", str(path), STATUS_KEY], stdin=json.dumps(status).encode("utf-8")
            )
            if code != 0:
                log.warning("backup status not recorded: %s", message or f"helper exit status {code}")
                return False
        else:
            write_state(path, STATUS_KEY, status, status["at"])
    except (sqlite3.Error, OSError, subprocess.SubprocessError) as exc:
        log.warning("backup status not recorded: %s", exc)
        return False
    for problem in ownership_problems(settings):
        log.error("control database files differ from CONTRACTS 2.1: %s", problem)
    return True


def read_status(settings: Settings) -> dict | None:
    path = settings.control_db_path
    if not path.is_file():
        return None
    try:
        if runas.privileged(settings):
            code, out, message = runas.run_helper(runas.control_identity(), ["state-read", str(path), STATUS_KEY])
            if code != 0:
                raise BackupError(f"cannot read the backup status: {message or f'helper exit status {code}'}")
            value = json.loads(out.decode("utf-8") or "null")
        else:
            value = read_state(path, STATUS_KEY)
    except (sqlite3.Error, OSError, subprocess.SubprocessError, ValueError) as exc:
        raise BackupError(f"cannot read the backup status: {exc}") from exc
    return value if isinstance(value, dict) else None


def ownership_problems(settings: Settings) -> list[str]:
    """Differences from CONTRACTS 2.1 for control.db, -wal and -shm (OS isolation on POSIX only)."""
    if settings.isolation != "os" or os.name != "posix":
        return []
    return file_problems(settings.control_db_path)


def file_problems(db_path: Path, lstat: Callable[[Path], os.stat_result] = os.lstat) -> list[str]:
    problems: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        path = db_path.with_name(db_path.name + suffix)
        try:
            st = lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            problems.append(f"{path.name} is not a regular file")
            continue
        if (st.st_uid, st.st_gid) != CONTROL_DB_OWNER:
            problems.append(
                f"{path.name} is owned by {st.st_uid}:{st.st_gid}, expected {CONTROL_DB_OWNER[0]}:{CONTROL_DB_OWNER[1]}"
            )
        if stat.S_IMODE(st.st_mode) != CONTROL_DB_MODE:
            problems.append(f"{path.name} has mode {stat.S_IMODE(st.st_mode):o}, expected {CONTROL_DB_MODE:o}")
    return problems
