"""Website sessions stored in `web_sessions`.

- The cookie holds a random 256-bit id; the table stores only its SHA-256 (`id_hash`).
- Cookie: `settings.session_cookie_name` (`__Host-` prefixed on https), HttpOnly, Secure on https,
  SameSite=Lax, Path=/, no Domain, Max-Age up to the absolute expiry.
- Lifetimes: 14 days idle (extended at most every 5 minutes), 30 days absolute.
- The id is rotated on login, on admin elevation and on password change (`rotate`).
- A session is valid only while its `auth_epoch` equals the account's (checked in `auth.deps`).
"""
from __future__ import annotations

import random
import re
import sqlite3
from dataclasses import dataclass

from fastapi import Response

from ..config import Settings
from ..security.crypto import random_token, sha256_hex

IDLE_S = 14 * 86400
ABSOLUTE_S = 30 * 86400
TOUCH_INTERVAL_S = 300
ADMIN_ELEVATION_S = 15 * 60
USER_AGENT_MAX = 200
PURGE_AFTER_S = 7 * 86400

COLUMNS = (
    "id_hash, account_id, auth_epoch, csrf_secret, created_at, last_seen_at, idle_expires_at, "
    "abs_expires_at, admin_elevated_until, ip_prefix, user_agent, revoked_at"
)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_KEEP = object()


@dataclass(frozen=True)
class WebSession:
    id_hash: str
    account_id: str
    auth_epoch: int
    csrf_secret: str
    created_at: int
    last_seen_at: int
    idle_expires_at: int
    abs_expires_at: int
    admin_elevated_until: int | None
    ip_prefix: str | None
    user_agent: str | None
    revoked_at: int | None

    def is_active(self, now: int) -> bool:
        return self.revoked_at is None and now < self.idle_expires_at and now < self.abs_expires_at

    def is_elevated(self, now: int) -> bool:
        return self.admin_elevated_until is not None and now < self.admin_elevated_until

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WebSession":
        return cls(**{name: row[name] for name in COLUMNS.replace(" ", "").split(",")})


def get(conn: sqlite3.Connection, session_id: str | None) -> WebSession | None:
    if not session_id or not _ID_RE.match(session_id):
        return None
    row = conn.execute(f"SELECT {COLUMNS} FROM web_sessions WHERE id_hash = ?", (sha256_hex(session_id),)).fetchone()
    return WebSession.from_row(row) if row else None


def get_by_hash(conn: sqlite3.Connection, id_hash: str) -> WebSession | None:
    row = conn.execute(f"SELECT {COLUMNS} FROM web_sessions WHERE id_hash = ?", (id_hash,)).fetchone()
    return WebSession.from_row(row) if row else None


def still_valid(conn: sqlite3.Connection, id_hash: str, now: int) -> WebSession | None:
    """Re-read a session inside a transaction: active and on the account's current epoch."""
    session = get_by_hash(conn, id_hash)
    if session is None or not session.is_active(now):
        return None
    row = conn.execute("SELECT auth_epoch, status FROM accounts WHERE id = ?", (session.account_id,)).fetchone()
    if row is None or int(row["auth_epoch"]) != session.auth_epoch or row["status"] not in ("active", "suspended"):
        return None
    return session


def create(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    auth_epoch: int,
    now: int,
    ip_prefix: str | None,
    user_agent: str | None,
    admin_elevated_until: int | None = None,
    abs_expires_at: int | None = None,
) -> tuple[str, WebSession]:
    """Insert a session inside a writer transaction; returns (cookie value, row)."""
    if random.random() < 0.01:
        purge(conn, now)
    session_id = random_token()
    absolute = abs_expires_at if abs_expires_at is not None else now + ABSOLUTE_S
    session = WebSession(
        id_hash=sha256_hex(session_id),
        account_id=account_id,
        auth_epoch=auth_epoch,
        csrf_secret=random_token(),
        created_at=now,
        last_seen_at=now,
        idle_expires_at=min(now + IDLE_S, absolute),
        abs_expires_at=absolute,
        admin_elevated_until=admin_elevated_until,
        ip_prefix=ip_prefix,
        user_agent=(user_agent or "")[:USER_AGENT_MAX] or None,
        revoked_at=None,
    )
    conn.execute(
        f"INSERT INTO web_sessions ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session.id_hash, session.account_id, session.auth_epoch, session.csrf_secret, session.created_at,
            session.last_seen_at, session.idle_expires_at, session.abs_expires_at, session.admin_elevated_until,
            session.ip_prefix, session.user_agent, None,
        ),
    )
    return session_id, session


def rotate(
    conn: sqlite3.Connection,
    old: WebSession,
    *,
    now: int,
    ip_prefix: str | None,
    user_agent: str | None,
    auth_epoch: int | None = None,
    admin_elevated_until: object = _KEEP,
) -> tuple[str, WebSession]:
    """Issue a new id for the same sign-in (absolute expiry unchanged) and revoke the old id."""
    revoke(conn, old.id_hash, now)
    elevated = old.admin_elevated_until if admin_elevated_until is _KEEP else admin_elevated_until
    return create(
        conn,
        account_id=old.account_id,
        auth_epoch=old.auth_epoch if auth_epoch is None else auth_epoch,
        now=now,
        ip_prefix=ip_prefix,
        user_agent=user_agent,
        admin_elevated_until=elevated,  # type: ignore[arg-type]
        abs_expires_at=old.abs_expires_at,
    )


def touch(conn: sqlite3.Connection, id_hash: str, now: int) -> None:
    conn.execute(
        "UPDATE web_sessions SET last_seen_at = ?, idle_expires_at = MIN(?, abs_expires_at) "
        "WHERE id_hash = ? AND revoked_at IS NULL",
        (now, now + IDLE_S, id_hash),
    )


def revoke(conn: sqlite3.Connection, id_hash: str, now: int) -> None:
    conn.execute("UPDATE web_sessions SET revoked_at = ? WHERE id_hash = ? AND revoked_at IS NULL", (now, id_hash))


def revoke_all(conn: sqlite3.Connection, account_id: str, now: int, except_id_hash: str | None = None) -> int:
    cursor = conn.execute(
        "UPDATE web_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL AND id_hash != ?",
        (now, account_id, except_id_hash or ""),
    )
    return cursor.rowcount


def list_active(conn: sqlite3.Connection, account_id: str, now: int) -> list[WebSession]:
    rows = conn.execute(
        f"SELECT {', '.join('s.' + c for c in COLUMNS.replace(' ', '').split(','))} FROM web_sessions s "
        "JOIN accounts a ON a.id = s.account_id "
        "WHERE s.account_id = ? AND s.revoked_at IS NULL AND s.idle_expires_at > ? AND s.abs_expires_at > ? "
        "AND s.auth_epoch = a.auth_epoch ORDER BY s.last_seen_at DESC",
        (account_id, now, now),
    ).fetchall()
    return [WebSession.from_row(row) for row in rows]


def purge(conn: sqlite3.Connection, now: int) -> None:
    conn.execute(
        "DELETE FROM web_sessions WHERE abs_expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
        (now - PURGE_AFTER_S, now - PURGE_AFTER_S),
    )


def set_cookie(response: Response, settings: Settings, session_id: str, session: WebSession, now: int) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        max_age=max(0, session.abs_expires_at - now),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )
