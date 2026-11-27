"""Device tokens for the extension (CONTRACTS section 9, step 5).

- Token: `stl_dev_` + 256 random bits (base64url); only the SHA-256 is stored, plus a short
  display prefix.
- Lifetimes: 60 days idle (slides when used), 1 year absolute.
- `last_used_at` is written at most every 10 minutes.
- Valid only on `/api/v1`, only while `auth_epoch` matches the account (password reset revokes).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ..security.crypto import random_token, sha256_hex

TOKEN_PREFIX = "stl_dev_"
IDLE_S = 60 * 86400
ABSOLUTE_S = 365 * 86400
LAST_USED_INTERVAL_S = 600
DISPLAY_PREFIX_LENGTH = len(TOKEN_PREFIX) + 4

_TOKEN_RE = re.compile(r"^stl_dev_[A-Za-z0-9_-]{43}$")
_VERSION_RE = re.compile(r"^(\d{1,6}(?:\.\d{1,6}){0,3})(?:[-+][0-9A-Za-z.-]{1,32})?$")

COLUMNS = (
    "id, account_id, token_prefix, label, extension_id, extension_version, auth_epoch, created_at, "
    "last_used_at, idle_expires_at, abs_expires_at, revoked_at, revoked_reason"
)


@dataclass(frozen=True)
class Device:
    id: str
    account_id: str
    token_prefix: str
    label: str
    extension_id: str
    extension_version: str | None
    auth_epoch: int
    created_at: int
    last_used_at: int | None
    idle_expires_at: int
    abs_expires_at: int
    revoked_at: int | None
    revoked_reason: str | None

    def is_active(self, now: int) -> bool:
        return self.revoked_at is None and now < self.idle_expires_at and now < self.abs_expires_at

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Device":
        return cls(**{name: row[name] for name in COLUMNS.replace(" ", "").split(",")})


def parse_bearer(header_value: str | None) -> str | None:
    """The device token from `Authorization: Bearer stl_dev_...`, or None."""
    if not header_value:
        return None
    scheme, _, credentials = header_value.strip().partition(" ")
    token = credentials.strip()
    if scheme.lower() != "bearer" or not _TOKEN_RE.match(token):
        return None
    return token


def find_by_token(conn: sqlite3.Connection, token: str) -> Device | None:
    row = conn.execute(f"SELECT {COLUMNS} FROM devices WHERE token_hash = ?", (sha256_hex(token),)).fetchone()
    return Device.from_row(row) if row else None


def get(conn: sqlite3.Connection, device_id: str) -> Device | None:
    row = conn.execute(f"SELECT {COLUMNS} FROM devices WHERE id = ?", (device_id,)).fetchone()
    return Device.from_row(row) if row else None


def issue(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    account_id: str,
    auth_epoch: int,
    label: str,
    extension_id: str,
    extension_version: str | None,
    now: int,
) -> tuple[str, Device]:
    """Insert a device inside a writer transaction; returns (plaintext token, row)."""
    token = TOKEN_PREFIX + random_token()
    device = Device(
        id=device_id,
        account_id=account_id,
        token_prefix=token[:DISPLAY_PREFIX_LENGTH],
        label=label,
        extension_id=extension_id,
        extension_version=extension_version,
        auth_epoch=auth_epoch,
        created_at=now,
        last_used_at=None,
        idle_expires_at=now + IDLE_S,
        abs_expires_at=now + ABSOLUTE_S,
        revoked_at=None,
        revoked_reason=None,
    )
    conn.execute(
        "INSERT INTO devices (id, account_id, token_hash, token_prefix, label, extension_id, extension_version, "
        "auth_epoch, created_at, last_used_at, idle_expires_at, abs_expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            device.id, account_id, sha256_hex(token), device.token_prefix, label, extension_id, extension_version,
            auth_epoch, now, device.idle_expires_at, device.abs_expires_at,
        ),
    )
    return token, device


def mark_used(conn: sqlite3.Connection, device_id: str, now: int, extension_version: str | None) -> None:
    conn.execute(
        "UPDATE devices SET last_used_at = ?, idle_expires_at = MIN(?, abs_expires_at), "
        "extension_version = COALESCE(?, extension_version) "
        "WHERE id = ? AND revoked_at IS NULL AND (last_used_at IS NULL OR last_used_at <= ?)",
        (now, now + IDLE_S, extension_version, device_id, now - LAST_USED_INTERVAL_S),
    )


def revoke(conn: sqlite3.Connection, device_id: str, now: int, reason: str, account_id: str | None = None) -> bool:
    sql = "UPDATE devices SET revoked_at = ?, revoked_reason = ? WHERE id = ? AND revoked_at IS NULL"
    params: list[object] = [now, reason, device_id]
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    return conn.execute(sql, params).rowcount == 1


def revoke_all(conn: sqlite3.Connection, account_id: str, now: int, reason: str) -> int:
    return conn.execute(
        "UPDATE devices SET revoked_at = ?, revoked_reason = ? WHERE account_id = ? AND revoked_at IS NULL",
        (now, reason, account_id),
    ).rowcount


def list_active(conn: sqlite3.Connection, account_id: str, auth_epoch: int, now: int) -> list[Device]:
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM devices WHERE account_id = ? AND revoked_at IS NULL AND auth_epoch = ? "
        "AND idle_expires_at > ? AND abs_expires_at > ? ORDER BY created_at DESC",
        (account_id, auth_epoch, now, now),
    ).fetchall()
    return [Device.from_row(row) for row in rows]


def parse_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = _VERSION_RE.match(value.strip())
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def version_ok(value: str | None, minimum: str) -> bool:
    """True when `value` is a version at least `minimum` (a missing or unreadable value is outdated)."""
    got = parse_version(value)
    return got is not None and got >= (parse_version(minimum) or (0, 0, 0, 0))
