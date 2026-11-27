"""Single-use email tokens stored as SHA-256 digests in `email_tokens`.

Purposes and lifetimes:

| Purpose | Sent to | Lifetime | `new_email` |
|---|---|---|---|
| `verify` | the account address | 24 hours | NULL |
| `email_change` | the requested new address | 24 hours | the new address |
| `reset` | the account address (forgot password) | 60 minutes | NULL |
| `reset` (revert) | the old address after an email change request | 7 days | the old address to restore |

Links open a page with a confirmation form (GET never consumes); the POST consumes the token
inside a writer transaction, so a token works exactly once.

A `verify` token from sign-up also carries a 6-digit code (`code_hash`) and is tied to the browser
that signed up (`browser_hash`). The code works once (`code_used_at`), only in that browser, and
only for `CODE_ATTEMPTS` wrong guesses; opening the link in ANOTHER browser confirms the address
but leaves the code usable, so the tab where the person signed up can still finish.
"""
from __future__ import annotations

import random
import re
import sqlite3
import uuid
from dataclasses import dataclass

from ..security.crypto import random_token, sha256_hex

VERIFY_TTL_S = 24 * 3600
EMAIL_CHANGE_TTL_S = 24 * 3600
RESET_TTL_S = 60 * 60
REVERT_TTL_S = 7 * 24 * 3600
# Rows that can never be valid again are cleared out occasionally, the way sessions and connect
# requests are: nothing else ever deletes them, so without this the table only grows.
PURGE_AFTER_S = 7 * 86400
PURGE_PROBABILITY = 0.01

PURPOSES = ("verify", "reset", "email_change")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
CODE_ATTEMPTS = 5
_CODE_COLUMNS = (
    "id, account_id, purpose, new_email, created_at, expires_at, used_at, browser_hash, code_hash, "
    "code_attempts, code_used_at, new_password_hash"
)


@dataclass(frozen=True)
class EmailToken:
    id: str
    account_id: str
    purpose: str
    new_email: str | None
    created_at: int
    expires_at: int
    used_at: int | None

    @property
    def is_revert(self) -> bool:
        return self.purpose == "reset" and self.new_email is not None


@dataclass(frozen=True)
class CodeToken:
    """A sign-up `verify` token as the code check sees it."""

    id: str
    account_id: str
    browser_hash: str | None
    code_hash: str | None
    code_attempts: int
    code_used_at: int | None
    used_at: int | None
    expires_at: int
    new_password_hash: str | None
    created_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CodeToken":
        return cls(
            id=row["id"],
            account_id=row["account_id"],
            browser_hash=row["browser_hash"],
            code_hash=row["code_hash"],
            code_attempts=int(row["code_attempts"]),
            code_used_at=row["code_used_at"],
            used_at=row["used_at"],
            expires_at=int(row["expires_at"]),
            new_password_hash=row["new_password_hash"],
            created_at=int(row["created_at"]),
        )


