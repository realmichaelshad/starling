"""Serial billing event processor (CONTRACTS section 8).

Each stored event is claimed (`processing`), the objects it concerns are fetched again
from Stripe (event order and payload freshness are never trusted), the mirror is updated
and the entitlement rows are granted or adjusted; the event ends `processed`, `ignored`
or `failed` (with the error; failed events are retried). One worker thread processes
events strictly one at a time.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..config import Settings
from ..db import Database
from . import entitlements, mirror
from .stripe_client import StripeClient, StripeNotFoundError, object_id
from .webhooks import payload_customer

log = logging.getLogger(__name__)


class RetryLater(Exception):
    """The event cannot be applied yet; it is marked failed and retried."""


@dataclass(frozen=True)
class Context:
    db: Database
    settings: Settings
    client: StripeClient
    event: dict[str, Any]
    now: int

    @property
    def obj(self) -> dict[str, Any]:
        return self.event["data"]["object"]

    @property
    def created(self) -> int:
        return int(self.event.get("created") or self.now)


# --------------------------------------------------------------------------- mirror sync (shared)


def resolve_plan(db: Database, settings: Settings, client: StripeClient, price_id: str | None) -> str | None:
    """Map a price to a plan through `price_map`; learn unmapped prices by lookup key when the
    Stripe price matches plans.toml exactly. Anything else is unknown (None)."""
    if not price_id:
        return None
    with db.read() as conn:
        code = mirror.plan_for_price(conn, settings, price_id)
        known = conn.execute("SELECT 1 FROM price_map WHERE price_id = ?", (price_id,)).fetchone() is not None
    if code is not None or known:
        return code
    try:
        price = client.retrieve_price(price_id)
    except StripeNotFoundError:
        return None
    plan = settings.plans.by_lookup_key(str(price.get("lookup_key") or ""))
    if plan is None or bool(price.get("livemode")) != mirror.livemode(settings) or mirror.price_problems(price, plan):
        return None
    db.write(lambda conn: mirror.upsert_price(conn, price, plan.code))
    return plan.code


def scheduled_price_for(client: StripeClient, subscription: Mapping[str, Any]) -> str | None:
    """The price of the schedule phase that follows the current one, if it changes the price."""
    schedule_id = object_id(subscription.get("schedule"))
    if not schedule_id:
        return None
    try:
        schedule = client.retrieve_subscription_schedule(schedule_id)
    except StripeNotFoundError:
        return None
    if schedule.get("status") not in ("not_started", "active"):
        return None
    current_end = (schedule.get("current_phase") or {}).get("end_date")
    current_price = mirror.item_price_id(mirror.first_item(subscription))
    for phase in schedule.get("phases") or []:
        start = phase.get("start_date")
        if current_end is None or start is None or int(start) < int(current_end):
            continue
        items = phase.get("items") or []
        price = object_id(items[0].get("price")) if items else None
        return price if price and price != current_price else None
    return None


def store_subscription(
    db: Database,
    settings: Settings,
    client: StripeClient,
    subscription: Mapping[str, Any],
    now: int,
    *,
    failure_at: int | None = None,
) -> str | None:
    """Mirror a freshly fetched subscription. Returns its account id, or None when the
    customer is not ours or was deleted."""
    customer_id = object_id(subscription.get("customer"))
    with db.read() as conn:
        tombstoned = mirror.is_tombstoned(conn, customer_id)
        mapping = mirror.customer_row(conn, customer_id)
    if tombstoned:
        log.info("ignoring subscription %s of a deleted customer", subscription.get("id"))
        return None
    if mapping is None:
        db.write(lambda conn: mirror.raise_alert(
            conn, "unknown_customer", customer_id, "A subscription belongs to a customer this service did not create.",
            now, {"subscription": subscription.get("id")},
        ))
        return None
    account_id = mapping["account_id"]
    status = subscription.get("status")
    price_id = mirror.item_price_id(mirror.first_item(subscription))
    plan_code = resolve_plan(db, settings, client, price_id)
    scheduled_price = scheduled_price_for(client, subscription)
    metadata_account = (subscription.get("metadata") or {}).get("account_id")

    def write(conn: Any) -> None:
        mirror.upsert_subscription(
            conn, subscription, account_id=account_id, plan_code=plan_code,
            scheduled_price_id=scheduled_price, now=now, failure_at=failure_at,
        )
        if plan_code is None and status not in mirror.TERMINAL_STATUSES:
            mirror.raise_alert(
                conn, "unknown_price", price_id,
                "A subscription uses a price that is not mapped to a plan; the account stays on the free plan.",
                now, {"subscription": subscription["id"], "account_id": account_id},
            )
        if metadata_account and metadata_account != account_id:
            mirror.raise_alert(
                conn, "subscription_account_mismatch", subscription["id"],
                "Subscription metadata names a different account than its customer.", now,
            )
        entitling = [
            row["subscription_id"] for row in mirror.subscription_rows_for_account(conn, account_id)
            if row["status"] in mirror.ENTITLING_STATUSES and row["plan_code"]
        ]
        if len(entitling) > 1:
            mirror.raise_alert(
                conn, "duplicate_subscriptions", account_id,
                "More than one entitling subscription; the highest plan applies and the reconciler cancels the others.",
                now, {"subscriptions": entitling},
            )

    db.write(write)
    return account_id


def sync_subscription(
    db: Database, settings: Settings, client: StripeClient, subscription_id: str, now: int, *, failure_at: int | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch and mirror one subscription. A 404 mirrors it as canceled and returns (None, None)."""
    try:
        subscription = client.retrieve_subscription(subscription_id, expand=["latest_invoice"])
    except StripeNotFoundError:
        db.write(lambda conn: mirror.mark_subscription_gone(conn, subscription_id, now))
        return None, None
    return subscription, store_subscription(db, settings, client, subscription, now, failure_at=failure_at)


