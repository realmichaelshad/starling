"""Admin credit grants and debits, called by the admin workstream.

Each change is one `credit_ledger` row (`admin_grant` or `admin_debit`, always a positive
`amount`) plus the matching change to the current period's `usage_counters.granted`, in one
writer transaction. Admin routes write `admin_audit` themselves; this module only moves credits.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..billing import entitlements
from ..config import Settings
from ..db import Database
from . import service
from .service import UsageError

MAX_CREDIT_CHANGE = 10_000
MAX_REASON_LENGTH = 500
MAX_ACTOR_LENGTH = 200


def grant_credits(
    db: Database, settings: Settings, account_id: str, amount: int, actor: str, reason: str, now: int | None = None
) -> dict[str, Any]:
    """Add `amount` credits to the account's current period."""
    return _apply(db, settings, account_id, amount, actor, reason, "admin_grant", now)


def debit_credits(
    db: Database, settings: Settings, account_id: str, amount: int, actor: str, reason: str, now: int | None = None
) -> dict[str, Any]:
    """Remove `amount` credits from the account's current period."""
    return _apply(db, settings, account_id, amount, actor, reason, "admin_debit", now)


def _apply(
    db: Database, settings: Settings, account_id: str, amount: int, actor: str, reason: str, kind: str, now: int | None
) -> dict[str, Any]:
    now = service._now(now)
    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= MAX_CREDIT_CHANGE:
        raise UsageError("bad_request", 400, f"The amount must be a whole number from 1 to {MAX_CREDIT_CHANGE:,}.")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > MAX_REASON_LENGTH:
        raise UsageError("bad_request", 400, f"A reason of 1 to {MAX_REASON_LENGTH} characters is required.")
    actor = actor.strip() if isinstance(actor, str) else ""
    if not actor or len(actor) > MAX_ACTOR_LENGTH:
        raise UsageError("bad_request", 400, "The acting admin is required.")
    with db.read() as conn:
        known = conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if known is None:
        raise UsageError("not_found", 404, "Unknown account.")
    service.bind_settings(db, settings)
    change = amount if kind == "admin_grant" else -amount

    def transaction(conn: sqlite3.Connection) -> dict[str, Any]:
        entitlement = entitlements.entitlement_in_transaction(conn, settings, account_id, now)
        period_id = entitlement.period_id
        cursor = conn.execute(
            "INSERT INTO credit_ledger (account_id, period_id, kind, amount, reservation_id, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
            (account_id, period_id, kind, amount, actor, reason, now),
        )
        service._bump(conn, account_id, period_id, granted=change)
        committed, reserved, granted, cost_micros = service._counters(conn, account_id, period_id)
        return {
            "ledger_id": int(cursor.lastrowid),
            "kind": kind,
            "amount": amount,
            "period_id": period_id,
            "granted": granted,
            "usage": service.usage_block(settings, entitlement, committed, reserved, granted, cost_micros),
        }

    return db.write(transaction)
