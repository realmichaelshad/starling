"""What the gateway holds about one customer, for the data export (CONTRACTS section 10).

The engine's bundle (their own material, drafts, ratings, snapshots) arrives from `GET /profile/export`;
this adds the control database's side: the account, its devices and browser sessions, consent, billing
and the usage ledger. Identifiers that are stored only as hashes (session ids, device tokens, password)
are never exported, and no prompt or draft text lives here.
"""
from __future__ import annotations

import sqlite3
from typing import Any

ACCOUNT_SQL = (
    "SELECT id, email, created_at, status, email_verified_at, password_changed_at, is_admin, deleted_at "
    "FROM accounts WHERE id = ?"
)
LISTS: dict[str, str] = {
    "devices": (
        "SELECT label, extension_id, extension_version, created_at, last_used_at, idle_expires_at, abs_expires_at, "
        "revoked_at, revoked_reason FROM devices WHERE account_id = ? ORDER BY created_at"
    ),
    "sessions": (
        "SELECT created_at, last_seen_at, idle_expires_at, abs_expires_at, ip_prefix, user_agent, revoked_at "
        "FROM web_sessions WHERE account_id = ? ORDER BY created_at"
    ),
    "sign_in_methods": (
        "SELECT provider, subject, email, created_at, last_used_at FROM account_identities WHERE account_id = ? "
        "ORDER BY created_at"
    ),
    "consents": (
        "SELECT kind, version, passive_learning, granted_at, revoked_at FROM consents WHERE account_id = ? "
        "ORDER BY granted_at"
    ),
    "subscriptions": (
        "SELECT subscription_id, status, plan_code, current_period_start, current_period_end, cancel_at_period_end, "
        "cancel_at, canceled_at, ended_at FROM subscriptions WHERE account_id = ? ORDER BY current_period_start"
    ),
    "entitlement_periods": (
        "SELECT source, plan_code, period_start, period_end, allowance, cost_ceiling_micros, created_at "
        "FROM entitlement_periods WHERE account_id = ? ORDER BY period_start"
    ),
    "counters": (
        "SELECT period_id, committed, reserved, granted, cost_micros, background_micros, released_with_spend "
        "FROM usage_counters WHERE account_id = ?"
    ),
    "credit_ledger": (
        "SELECT kind, amount, actor, reason, created_at FROM credit_ledger WHERE account_id = ? ORDER BY id"
    ),
    "requests": (
        "SELECT route, mode, state, credits, ai_calls, cost_micros, created_at, finished_at, release_reason "
        "FROM usage_reservations WHERE account_id = ? ORDER BY created_at"
    ),
}


def _rows(conn: sqlite3.Connection, name: str, account_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(LISTS[name], (account_id,))]


def control_records(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    """The gateway's own records for this account, grouped the way the export presents them."""
    account = conn.execute(ACCOUNT_SQL, (account_id,)).fetchone()
    customer = conn.execute(
        "SELECT customer_id, created_at FROM stripe_customers WHERE account_id = ?", (account_id,)
    ).fetchone()
    return {
        "account": dict(account) if account is not None else {"id": account_id},
        "devices": _rows(conn, "devices", account_id),
        "sessions": _rows(conn, "sessions", account_id),
        "sign_in_methods": _rows(conn, "sign_in_methods", account_id),
        "consents": _rows(conn, "consents", account_id),
        "billing": {
            "stripe_customer": dict(customer) if customer is not None else None,
            "subscriptions": _rows(conn, "subscriptions", account_id),
            "entitlement_periods": _rows(conn, "entitlement_periods", account_id),
        },
        "usage": {
            "counters": _rows(conn, "counters", account_id),
            "credit_ledger": _rows(conn, "credit_ledger", account_id),
            "requests": _rows(conn, "requests", account_id),
        },
    }
