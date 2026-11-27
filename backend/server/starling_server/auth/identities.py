"""Sign-in identities from other providers, stored in `account_identities` (migration 4).

Only Google today. An identity is keyed by the provider's stable subject id (`sub`), never by the
email address: a Google account keeps its `sub` when its owner changes the address, and a Starling
account keeps its Google sign-in when its owner changes the Starling address. `email` is the address
Google reported the last time the identity was used, for display on the Security page only.

Helpers take a `sqlite3.Connection` and work on read connections and inside writer transactions.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

GOOGLE = "google"
EMAIL_MAX = 320


@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    account_id: str
    email: str
    created_at: int
    last_used_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Identity":
        return cls(
            provider=row["provider"],
            subject=row["subject"],
            account_id=row["account_id"],
            email=row["email"],
            created_at=int(row["created_at"]),
            last_used_at=int(row["last_used_at"]),
        )


_COLUMNS = "provider, subject, account_id, email, created_at, last_used_at"


def find(conn: sqlite3.Connection, provider: str, subject: str) -> Identity | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM account_identities WHERE provider = ? AND subject = ?", (provider, subject)
    ).fetchone()
    return Identity.from_row(row) if row else None


def for_account(conn: sqlite3.Connection, account_id: str, provider: str = GOOGLE) -> Identity | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM account_identities WHERE account_id = ? AND provider = ?", (account_id, provider)
    ).fetchone()
    return Identity.from_row(row) if row else None


def link(conn: sqlite3.Connection, *, provider: str, subject: str, account_id: str, email: str, now: int) -> None:
    """Attach an identity to an account (writer transaction). The caller has checked both are free."""
    conn.execute(
        f"INSERT INTO account_identities ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
        (provider, subject, account_id, email[:EMAIL_MAX], int(now), int(now)),
    )


def touch(conn: sqlite3.Connection, *, provider: str, subject: str, email: str, now: int) -> None:
    conn.execute(
        "UPDATE account_identities SET email = ?, last_used_at = ? WHERE provider = ? AND subject = ?",
        (email[:EMAIL_MAX], int(now), provider, subject),
    )


def unlink(conn: sqlite3.Connection, account_id: str, provider: str = GOOGLE) -> int:
    return conn.execute(
        "DELETE FROM account_identities WHERE account_id = ? AND provider = ?", (account_id, provider)
    ).rowcount


def delete_all(conn: sqlite3.Connection, account_id: str) -> None:
    """Account deletion: the link to the provider account is personal data and goes with the rest."""
    conn.execute("DELETE FROM account_identities WHERE account_id = ?", (account_id,))
