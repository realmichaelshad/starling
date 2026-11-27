"""Capture spool: passive captures for customers whose engine is not running.

Captures (`/snapshots/*`, `/outcomes`, `/images/feedback`, `/preferences/confirm`) never
spawn an engine on their own. They are appended to a per-tenant JSON-lines file owned by
the gateway (not inside the customer data folder, which belongs to the engine's OS user)
and replayed in order right after that customer's next spawn, before any other request is
forwarded (`EnginePool` holds the tenant's spool lock while replaying).

The file is size-capped. When a new entry does not fit, the oldest entries are dropped
first: newer captures supersede older ones (metrics of the same post, the same profile).
Deleting a customer's data deletes their spool file too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .spawner import validate_tenant_seq

log = logging.getLogger(__name__)

SPOOLABLE_PATHS = frozenset({
    "/snapshots/profile",
    "/snapshots/metrics",
    "/outcomes/record",
    "/images/feedback",
    "/preferences/confirm",
})
DEFAULT_MAX_BYTES = 1024 * 1024
FORMAT_VERSION = 1
_IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class SpoolEntry:
    path: str
    body: bytes
    time_zone: str | None
    queued_at: int

    def to_line(self) -> bytes:
        payload = {
            "v": FORMAT_VERSION,
            "path": self.path,
            "body": self.body.decode("utf-8"),
            "tz": self.time_zone,
            "at": self.queued_at,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    @classmethod
    def from_line(cls, raw: bytes) -> "SpoolEntry | None":
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("v") != FORMAT_VERSION:
            return None
        path, body, tz, at = payload.get("path"), payload.get("body"), payload.get("tz"), payload.get("at")
        if path not in SPOOLABLE_PATHS or not isinstance(body, str):
            return None
        if tz is not None and not isinstance(tz, str):
            return None
        if isinstance(at, bool) or not isinstance(at, int):
            return None
        return cls(path=path, body=body.encode("utf-8"), time_zone=tz, queued_at=at)

    @classmethod
    def create(cls, path: str, body: bytes, time_zone: str | None) -> "SpoolEntry":
        if path not in SPOOLABLE_PATHS:
            raise ValueError(f"{path} is not a capture path")
        body.decode("utf-8")  # raises for bodies that are not UTF-8 text
        return cls(path=path, body=body, time_zone=time_zone, queued_at=int(time.time()))


class CaptureSpool:
    def __init__(self, directory: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self._locks: dict[int, asyncio.Lock] = {}

    def path_for(self, tenant_seq: int) -> Path:
        return self.directory / f"{validate_tenant_seq(tenant_seq)}.jsonl"

    def lock(self, tenant_seq: int) -> asyncio.Lock:
        """The tenant's spool lock. The pool holds it while deciding "spool or forward" and
        while replaying, so a capture can never overtake the replay of older ones."""
        lock = self._locks.get(tenant_seq)
        if lock is None:
            lock = self._locks[tenant_seq] = asyncio.Lock()
        return lock

    def size(self, tenant_seq: int) -> int:
        try:
            return self.path_for(tenant_seq).stat().st_size
        except OSError:
            return 0

    async def append(self, tenant_seq: int, entry: SpoolEntry) -> bool:
        return await asyncio.to_thread(self._append_sync, tenant_seq, entry)

    async def read(self, tenant_seq: int) -> list[SpoolEntry]:
        return await asyncio.to_thread(self._read_sync, tenant_seq)

    async def replace(self, tenant_seq: int, entries: list[SpoolEntry]) -> None:
        await asyncio.to_thread(self._replace_sync, tenant_seq, entries)

    async def delete(self, tenant_seq: int) -> None:
        await asyncio.to_thread(self._delete_sync, tenant_seq)

    # ------------------------------------------------------------------ file work

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not _IS_WINDOWS:
            os.chmod(self.directory, 0o700)

    def _append_sync(self, tenant_seq: int, entry: SpoolEntry) -> bool:
        line = entry.to_line()
        if len(line) > self.max_bytes:
            log.warning("capture for tenant %s is larger than the spool cap; dropped", tenant_seq)
            return False
        self._ensure_dir()
        path = self.path_for(tenant_seq)
        size = path.stat().st_size if path.exists() else 0
        if size + len(line) <= self.max_bytes:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(path, flags, 0o600), "ab") as fh:
                fh.write(line)
            return True
        lines = self._raw_lines(path)
        budget = self.max_bytes - len(line)
        kept: list[bytes] = []
        for raw in reversed(lines):
            if len(raw) > budget:
                break
            kept.append(raw)
            budget -= len(raw)
        kept.reverse()
        self._write_atomic(path, kept + [line])
        log.info("capture spool for tenant %s is full; dropped %s oldest entries", tenant_seq, len(lines) - len(kept))
        return True

    def _raw_lines(self, path: Path) -> list[bytes]:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return []
        return [line + b"\n" for line in data.split(b"\n") if line.strip()]

    def _read_sync(self, tenant_seq: int) -> list[SpoolEntry]:
        entries, invalid = [], 0
        for raw in self._raw_lines(self.path_for(tenant_seq)):
            entry = SpoolEntry.from_line(raw)
            if entry is None:
                invalid += 1
            else:
                entries.append(entry)
        if invalid:
            log.warning("capture spool for tenant %s had %s unreadable entries", tenant_seq, invalid)
        return entries

    def _replace_sync(self, tenant_seq: int, entries: list[SpoolEntry]) -> None:
        path = self.path_for(tenant_seq)
        if not entries:
            self._delete_sync(tenant_seq)
            return
        self._ensure_dir()
        self._write_atomic(path, [entry.to_line() for entry in entries])

    def _delete_sync(self, tenant_seq: int) -> None:
        path = self.path_for(tenant_seq)
        for candidate in (path, path.with_name(path.name + ".tmp")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

    def _write_atomic(self, path: Path, lines: list[bytes]) -> None:
        tmp = path.with_name(path.name + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(tmp, flags, 0o600), "wb") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
