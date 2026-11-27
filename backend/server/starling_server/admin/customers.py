"""Customer list and customer page data for the owner dashboard.

Metadata, usage, cost and error codes only; nothing here reads a customer database, a stored result,
a prompt, a draft or a sample (CONTRACTS 13.9). Plan and usage numbers come from
`usage.service.account_state`, the function behind the customer's own account pages and
`/api/v1/session` (CONTRACTS 6.2), so both sides always show the same figures.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..auth import accounts
from ..billing import entitlements, mirror
from ..config import Settings
from ..db import Database
from ..devices import tokens as device_tokens
from ..usage import service as usage_service
from .metrics import DAY_S, snapshot

PAGE_SIZE = 50
MAX_QUERY_LENGTH = 200
RECENT_ERRORS_S = 7 * DAY_S
MAX_RECENT_ERRORS = 20
MAX_LEDGER_ROWS = 50


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(db: Database, settings: Settings, query: str, page: int, now: int) -> dict[str, Any]:
    """Accounts whose email contains `query` (or whose id is `query`), newest first, 50 per page.
    Deleted (anonymised) accounts are left out; their pages stay reachable by id."""
    cleaned = query.strip()[:MAX_QUERY_LENGTH]
    clauses, params = ["a.status != 'deleted'"], []
    if cleaned:
        clauses.append("(a.email_norm LIKE ? ESCAPE '\\' OR a.id = ?)")
        params.extend([f"%{_escape_like(cleaned.lower())}%", cleaned.lower()])
    with db.read() as conn:
        rows = conn.execute(
            "SELECT a.id, a.email, a.status, a.created_at, "
            "(SELECT MAX(d.last_used_at) FROM devices d WHERE d.account_id = a.id) AS device_at, "
            "(SELECT MAX(s.last_seen_at) FROM web_sessions s WHERE s.account_id = a.id) AS web_at, "
            "(SELECT MAX(r.created_at) FROM usage_reservations r WHERE r.account_id = a.id) AS request_at "
            f"FROM accounts a WHERE {' AND '.join(clauses)} "
            "ORDER BY a.created_at DESC, a.tenant_seq DESC LIMIT ? OFFSET ?",
            (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
        ).fetchall()
    return {
        "query": cleaned,
        "page": {"number": page, "has_prev": page > 1, "has_next": len(rows) > PAGE_SIZE},
        "customers": [_list_entry(db, settings, row, now) for row in rows[:PAGE_SIZE]],
    }


def _list_entry(db: Database, settings: Settings, row: sqlite3.Row, now: int) -> dict[str, Any]:
    try:
        state = usage_service.account_state(db, settings, row["id"], now)
    except (usage_service.UsageError, LookupError):
        state = None
    active = [value for value in (row["device_at"], row["web_at"], row["request_at"]) if value is not None]
    return {
        "id": row["id"],
        "email": row["email"],
        "plan_name": state["plan"]["name"] if state else "Unknown",
        "plan_status": state["plan"]["status"] if state else "unknown",
        "account_status": row["status"],
        "used": state["usage"]["used"] if state else 0,
        "allowance": state["usage"]["allowance"] if state else 0,
        "created_at": int(row["created_at"]),
        "last_active_at": max(active) if active else None,
    }


def detail(db: Database, settings: Settings, account_id: str, viewer_id: str, now: int) -> dict[str, Any] | None:
    """The admin/customer.html context (without `errors`), or None when the account does not exist."""
    with db.read() as conn:
        account = accounts.get_by_id(conn, account_id)
    if account is None:
        return None
    try:
        state = usage_service.account_state(db, settings, account_id, now)
        entitlement = entitlements.current_entitlement(db, settings, account_id, now)
    except (usage_service.UsageError, LookupError):
        return None
    with snapshot(db) as conn:
        counters = conn.execute(
            "SELECT cost_micros, released_with_spend FROM usage_counters WHERE account_id = ? AND period_id = ?",
            (account_id, entitlement.period_id),
        ).fetchone()
        ai_calls = conn.execute(
            "SELECT COUNT(*) FROM ai_calls WHERE account_id = ? AND started_at >= ? AND started_at < ?",
            (account_id, entitlement.period_start, entitlement.period_end),
        ).fetchone()[0]
        customer = mirror.customer_row_for_account(conn, account_id)
        subscriptions = mirror.subscription_rows_for_account(conn, account_id)
        devices_active = len(device_tokens.list_active(conn, account_id, account.auth_epoch, now))
        errors = conn.execute(
            "SELECT code, route, MAX(last_at) AS at, SUM(count) AS total FROM request_errors "
            "WHERE account_id = ? AND last_at >= ? GROUP BY code, route ORDER BY at DESC, code LIMIT ?",
            (account_id, now - RECENT_ERRORS_S, MAX_RECENT_ERRORS),
        ).fetchall()
        ledger = conn.execute(
            "SELECT created_at, kind, amount, actor, reason FROM credit_ledger WHERE account_id = ? AND kind != 'commit' "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (account_id, MAX_LEDGER_ROWS),
        ).fetchall()
    return {
        "customer": {
            "id": account.id,
            "email": account.email,
            "tenant_seq": account.tenant_seq,
            "created_at": account.created_at,
            "email_verified": account.email_verified,
            "account_status": account.status,
            "is_admin": account.is_admin,
            "is_self": account.id == viewer_id,
            "dispute_open": any(row["disputed_at"] is not None for row in subscriptions),
            "stripe_customer_id": customer["customer_id"] if customer is not None and customer["deleted_at"] is None else None,
            "devices_active": devices_active,
            "subscription": _subscription_view(settings, subscriptions),
        },
        "state": state,
        "cost": {
            "period_cost_micros": int(counters["cost_micros"]) if counters is not None else 0,
            "ceiling_micros": int(entitlement.cost_ceiling_micros),
            "ai_calls": int(ai_calls),
            "released_with_spend": int(counters["released_with_spend"]) if counters is not None else 0,
        },
        "recent_errors": [
            {"at": int(row["at"]), "code": row["code"], "route": row["route"], "count": int(row["total"])} for row in errors
        ],
        "ledger": [
            {"created_at": int(row["created_at"]), "kind": row["kind"], "amount": int(row["amount"]),
             "actor": row["actor"] or "", "reason": row["reason"] or ""}
            for row in ledger
        ],
    }


def _subscription_view(settings: Settings, rows: list[sqlite3.Row]) -> dict[str, Any] | None:
    """The entitling subscription with the latest period (else the latest of any status)."""
    if not rows:
        return None
    entitling = [row for row in rows if row["status"] in mirror.ENTITLING_STATUSES]
    chosen = max(entitling or rows, key=lambda row: (row["current_period_start"] or 0, row["subscription_id"]))
    plan = settings.plans.get(chosen["plan_code"] or "")
    cancel_at = chosen["current_period_end"] if chosen["cancel_at_period_end"] else chosen["cancel_at"]
    return {
        "id": chosen["subscription_id"],
        "status": chosen["status"],
        "plan_name": plan.name if plan is not None else (chosen["plan_code"] or "Unknown plan"),
        "current_period_end": chosen["current_period_end"],
        "cancel_at": cancel_at,
    }