def issue(
    conn: sqlite3.Connection,
    account_id: str,
    purpose: str,
    ttl_s: int,
    now: int,
    new_email: str | None = None,
    *,
    browser_hash: str | None = None,
    code_hash: str | None = None,
    new_password_hash: str | None = None,
) -> str:
    """Create a token inside a writer transaction and return the plaintext (for the email only)."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown email token purpose {purpose!r}")
    if random.random() < PURGE_PROBABILITY:
        purge(conn, now)
    token = random_token()
    conn.execute(
        "INSERT INTO email_tokens (id, account_id, purpose, token_hash, new_email, created_at, expires_at, "
        "browser_hash, code_hash, new_password_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), account_id, purpose, sha256_hex(token), new_email, now, now + ttl_s,
         browser_hash, code_hash, new_password_hash),
    )
    return token


def code_token_for_link(conn: sqlite3.Connection, token: object) -> CodeToken | None:
    """The sign-up row behind a link, used or not (the caller has just consumed it)."""
    if not looks_like_token(token):
        return None
    row = conn.execute(
        f"SELECT {_CODE_COLUMNS} FROM email_tokens WHERE token_hash = ? AND purpose = 'verify'",
        (sha256_hex(token),),  # type: ignore[arg-type]
    ).fetchone()
    return CodeToken.from_row(row) if row else None


def code_token_for_browser(conn: sqlite3.Connection, browser_hash: str, now: int) -> CodeToken | None:
    """The newest live sign-up code issued to this browser."""
    row = conn.execute(
        f"SELECT {_CODE_COLUMNS} FROM email_tokens WHERE browser_hash = ? AND purpose = 'verify' "
        "AND code_hash IS NOT NULL AND code_used_at IS NULL AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
        (browser_hash, int(now)),
    ).fetchone()
    return CodeToken.from_row(row) if row else None


def code_token_for_account(conn: sqlite3.Connection, account_id: str, now: int) -> CodeToken | None:
    """The newest live code for a signed-in account (a resend from the account page)."""
    row = conn.execute(
        f"SELECT {_CODE_COLUMNS} FROM email_tokens WHERE account_id = ? AND purpose = 'verify' "
        "AND code_hash IS NOT NULL AND code_used_at IS NULL AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
        (account_id, int(now)),
    ).fetchone()
    return CodeToken.from_row(row) if row else None


def count_code_attempt(conn: sqlite3.Connection, token_id: str) -> int:
    conn.execute("UPDATE email_tokens SET code_attempts = code_attempts + 1 WHERE id = ?", (token_id,))
    row = conn.execute("SELECT code_attempts FROM email_tokens WHERE id = ?", (token_id,)).fetchone()
    return int(row["code_attempts"]) if row else CODE_ATTEMPTS


def use_code(conn: sqlite3.Connection, token_id: str, now: int) -> bool:
    """Spend the code (and the link with it). False when someone else spent it first."""
    cursor = conn.execute(
        "UPDATE email_tokens SET code_used_at = ?, used_at = COALESCE(used_at, ?) WHERE id = ? AND code_used_at IS NULL",
        (int(now), int(now), token_id),
    )
    return cursor.rowcount == 1


def looks_like_token(token: object) -> bool:
    return isinstance(token, str) and bool(_TOKEN_RE.match(token))


def find_valid(conn: sqlite3.Connection, token: object, purposes: tuple[str, ...], now: int) -> EmailToken | None:
    if not looks_like_token(token):
        return None
    row = conn.execute(
        "SELECT id, account_id, purpose, new_email, created_at, expires_at, used_at FROM email_tokens "
        "WHERE token_hash = ?",
        (sha256_hex(token),),  # type: ignore[arg-type]
    ).fetchone()
    if row is None or row["purpose"] not in purposes or row["used_at"] is not None or row["expires_at"] <= now:
        return None
    return EmailToken(
        id=row["id"],
        account_id=row["account_id"],
        purpose=row["purpose"],
        new_email=row["new_email"],
        created_at=int(row["created_at"]),
        expires_at=int(row["expires_at"]),
        used_at=row["used_at"],
    )


def consume(conn: sqlite3.Connection, token: object, purposes: tuple[str, ...], now: int) -> EmailToken | None:
    """Mark the token used (writer transaction); None when unknown, used, expired or another purpose."""
    found = find_valid(conn, token, purposes, now)
    if found is None:
        return None
    cursor = conn.execute("UPDATE email_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL", (now, found.id))
    return found if cursor.rowcount == 1 else None


def invalidate(
    conn: sqlite3.Connection,
    account_id: str,
    purpose: str,
    now: int,
    *,
    plain_resets_only: bool = False,
) -> None:
    """Retire outstanding tokens of one purpose (optionally keeping revert links), codes included."""
    sql = (
        "UPDATE email_tokens SET used_at = COALESCE(used_at, ?), code_used_at = COALESCE(code_used_at, ?) "
        "WHERE account_id = ? AND purpose = ? AND (used_at IS NULL OR (code_hash IS NOT NULL AND code_used_at IS NULL))"
    )
    if plain_resets_only:
        sql += " AND new_email IS NULL"
    conn.execute(sql, (now, now, account_id, purpose))


def address_is_held_for_a_revert(conn: sqlite3.Connection, email_norm: str, now: int) -> bool:
    """Somebody's email change away from this address can still be undone.

    While that is true the address must stay free. The revert link is the one control against a
    hijacked session changing the account's address, and it restores the OLD address: an attacker
    who registers a throwaway account on that address the moment it is released makes the restore
    impossible, and then takes the account back with an ordinary password reset.
    """
    rows = conn.execute(
        "SELECT new_email FROM email_tokens WHERE purpose = 'reset' AND new_email IS NOT NULL "
        "AND used_at IS NULL AND expires_at > ?",
        (int(now),),
    ).fetchall()
    from . import accounts  # local: accounts does not import this module, but keep the edge explicit

    return any(accounts.normalize_email(row["new_email"] or "") == email_norm for row in rows)


def purge(conn: sqlite3.Connection, now: int) -> None:
    """Delete tokens that expired or were used long ago (writer transaction)."""
    cutoff = now - PURGE_AFTER_S
    conn.execute(
        "DELETE FROM email_tokens WHERE expires_at < ? OR (used_at IS NOT NULL AND used_at < ?)", (cutoff, cutoff)
    )
