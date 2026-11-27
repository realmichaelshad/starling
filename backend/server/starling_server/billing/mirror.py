"""The local mirror of Stripe state and owner alerts (CONTRACTS sections 8 and 11).

Pure database helpers shared by the processor, the reconciler and the service. Every
function taking `conn` runs inside a `Database.write` transaction (or a read connection
for the read-only ones); none of them calls Stripe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import zlib
from typing import Any, Mapping

from ..config import Plan, Settings
from .stripe_client import object_id

log = logging.getLogger(__name__)

ENTITLING_STATUSES = ("active", "trialing", "past_due")
TERMINAL_STATUSES = ("canceled", "incomplete_expired")
OPEN_DISPUTE_STATUSES = frozenset({"warning_needs_response", "warning_under_review", "needs_response", "under_review"})
WON_DISPUTE_STATUSES = frozenset({"won", "warning_closed", "prevented"})
ALERT_PREFIX = "alert:billing:"

_REF_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def now_or(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


def livemode(settings: Settings) -> bool:
    return settings.stripe_mode == "live"


def plan_rank(plan: Plan) -> tuple[int, int]:
    """Order plans by price, then allowance (higher is a higher tier)."""
    return (plan.amount_cents or 0, plan.monthly_credits)


_ACCOUNT_LOCKS = [threading.RLock() for _ in range(64)]


def account_lock(account_id: str) -> threading.RLock:
    """The in-process lock serialising billing work on one account: checkout and portal
    actions, schedule changes, event handling and resyncs. Striped, so unrelated accounts can
    share a lock, which only adds a short wait."""
    return _ACCOUNT_LOCKS[zlib.crc32(account_id.encode("utf-8")) % len(_ACCOUNT_LOCKS)]


# --------------------------------------------------------------------------- service_state


def get_state(conn: sqlite3.Connection, key: str) -> Any | None:
    row = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else None


def set_state(conn: sqlite3.Connection, key: str, value: Any, now: int) -> None:
    conn.execute(
        "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
        (key, json.dumps(value, sort_keys=True), int(now)),
    )


def raise_alert(
    conn: sqlite3.Connection,
    kind: str,
    ref: str | None,
    message: str,
    now: int,
    details: Mapping[str, Any] | None = None,
) -> str:
    """Record (or bump) an owner alert in `service_state` under `alert:billing:<kind>:<ref>`."""
    clean_ref = _REF_RE.sub("_", str(ref or "-"))[:120]
    key = f"{ALERT_PREFIX}{kind}:{clean_ref}"
    previous = get_state(conn, key) or {}
    set_state(
        conn,
        key,
        {
            "source": "billing",
            "level": "warning",
            "kind": kind,
            "ref": clean_ref,
            "message": message,
            "details": dict(details or {}),
            "first_at": previous.get("first_at", int(now)),
            "last_at": int(now),
            "count": int(previous.get("count", 0)) + 1,
        },
        now,
    )
    log.warning("billing alert %s (%s): %s", kind, clean_ref, message)
    return key


def list_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key, value_json FROM service_state WHERE key LIKE ? ORDER BY key", (ALERT_PREFIX + "%",)
    ).fetchall()
    return [{"key": row["key"], **json.loads(row["value_json"])} for row in rows]


# --------------------------------------------------------------------------- customers


def customer_row_for_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM stripe_customers WHERE account_id = ?", (account_id,)).fetchone()


def customer_row(conn: sqlite3.Connection, customer_id: str | None) -> sqlite3.Row | None:
    if not customer_id:
        return None
    return conn.execute("SELECT * FROM stripe_customers WHERE customer_id = ?", (customer_id,)).fetchone()


def is_tombstoned(conn: sqlite3.Connection, customer_id: str | None) -> bool:
    """A customer deleted in Stripe (or detached from a deleted account): ignore late events."""
    if not customer_id:
        return False
    if conn.execute("SELECT 1 FROM tombstones WHERE customer_id = ?", (customer_id,)).fetchone():
        return True
    row = customer_row(conn, customer_id)
    return bool(row is not None and row["deleted_at"] is not None)


def tombstone_hash(account_id: str, customer_id: str) -> str:
    return hashlib.sha256(f"stripe-customer:{account_id}:{customer_id}".encode("utf-8")).hexdigest()


def tombstone_customer(conn: sqlite3.Connection, account_id: str, customer_id: str, now: int) -> None:
    conn.execute(
        "UPDATE stripe_customers SET deleted_at = COALESCE(deleted_at, ?) WHERE customer_id = ?",
        (int(now), customer_id),
    )
    conn.execute(
        "INSERT INTO tombstones (account_hash, customer_id, deleted_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        (tombstone_hash(account_id, customer_id), customer_id, int(now)),
    )


# --------------------------------------------------------------------------- prices


def price_problems(price: Mapping[str, Any], plan: Plan) -> list[str]:
    """Differences between a Stripe price and its plan in plans.toml (empty when they match)."""
    problems: list[str] = []
    recurring = price.get("recurring") or {}
    if price.get("unit_amount") != plan.amount_cents:
        problems.append(f"{plan.code}: Stripe amount {price.get('unit_amount')} != plans.toml amount_cents {plan.amount_cents}")
    if str(price.get("currency") or "").lower() != (plan.currency or ""):
        problems.append(f"{plan.code}: Stripe currency {price.get('currency')!r} != plans.toml currency {plan.currency!r}")
    if recurring.get("interval") != plan.interval or int(recurring.get("interval_count") or 1) != 1:
        problems.append(
            f"{plan.code}: Stripe interval {recurring.get('interval')!r} x{recurring.get('interval_count')} "
            f"!= plans.toml interval {plan.interval!r}"
        )
    return problems


def upsert_price(conn: sqlite3.Connection, price: Mapping[str, Any], plan_code: str) -> None:
    recurring = price.get("recurring") or {}
    lookup_key = price.get("lookup_key")
    if lookup_key:
        # At Stripe a lookup key belongs to one price at a time, and creating a price with
        # `transfer_lookup_key` takes it away from the previous holder. Mirror that, or the old
        # row keeps a key it no longer has and checkout goes on selling the superseded price.
        # The row itself stays, with its plan, so existing subscribers keep their plan.
        conn.execute(
            "UPDATE price_map SET lookup_key = NULL WHERE lookup_key = ? AND price_id != ?",
            (lookup_key, price["id"]),
        )
    conn.execute(
        "INSERT INTO price_map (price_id, lookup_key, plan_code, interval, currency, unit_amount, active, livemode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(price_id) DO UPDATE SET lookup_key = excluded.lookup_key, "
        "plan_code = excluded.plan_code, interval = excluded.interval, currency = excluded.currency, "
        "unit_amount = excluded.unit_amount, active = excluded.active, livemode = excluded.livemode",
        (
            price["id"],
            price.get("lookup_key"),
            plan_code,
            str(recurring.get("interval") or ""),
            str(price.get("currency") or "").lower(),
            int(price.get("unit_amount") or 0),
            1 if price.get("active", True) else 0,
            1 if price.get("livemode") else 0,
        ),
    )


def plan_for_price(conn: sqlite3.Connection, settings: Settings, price_id: str | None) -> str | None:
    """The paid plan a known price belongs to. Unknown prices, prices from the other mode and
    plans no longer in plans.toml return None (fail closed). Grandfathered prices that lost
    their lookup key keep mapping to their plan."""
    if not price_id:
        return None
    row = conn.execute("SELECT plan_code, livemode FROM price_map WHERE price_id = ?", (price_id,)).fetchone()
    if row is None or bool(row["livemode"]) != livemode(settings):
        return None
    plan = settings.plans.get(row["plan_code"])
    return plan.code if plan is not None and plan.is_paid else None


def price_id_for_plan(conn: sqlite3.Connection, settings: Settings, plan: Plan) -> str | None:
    """The active mapped price that currently sells `plan` at the plans.toml amount."""
    row = conn.execute(
        "SELECT price_id FROM price_map WHERE lookup_key = ? AND plan_code = ? AND active = 1 AND livemode = ? "
        "AND unit_amount = ? AND currency = ? AND interval = ? ORDER BY price_id LIMIT 1",
        (plan.stripe_lookup_key, plan.code, 1 if livemode(settings) else 0, plan.amount_cents, plan.currency, plan.interval),
    ).fetchone()
    return row["price_id"] if row else None


# --------------------------------------------------------------------------- subscriptions


def first_item(subscription: Mapping[str, Any]) -> Mapping[str, Any] | None:
    items = (subscription.get("items") or {}).get("data") or []
    return items[0] if items else None


def item_price_id(item: Mapping[str, Any] | None) -> str | None:
    return object_id(item.get("price")) if item else None


def subscription_row(conn: sqlite3.Connection, subscription_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)).fetchone()


def subscription_rows_for_account(conn: sqlite3.Connection, account_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM subscriptions WHERE account_id = ? ORDER BY current_period_start, subscription_id", (account_id,)
    ).fetchall()


def upsert_subscription(
    conn: sqlite3.Connection,
    subscription: Mapping[str, Any],
    *,
    account_id: str,
    plan_code: str | None,
    scheduled_price_id: str | None,
    now: int,
    failure_at: int | None = None,
) -> None:
    """Mirror Stripe's current subscription object. `disputed_at` is never touched here."""
    subscription_id = subscription["id"]
    existing = subscription_row(conn, subscription_id)
    item = first_item(subscription)
    status = str(subscription.get("status") or "incomplete")
    latest = subscription.get("latest_invoice")
    latest_id = object_id(latest)
    if isinstance(latest, Mapping):
        latest_status = latest.get("status")
    elif existing is not None and latest_id and existing["latest_invoice_id"] == latest_id:
        latest_status = existing["latest_invoice_status"]
    else:
        latest_status = None
    failed_at = existing["first_payment_failed_at"] if existing is not None else None
    if status in ("active", "trialing"):
        failed_at = None
    elif status in ("past_due", "unpaid", "incomplete") and failed_at is None:
        failed_at = int(failure_at if failure_at is not None else now)
    schedule_id = object_id(subscription.get("schedule"))
    conn.execute(
        "INSERT INTO subscriptions (subscription_id, account_id, customer_id, status, price_id, plan_code, "
        "current_period_start, current_period_end, cancel_at_period_end, cancel_at, canceled_at, ended_at, "
        "schedule_id, scheduled_price_id, pending_update, latest_invoice_id, latest_invoice_status, "
        "first_payment_failed_at, disputed_at, livemode, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
        "ON CONFLICT(subscription_id) DO UPDATE SET account_id = excluded.account_id, "
        "customer_id = excluded.customer_id, status = excluded.status, price_id = excluded.price_id, "
        "plan_code = excluded.plan_code, current_period_start = excluded.current_period_start, "
        "current_period_end = excluded.current_period_end, cancel_at_period_end = excluded.cancel_at_period_end, "
        "cancel_at = excluded.cancel_at, canceled_at = excluded.canceled_at, ended_at = excluded.ended_at, "
        "schedule_id = excluded.schedule_id, scheduled_price_id = excluded.scheduled_price_id, "
        "pending_update = excluded.pending_update, latest_invoice_id = excluded.latest_invoice_id, "
        "latest_invoice_status = excluded.latest_invoice_status, "
        "first_payment_failed_at = excluded.first_payment_failed_at, livemode = excluded.livemode, "
        "synced_at = excluded.synced_at",
        (
            subscription_id,
            account_id,
            object_id(subscription.get("customer")),
            status,
            item_price_id(item),
            plan_code,
            _int_or_none(item.get("current_period_start") if item else None),
            _int_or_none(item.get("current_period_end") if item else None),
            1 if subscription.get("cancel_at_period_end") else 0,
            _int_or_none(subscription.get("cancel_at")),
            _int_or_none(subscription.get("canceled_at")),
            _int_or_none(subscription.get("ended_at")),
            schedule_id,
            scheduled_price_id if schedule_id else None,
            1 if subscription.get("pending_update") else 0,
            latest_id,
            latest_status,
            failed_at,
            1 if subscription.get("livemode") else 0,
            int(now),
        ),
    )


def mark_subscription_gone(conn: sqlite3.Connection, subscription_id: str, now: int) -> None:
    """Stripe no longer knows the subscription (404): mirror it as canceled."""
    conn.execute(
        "UPDATE subscriptions SET status = 'canceled', ended_at = COALESCE(ended_at, ?), synced_at = ? "
        "WHERE subscription_id = ? AND status NOT IN ('canceled', 'incomplete_expired')",
        (int(now), int(now), subscription_id),
    )


def set_session_status(conn: sqlite3.Connection, session_id: str, status: str) -> None:
    """Checkout sessions only move forward: open -> complete | expired."""
    if status in ("complete", "expired"):
        conn.execute(
            "UPDATE checkout_sessions SET status = ? WHERE session_id = ? AND status = 'open'", (status, session_id)
        )


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