def cancel_immediately(db: Database, settings: Settings, client: StripeClient, subscription_id: str, now: int, *, prorate: bool = False) -> bool:
    """Cancel a subscription now (no-op when already ended). Returns True when Stripe canceled it."""
    try:
        subscription = client.retrieve_subscription(subscription_id)
    except StripeNotFoundError:
        db.write(lambda conn: mirror.mark_subscription_gone(conn, subscription_id, now))
        return False
    canceled = False
    if subscription.get("status") not in mirror.TERMINAL_STATUSES:
        client.cancel_subscription(subscription_id, prorate=prorate, invoice_now=False)
        canceled = True
    sync_subscription(db, settings, client, subscription_id, now)
    return canceled


# --------------------------------------------------------------------------- helpers


def invoice_subscription_id(invoice: Mapping[str, Any]) -> str | None:
    parent = invoice.get("parent") or {}
    if parent.get("type") != "subscription_details":
        return None
    return object_id((parent.get("subscription_details") or {}).get("subscription"))


def _lines(invoice: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list((invoice.get("lines") or {}).get("data") or [])


def _is_subscription_line(line: Mapping[str, Any]) -> bool:
    return (line.get("parent") or {}).get("type") == "subscription_item_details"


def _is_proration(line: Mapping[str, Any]) -> bool:
    return bool(((line.get("parent") or {}).get("subscription_item_details") or {}).get("proration"))


def _line_price(line: Mapping[str, Any]) -> str | None:
    return object_id(((line.get("pricing") or {}).get("price_details") or {}).get("price"))


def _customer_account(ctx: Context, customer_id: str | None) -> tuple[str | None, bool]:
    with ctx.db.read() as conn:
        if mirror.is_tombstoned(conn, customer_id):
            return None, True
        row = mirror.customer_row(conn, customer_id)
    return (row["account_id"] if row is not None else None), False


def subscriptions_for_charge(ctx: Context, charge: Mapping[str, Any]) -> list[str]:
    """Subscriptions whose invoices this charge paid (via invoice payments)."""
    payment_intent = object_id(charge.get("payment_intent"))
    if not payment_intent:
        return []
    found: list[str] = []
    for payment in ctx.client.list_invoice_payments_for_payment_intent(payment_intent):
        invoice_id = object_id(payment.get("invoice"))
        if not invoice_id:
            continue
        subscription_id = invoice_subscription_id(ctx.client.retrieve_invoice(invoice_id))
        if subscription_id and subscription_id not in found:
            found.append(subscription_id)
    return found


def _alert(ctx: Context, kind: str, ref: str | None, message: str, **details: Any) -> None:
    ctx.db.write(lambda conn: mirror.raise_alert(conn, kind, ref, message, ctx.now, details))


# --------------------------------------------------------------------------- handlers


def handle_checkout_session(ctx: Context) -> str:
    session_id = ctx.obj.get("id")
    with ctx.db.read() as conn:
        local = conn.execute("SELECT * FROM checkout_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if local is None:
        _alert(ctx, "unknown_checkout_session", session_id, "A Checkout Session this service did not create reported an event.")
        return "ignored"
    session = ctx.client.retrieve_checkout_session(session_id)
    customer_id = object_id(session.get("customer"))
    with ctx.db.read() as conn:
        if mirror.is_tombstoned(conn, customer_id):
            return "ignored"
        mapping = mirror.customer_row_for_account(conn, local["account_id"])
    if mapping is None or mapping["customer_id"] != customer_id or session.get("client_reference_id") != local["account_id"]:
        _alert(ctx, "checkout_customer_mismatch", session_id, "A Checkout Session's customer does not match its account.")
        return "ignored"
    status = str(session.get("status") or "")
    ctx.db.write(lambda conn: mirror.set_session_status(conn, session_id, status))
    subscription_id = object_id(session.get("subscription"))
    if status == "complete" and subscription_id:
        sync_subscription(ctx.db, ctx.settings, ctx.client, subscription_id, ctx.now)
    return "processed"


def handle_subscription(ctx: Context) -> str:
    subscription, account_id = sync_subscription(ctx.db, ctx.settings, ctx.client, ctx.obj["id"], ctx.now)
    return "processed" if account_id is not None or subscription is None else "ignored"


def handle_invoice_paid(ctx: Context) -> str:
    invoice = ctx.client.retrieve_invoice(ctx.obj["id"])
    subscription_id = invoice_subscription_id(invoice)
    if invoice.get("status") != "paid" or not subscription_id:
        return "ignored"
    subscription, account_id = sync_subscription(ctx.db, ctx.settings, ctx.client, subscription_id, ctx.now)
    if account_id is None or subscription is None:
        return "ignored" if subscription is not None else "processed"
    if apply_paid_invoice(ctx.db, ctx.settings, ctx.client, invoice, subscription, account_id, ctx.now) == "no_period":
        raise RetryLater("the period this upgrade belongs to is not granted yet")
    return "processed"


def apply_paid_invoice(
    db: Database,
    settings: Settings,
    client: StripeClient,
    invoice: Mapping[str, Any],
    subscription: Mapping[str, Any],
    account_id: str,
    now: int,
) -> str:
    """Grant what a paid subscription invoice pays for; idempotent (processor and resync).

    Returns "granted", "uplifted", "already", "not_granting" (unpaid, or not a period or
    upgrade invoice), "unknown_price", "no_period_line" or "no_period" (an upgrade whose
    period is not granted yet)."""
    reason = invoice.get("billing_reason")
    if invoice.get("status") != "paid" or reason not in entitlements.GRANT_REASONS:
        return "not_granting"
    subscription_id = subscription["id"]
    lines = _lines(invoice)
    outcome = "already"
    period_line = next(
        (line for line in lines if _is_subscription_line(line) and not _is_proration(line) and line.get("period")), None
    )
    if period_line is not None:
        price_id = _line_price(period_line) or mirror.item_price_id(mirror.first_item(subscription))
        plan_code = resolve_plan(db, settings, client, price_id)
        if plan_code is None:
            _owner_alert(db, now, "unknown_price", price_id,
                         "A paid invoice is for a price that is not mapped to a plan; nothing was granted.", invoice=invoice["id"])
            return "unknown_price"
        start, end = int(period_line["period"]["start"]), int(period_line["period"]["end"])
        outcome = db.write(lambda conn: entitlements.grant_paid_period(
            conn, settings, account_id=account_id, subscription_id=subscription_id, plan_code=plan_code,
            period_start=start, period_end=end, invoice_id=invoice["id"], now=now,
        ))
        if outcome == "granted":
            _warn_if_already_over(db, settings, subscription, invoice, account_id, end, now)
    elif reason != "subscription_update":
        _owner_alert(db, now, "invoice_without_period", invoice["id"],
                     "A paid subscription invoice has no period line; nothing was granted.")
        return "no_period_line"
    if reason != "subscription_update":
        return outcome
    charges = [line for line in lines if _is_proration(line) and int(line.get("amount") or 0) > 0]
    if charges:
        line = max(charges, key=lambda item: int(item.get("amount") or 0))
        price_id = _line_price(line)
        effective_at = int((line.get("period") or {}).get("start") or invoice.get("created") or now)
    else:
        price_id = mirror.item_price_id(mirror.first_item(subscription))
        effective_at = int(invoice.get("created") or now)
    plan_code = resolve_plan(db, settings, client, price_id)
    if plan_code is None:
        _owner_alert(db, now, "unknown_price", price_id,
                     "A paid upgrade invoice is for a price that is not mapped to a plan.", invoice=invoice["id"])
        return "unknown_price"
    upgrade = db.write(lambda conn: entitlements.apply_upgrade(
        conn, settings, account_id=account_id, subscription_id=subscription_id,
        new_plan_code=plan_code, effective_at=effective_at,
    ))
    return upgrade if upgrade in ("uplifted", "no_period") else outcome


def _warn_if_already_over(
    db: Database,
    settings: Settings,
    subscription: Mapping[str, Any],
    invoice: Mapping[str, Any],
    account_id: str,
    period_end: int,
    now: int,
) -> None:
    """Tell the owner when a payment arrives for a period the customer can no longer use.

    A customer can pay an invoice that is still open after the subscription was cancelled (a
    dunning cycle that ran out, or a cancellation while a renewal invoice was unpaid). Stripe
    keeps the money and never revives the subscription, so the account falls back to free: the
    entitlement rules are right, but somebody has to decide about a refund. Ending exactly at
    the period end is the ordinary cancel-at-period-end case and is not reported.
    """
    if subscription.get("status") not in mirror.TERMINAL_STATUSES:
        return
    ended_at = subscription.get("ended_at")
    if int(ended_at if ended_at is not None else now) >= int(period_end):
        return
    _owner_alert(
        db, now, "paid_invoice_without_access", invoice.get("id"),
        "An invoice was paid for a subscription that had already ended; the account is on the free plan. "
        "Decide whether to refund it or start the subscription again.",
        account_id=account_id, subscription=subscription.get("id"), period_end=int(period_end),
    )


def _owner_alert(db: Database, now: int, kind: str, ref: str | None, message: str, **details: Any) -> None:
    db.write(lambda conn: mirror.raise_alert(conn, kind, ref, message, now, details))


def handle_invoice_payment_problem(ctx: Context) -> str:
    """`invoice.payment_failed` and `invoice.payment_action_required`."""
    invoice = ctx.client.retrieve_invoice(ctx.obj["id"])
    subscription_id = invoice_subscription_id(invoice)
    if not subscription_id:
        return "ignored"
    failure_at = ctx.created if invoice.get("status") != "paid" else None
    subscription, account_id = sync_subscription(
        ctx.db, ctx.settings, ctx.client, subscription_id, ctx.now, failure_at=failure_at
    )
    return "processed" if account_id is not None or subscription is None else "ignored"


def handle_charge_refunded(ctx: Context) -> str:
    charge = ctx.client.retrieve_charge(ctx.obj["id"])
    account_id, tombstoned = _customer_account(ctx, object_id(charge.get("customer")))
    if tombstoned or account_id is None:
        return "ignored"
    subscription_ids = subscriptions_for_charge(ctx, charge)
    if not charge.get("refunded"):
        # A partial refund changes nothing automatically; the owner decides.
        _alert(ctx, "partial_refund", charge.get("id"),
               "A subscription charge was partially refunded; the subscription and allowance were not changed.",
               account_id=account_id, subscriptions=subscription_ids,
               amount=charge.get("amount"), amount_refunded=charge.get("amount_refunded"))
        return "processed"
    if not subscription_ids:
        return "ignored"
    for subscription_id in subscription_ids:
        cancel_immediately(ctx.db, ctx.settings, ctx.client, subscription_id, ctx.now)
        ctx.db.write(lambda conn, sid=subscription_id: entitlements.end_subscription_periods(conn, sid, ctx.now))
    return "processed"


KEEPS_SUSPENSION = mirror.OPEN_DISPUTE_STATUSES | {"lost"}


def _other_open_dispute(ctx: Context, customer_id: str | None, dispute_id: Any) -> bool:
    """Whether another dispute of this customer still justifies the suspension.

    That is one still open, or one already lost: a lost chargeback keeps the account
    suspended for good (CONTRACTS 7.1), so winning a later one may not lift it. Asked of
    Stripe rather than remembered locally, the way every other decision here is made; only
    the few disputes that matter have their charge fetched.
    """
    if not customer_id:
        return False
    for other in ctx.client.list_disputes():
        if other.get("id") == dispute_id or other.get("status") not in KEEPS_SUSPENSION:
            continue
        charge_id = object_id(other.get("charge"))
        if not charge_id:
            continue
        try:
            if object_id(ctx.client.retrieve_charge(charge_id).get("customer")) == customer_id:
                return True
        except StripeNotFoundError:
            continue
    return False


def handle_dispute(ctx: Context) -> str:
    """`charge.dispute.created` and `charge.dispute.closed`: converge on the dispute's status."""
    dispute = ctx.client.retrieve_dispute(ctx.obj["id"])
    charge = ctx.client.retrieve_charge(object_id(dispute.get("charge")) or "")
    account_id, tombstoned = _customer_account(ctx, object_id(charge.get("customer")))
    if tombstoned or account_id is None:
        return "ignored"
    subscription_ids = subscriptions_for_charge(ctx, charge)
    for subscription_id in subscription_ids:
        sync_subscription(ctx.db, ctx.settings, ctx.client, subscription_id, ctx.now)
    if not subscription_ids:
        with ctx.db.read() as conn:
            subscription_ids = [row["subscription_id"] for row in mirror.subscription_rows_for_account(conn, account_id)]
    if not subscription_ids:
        _alert(ctx, "dispute_without_subscription", dispute.get("id"), "A dispute concerns an account with no subscription.")
        return "processed"
    status = str(dispute.get("status") or "")
    marks = ",".join("?" for _ in subscription_ids)
    opened_at = int(dispute.get("created") or ctx.now)
    if status in mirror.WON_DISPUTE_STATUSES:
        if _other_open_dispute(ctx, object_id(charge.get("customer")), dispute.get("id")):
            # The customer charged back more than one invoice. Winning the first one back does
            # not settle the rest, so the account stays suspended until none is open.
            log.info("dispute %s closed, but another of this customer is still open", dispute.get("id"))
            return "processed"
        ctx.db.write(lambda conn: conn.execute(
            f"UPDATE subscriptions SET disputed_at = NULL WHERE subscription_id IN ({marks})", subscription_ids))
        return "processed"
    ctx.db.write(lambda conn: conn.execute(
        f"UPDATE subscriptions SET disputed_at = COALESCE(disputed_at, ?) WHERE subscription_id IN ({marks})",
        [opened_at, *subscription_ids]))
    if status == "lost":
        for subscription_id in subscription_ids:
            cancel_immediately(ctx.db, ctx.settings, ctx.client, subscription_id, ctx.now)
        _alert(ctx, "dispute_lost", dispute.get("id"), "A dispute was lost; the subscription was canceled and the account stays suspended.",
               account_id=account_id)
    return "processed"


def handle_customer_deleted(ctx: Context) -> str:
    customer_id = ctx.obj.get("id")
    try:
        if not ctx.client.retrieve_customer(customer_id).get("deleted"):
            return "ignored"
    except StripeNotFoundError:
        pass
    with ctx.db.read() as conn:
        mapping = mirror.customer_row(conn, customer_id)
    if mapping is None:
        return "ignored"
    account_id = mapping["account_id"]
    try:
        subscriptions = ctx.client.list_subscriptions(customer=customer_id, status="all")
    except StripeNotFoundError:
        subscriptions = []
    for subscription in subscriptions:
        store_subscription(ctx.db, ctx.settings, ctx.client, subscription, ctx.now)

    def write(conn: Any) -> None:
        conn.execute(
            "UPDATE subscriptions SET status = 'canceled', ended_at = COALESCE(ended_at, ?), synced_at = ? "
            "WHERE customer_id = ? AND status NOT IN ('canceled', 'incomplete_expired')",
            (ctx.now, ctx.now, customer_id),
        )
        mirror.tombstone_customer(conn, account_id, customer_id, ctx.now)

    ctx.db.write(write)
    return "processed"


def handle_schedule(ctx: Context) -> str:
    schedule = ctx.client.retrieve_subscription_schedule(ctx.obj["id"])
    subscription_id = object_id(schedule.get("subscription")) or object_id(schedule.get("released_subscription"))
    if not subscription_id:
        return "ignored"
    subscription, account_id = sync_subscription(ctx.db, ctx.settings, ctx.client, subscription_id, ctx.now)
    return "processed" if account_id is not None or subscription is None else "ignored"


HANDLERS: dict[str, Callable[[Context], str]] = {
    "checkout.session.completed": handle_checkout_session,
    "checkout.session.expired": handle_checkout_session,
    "customer.subscription.created": handle_subscription,
    "customer.subscription.updated": handle_subscription,
    "customer.subscription.deleted": handle_subscription,
    "customer.subscription.pending_update_applied": handle_subscription,
    "customer.subscription.pending_update_expired": handle_subscription,
    "invoice.paid": handle_invoice_paid,
    "invoice.payment_failed": handle_invoice_payment_problem,
    "invoice.payment_action_required": handle_invoice_payment_problem,
    "charge.refunded": handle_charge_refunded,
    "charge.dispute.created": handle_dispute,
    "charge.dispute.closed": handle_dispute,
    "customer.deleted": handle_customer_deleted,
    "subscription_schedule.released": handle_schedule,
    "subscription_schedule.canceled": handle_schedule,
}


# --------------------------------------------------------------------------- processing


def process_event(db: Database, settings: Settings, client: StripeClient, event_id: str, now: int | None = None) -> str:
    """Process one stored event; returns its final status. Only `received` and `failed`
    events are claimed, so a duplicate of a processed event does nothing."""
    now = mirror.now_or(now)
    claimed = db.write(lambda conn: conn.execute(
        "UPDATE billing_events SET status = 'processing', attempts = attempts + 1 "
        "WHERE event_id = ? AND status IN ('received', 'failed')", (event_id,),
    ).rowcount)
    with db.read() as conn:
        row = conn.execute("SELECT type, status, payload_json FROM billing_events WHERE event_id = ?", (event_id,)).fetchone()
    if row is None:
        return "missing"
    if not claimed:
        return row["status"]
    ctx = Context(db, settings, client, json.loads(row["payload_json"]), now)
    try:
        outcome = _dispatch(ctx)
    except Exception as exc:  # recorded and retried; never lost
        message = f"{type(exc).__name__}: {exc}"[:500]
        db.write(lambda conn: conn.execute(
            "UPDATE billing_events SET status = 'failed', last_error = ? WHERE event_id = ?", (message, event_id)))
        log.warning("billing event %s (%s) failed: %s", event_id, row["type"], message)
        return "failed"
    db.write(lambda conn: conn.execute(
        "UPDATE billing_events SET status = ?, processed_at = ?, last_error = NULL WHERE event_id = ?",
        (outcome, now, event_id)))
    return outcome


def _dispatch(ctx: Context) -> str:
    handler = HANDLERS.get(ctx.event.get("type", ""))
    if handler is None:
        return "ignored"
    customer_id = payload_customer(ctx.event)
    with ctx.db.read() as conn:
        if customer_id and ctx.event.get("type") != "customer.deleted" and mirror.is_tombstoned(conn, customer_id):
            log.info("ignoring late %s event %s for a deleted customer", ctx.event.get("type"), ctx.event.get("id"))
            return "ignored"
        account_id = _payload_account(conn, ctx, customer_id)
    if account_id is None:
        return handler(ctx)
    with mirror.account_lock(account_id):  # serialised with that account's actions and resyncs
        return handler(ctx)


def _payload_account(conn: Any, ctx: Context, customer_id: str | None) -> str | None:
    row = mirror.customer_row(conn, customer_id)
    if row is not None:
        return row["account_id"]
    if ctx.obj.get("object") == "checkout.session":
        session = conn.execute("SELECT account_id FROM checkout_sessions WHERE session_id = ?", (ctx.obj.get("id"),)).fetchone()
        return session["account_id"] if session is not None else None
    return None


def process_pending(db: Database, settings: Settings, client: StripeClient, now: int | None = None, *, limit: int = 1000) -> dict[str, int]:
    """Process every `received` or `failed` event in arrival order (used by tests and the CLI)."""
    with db.read() as conn:
        ids = [
            row["event_id"] for row in conn.execute(
                "SELECT event_id FROM billing_events WHERE status IN ('received', 'failed') "
                "ORDER BY received_at, rowid LIMIT ?", (limit,),
            )
        ]
    return dict(Counter(process_event(db, settings, client, event_id, now) for event_id in ids))


# --------------------------------------------------------------------------- queue worker


class BillingQueue:
    """One worker thread that processes stored events strictly one at a time.

    `enqueue` is thread-safe and skips ids already waiting. A failed event is retried with
    exponential backoff (up to `max_attempts`, then an owner alert) and again at every
    startup through `recover`.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        client: StripeClient,
        *,
        retry_base_s: float = 30.0,
        retry_max_s: float = 3600.0,
        max_attempts: int = 20,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.client = client
        self.retry_base_s = retry_base_s
        self.retry_max_s = retry_max_s
        self.max_attempts = max_attempts
        self._clock = clock
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._waiting: set[str] = set()
        self._timers: set[threading.Timer] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="billing-events", daemon=True)
            self._thread.start()

    def enqueue(self, event_id: str) -> None:
        with self._lock:
            if self._stopped or event_id in self._waiting:
                return
            self._waiting.add(event_id)
        self._queue.put(event_id)

    def recover(self) -> int:
        """Events left `processing` by a stopped process return to `received`; every `received`
        and `failed` event is queued again. Returns how many were queued."""
        self.db.write(lambda conn: conn.execute("UPDATE billing_events SET status = 'received' WHERE status = 'processing'"))
        with self.db.read() as conn:
            ids = [
                row["event_id"] for row in conn.execute(
                    "SELECT event_id FROM billing_events WHERE status IN ('received', 'failed') ORDER BY received_at, rowid"
                )
            ]
        for event_id in ids:
            self.enqueue(event_id)
        return len(ids)

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait until every queued event was handled. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._stopped = True
            timers, self._timers = list(self._timers), set()
        for timer in timers:
            timer.cancel()
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        while True:
            event_id = self._queue.get()
            try:
                if event_id is None:
                    return
                with self._lock:
                    self._waiting.discard(event_id)
                try:
                    status = process_event(
                        self.db, self.settings, self.client, event_id, self._clock() if self._clock else None
                    )
                except Exception:
                    log.exception("billing queue could not process event %s", event_id)
                    status = "failed"
                if status == "failed":
                    self._schedule_retry(event_id)
            finally:
                self._queue.task_done()

    def _schedule_retry(self, event_id: str) -> None:
        try:
            with self.db.read() as conn:
                row = conn.execute("SELECT attempts FROM billing_events WHERE event_id = ?", (event_id,)).fetchone()
        except Exception:
            log.exception("could not read attempts for billing event %s", event_id)
            return
        attempts = int(row["attempts"]) if row is not None else self.max_attempts
        if attempts >= self.max_attempts:
            self.db.write(lambda conn: mirror.raise_alert(
                conn, "event_retries_exhausted", event_id,
                "A billing event keeps failing; it is retried again at the next restart.", mirror.now_or(None),
            ))
            return
        delay = min(self.retry_max_s, self.retry_base_s * (2 ** max(0, attempts - 1)))
        timer = threading.Timer(delay, self._retry, args=(event_id,))
        timer.daemon = True
        with self._lock:
            if self._stopped:
                return
            self._timers = {t for t in self._timers if t.is_alive()}
            self._timers.add(timer)
        timer.start()

    def _retry(self, event_id: str) -> None:
        self.enqueue(event_id)
