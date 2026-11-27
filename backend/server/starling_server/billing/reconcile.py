"""Recovery jobs (CONTRACTS section 8).

* Hourly: list the subscribed event types of the last 30 days from Stripe and store the
  ones whose webhook never arrived; the serial processor then handles them.
* Daily (`full_resync`) and on demand from the owner dashboard (`resync_account`): fetch
  every subscription of a customer (all statuses) and mirror it exactly as the webhook
  processor does, mirror subscriptions Stripe no longer returns as canceled, recover the
  grant of any paid period or upgrade invoice whose event was missed (idempotent), cancel
  duplicate entitling subscriptions (the highest plan stays, the older one on a tie, owner
  alert) and re-evaluate the entitlement.

A resync holds the account's billing lock, which checkout and portal actions, schedule
changes and the event processor also take, so it is safe while webhooks arrive. Nothing is
deleted, and a repeat run changes nothing.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Mapping, Protocol, Sequence

from ..config import Settings
from ..db import Database
from . import entitlements, mirror, processor, webhooks
from .stripe_client import StripeClient, StripeError, StripeNotFoundError

log = logging.getLogger(__name__)

EVENT_WINDOW_S = 30 * 86_400
INVOICE_LOOKBACK_S = 86_400
EVENTS_STATE_KEY = "billing:reconcile:events"
RESYNC_STATE_KEY = "billing:reconcile:resync"
MIRROR_FIELDS = (
    "account_id", "status", "price_id", "plan_code", "current_period_start", "current_period_end",
    "cancel_at_period_end", "cancel_at", "canceled_at", "ended_at", "schedule_id", "scheduled_price_id",
    "pending_update", "latest_invoice_id", "latest_invoice_status", "first_payment_failed_at", "disputed_at", "livemode",
)
COUNTERS = ("mirror_changed", "missing", "periods_granted", "upgrades_applied", "periods_created", "canceled_duplicates")


class EventSink(Protocol):
    def enqueue(self, event_id: str) -> None: ...


def reconcile_events(
    db: Database, settings: Settings, client: StripeClient, *, queue: EventSink | None = None, now: int | None = None
) -> int:
    """Store subscribed events of the last 30 days that are missing locally. Returns the count."""
    now = mirror.now_or(now)
    recovered: list[str] = []
    for event in client.list_events(types=sorted(webhooks.SUBSCRIBED_EVENT_TYPES), created_gte=now - EVENT_WINDOW_S):
        if not webhooks.is_event(event) or event["type"] not in webhooks.SUBSCRIBED_EVENT_TYPES:
            continue
        if event["livemode"] != mirror.livemode(settings):
            continue
        inserted, _status = db.write(lambda conn, item=event: webhooks.record_event(conn, item, now))
        if inserted:
            recovered.append(event["id"])
            if queue is not None:
                queue.enqueue(event["id"])
    db.write(lambda conn: mirror.set_state(conn, EVENTS_STATE_KEY, {"ran_at": now, "recovered": len(recovered)}, now))
    if recovered:
        log.warning("billing reconciler recovered %d events whose webhooks never arrived", len(recovered))
    return len(recovered)


def resync_account(
    db: Database, settings: Settings, client: StripeClient, account_id: str, now: int | None = None
) -> dict[str, Any]:
    """Owner dashboard "Resync billing" for one account. Never raises for Stripe errors.

    Returns `{"ok": False, "reason": "no_customer"}` without a (live) Stripe customer,
    `{"ok": False, "reason": "stripe_error", "message": ...}` when Stripe fails, otherwise
    `{"ok": True, "customer_id", "subscriptions", <COUNTERS>, "entitlement_changed",
    "entitlement"}` where the counters say what this run changed."""
    now = mirror.now_or(now)
    with db.read() as conn:
        customer = mirror.customer_row_for_account(conn, account_id)
    if customer is None or customer["deleted_at"] is not None:
        return {"ok": False, "reason": "no_customer"}
    try:
        with mirror.account_lock(account_id):
            before = _entitlement_view(entitlements.preview_entitlement(db, settings, account_id, now))
            counts, entitlement = _resync_customer(db, settings, client, account_id, customer["customer_id"], now)
    except StripeError as exc:
        log.warning("billing resync of one account failed at Stripe: %s", _short_error(exc))
        return {"ok": False, "reason": "stripe_error", "message": _short_error(exc)}
    after = _entitlement_view(entitlement)
    return {"ok": True, "customer_id": customer["customer_id"], **counts, "entitlement_changed": before != after,
            "entitlement": after}


def full_resync(db: Database, settings: Settings, client: StripeClient, *, now: int | None = None) -> dict[str, int]:
    """Resync every known customer (daily job); a customer that fails is counted and skipped."""
    now = mirror.now_or(now)
    with db.read() as conn:
        customers = [
            (row["account_id"], row["customer_id"])
            for row in conn.execute(
                "SELECT account_id, customer_id FROM stripe_customers WHERE deleted_at IS NULL ORDER BY created_at"
            )
        ]
    summary = {"customers": 0, "subscriptions": 0, "errors": 0, **{key: 0 for key in COUNTERS}}
    for account_id, customer_id in customers:
        summary["customers"] += 1
        try:
            with mirror.account_lock(account_id):
                counts, _entitlement = _resync_customer(db, settings, client, account_id, customer_id, now)
        except StripeNotFoundError:
            continue
        except StripeError as exc:
            summary["errors"] += 1
            log.warning("billing resync of a customer failed at Stripe: %s", _short_error(exc))
            continue
        summary["subscriptions"] += counts["subscriptions"]
        for key in COUNTERS:
            summary[key] += counts[key]
    db.write(lambda conn: mirror.set_state(conn, RESYNC_STATE_KEY, {"ran_at": now, **summary}, now))
    return summary


def _resync_customer(
    db: Database, settings: Settings, client: StripeClient, account_id: str, customer_id: str, now: int
) -> tuple[dict[str, int], entitlements.Entitlement]:
    """One customer's resync; the caller holds the account lock."""
    mirror_before = _mirror_rows(db, customer_id)
    periods_before = _period_count(db, account_id)
    subscriptions = client.list_subscriptions(customer=customer_id, status="all")
    listed = {subscription["id"] for subscription in subscriptions}
    for subscription in subscriptions:
        processor.store_subscription(db, settings, client, subscription, now)
    missing = [
        subscription_id for subscription_id, fields in mirror_before.items()
        if subscription_id not in listed and fields["status"] not in mirror.TERMINAL_STATUSES
    ]
    for subscription_id in missing:
        processor.sync_subscription(db, settings, client, subscription_id, now)  # a 404 mirrors it canceled
    outcomes: list[str] = []
    for subscription in subscriptions:
        if subscription.get("status") in mirror.ENTITLING_STATUSES:
            outcomes.extend(_recover_grants(db, settings, client, account_id, subscription, now))
    canceled = resolve_duplicates(db, settings, client, account_id, subscriptions, now=now)
    entitlement = entitlements.current_entitlement(db, settings, account_id, now)
    mirror_after = _mirror_rows(db, customer_id)
    counts = {
        "subscriptions": len(subscriptions),
        "mirror_changed": sum(1 for sid, fields in mirror_after.items() if mirror_before.get(sid) != fields),
        "missing": len(missing),
        "periods_granted": outcomes.count("granted"),
        "upgrades_applied": outcomes.count("uplifted"),
        "periods_created": _period_count(db, account_id) - periods_before,
        "canceled_duplicates": len(canceled),
    }
    return counts, entitlement


