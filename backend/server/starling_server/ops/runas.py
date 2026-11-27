"""Run `ops.helper` under the owner of the files it touches.

Backups and the backup status run inside the supervisor, which is root in production. Opening
SQLite files as root in folders the gateway or a customer engine can write to would let a planted
link (for example a `-shm` file pointing at a system file) redirect root's reads and writes. So
with OS isolation and root privileges every SQLite open happens in a short helper process running
as the owner: the gateway user for the control database, the engine user for customer data. The
helper gets a minimal environment (no secrets). Without OS isolation, or when not root, the same
operations run in-process.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings

GATEWAY_UID = 10001  # starling-gw (CONTRACTS 2.1)
GATEWAY_GID = 10001
ENGINE_GROUP = 10010  # starling-eng
DB_GROUP = 10020  # starling-db
EXIT_RAW_COPY = 3  # helper `snapshot`: SQLite could not read the file, raw bytes were sent instead
SERVER_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_TIMEOUT_S = 3600.0


@dataclass(frozen=True)
class Identity:
    """User to run the helper as; `uid=None` keeps the current user (tests, development)."""

    uid: int | None
    gid: int | None
    groups: tuple[int, ...] = ()


CURRENT_USER = Identity(None, None)


def privileged(settings: Settings) -> bool:
    """True when SQLite work must move into helper processes (OS isolation, running as root)."""
    return settings.isolation == "os" and os.name == "posix" and os.geteuid() == 0


def control_identity() -> Identity:
    return Identity(GATEWAY_UID, GATEWAY_GID, (ENGINE_GROUP, DB_GROUP))


def tenant_identity(settings: Settings, tenant_seq: int) -> Identity:
    uid = settings.engine_uid_base + tenant_seq
    return Identity(uid, uid, ())


def helper_env() -> dict[str, str]:
    env = {
        "PYTHONPATH": str(SERVER_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": tempfile.gettempdir(),
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    if os.name == "nt":  # Python on Windows needs these to start
        env.update({k: os.environ[k] for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP") if k in os.environ})
    return env


def run_helper(
    identity: Identity,
    args: list[str],
    *,
    stdin: bytes | None = None,
    stdout_path: Path | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes, str]:
    """Run the helper; returns (exit status, stdout, one-line stderr).

    With `stdout_path` the output goes into a new file created by the caller (so the helper never
    needs access to the caller's folders) and the returned stdout is empty.
    """
    options: dict = {}
    if identity.uid is not None:
        # extra_groups is always passed so root's supplementary groups are dropped too.
        options = {"user": identity.uid, "group": identity.gid, "extra_groups": list(identity.groups), "umask": 0o077}
    command = [sys.executable, "-m", "starling_server.ops.helper", *args]
    handle = open(stdout_path, "xb") if stdout_path is not None else None
    try:
        completed = subprocess.run(
            command,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            stdout=handle if handle is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=helper_env(),
            cwd=str(SERVER_ROOT),
            timeout=timeout,
            check=False,
            **options,
        )
    finally:
        if handle is not None:
            handle.close()
    message = completed.stderr.decode("utf-8", "replace").strip()
    return completed.returncode, completed.stdout or b"", message.splitlines()[-1][:300] if message else ""