def _recover_grants(
    db: Database, settings: Settings, client: StripeClient, account_id: str, subscription: Mapping[str, Any], now: int
) -> list[str]:
    """Apply the paid invoices of the current period, oldest first (idempotent)."""
    item = mirror.first_item(subscription)
    if item is None or item.get("current_period_start") is None:
        return []
    since = int(item["current_period_start"]) - INVOICE_LOOKBACK_S
    invoices = client.list_invoices(subscription=subscription["id"], status="paid", created_gte=since)
    return [
        processor.apply_paid_invoice(db, settings, client, invoice, subscription, account_id, now)
        for invoice in sorted(invoices, key=lambda invoice: (int(invoice.get("created") or 0), str(invoice.get("id"))))
    ]


def resolve_duplicates(
    db: Database,
    settings: Settings,
    client: StripeClient,
    account_id: str,
    subscriptions: Sequence[Mapping[str, Any]],
    *,
    now: int | None = None,
) -> list[str]:
    """Keep the highest-plan entitling subscription (older wins a tie); cancel the rest now.

    The extra subscriptions are canceled with proration, so the unused time is credited to the
    customer's Stripe balance; the owner alert lists them for any refund decision."""
    now = mirror.now_or(now)
    entitling = []
    for subscription in subscriptions:
        if subscription.get("status") not in mirror.ENTITLING_STATUSES:
            continue
        price_id = mirror.item_price_id(mirror.first_item(subscription))
        plan_code = processor.resolve_plan(db, settings, client, price_id)
        if plan_code is not None:
            entitling.append((settings.plans.get(plan_code), subscription))
    if len(entitling) <= 1:
        return []
    entitling.sort(
        key=lambda pair: (
            tuple(-part for part in mirror.plan_rank(pair[0])),
            int(pair[1].get("created") or 0),
            str(pair[1].get("id")),
        )
    )
    kept = entitling[0][1]["id"]
    canceled = [
        subscription["id"]
        for _, subscription in entitling[1:]
        if processor.cancel_immediately(db, settings, client, subscription["id"], now, prorate=True)
    ]
    db.write(lambda conn: mirror.raise_alert(
        conn, "duplicate_subscriptions_canceled", account_id,
        "Extra entitling subscriptions were canceled and the highest plan was kept; review refunds in Stripe.",
        now, {"kept": kept, "canceled": canceled},
    ))
    return canceled


def _mirror_rows(db: Database, customer_id: str) -> dict[str, dict[str, Any]]:
    with db.read() as conn:
        rows = conn.execute(
            f"SELECT subscription_id, {', '.join(MIRROR_FIELDS)} FROM subscriptions WHERE customer_id = ?", (customer_id,)
        ).fetchall()
    return {row["subscription_id"]: {field: row[field] for field in MIRROR_FIELDS} for row in rows}


def _period_count(db: Database, account_id: str) -> int:
    with db.read() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM entitlement_periods WHERE account_id = ?", (account_id,)).fetchone()[0])


def _entitlement_view(entitlement: entitlements.Entitlement) -> dict[str, Any]:
    view = asdict(entitlement)
    view.pop("period_id")
    return view


def _short_error(exc: StripeError) -> str:
    """Error type, HTTP status and Stripe error code only: no message text, ids or secrets."""
    parts = [type(exc).__name__]
    if exc.http_status:
        parts.append(f"HTTP {exc.http_status}")
    if exc.code:
        parts.append(str(exc.code)[:60])
    return " ".join(parts)
