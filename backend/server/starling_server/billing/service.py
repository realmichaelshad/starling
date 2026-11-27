"""Billing actions for signed-in users and for account deletion (CONTRACTS sections 8 and 10).

Nothing here grants an entitlement: checkout, portal and schedule calls change Stripe, and
only the webhook processor (after refetching) changes what an account may use. The mirror
is refreshed after each action so pages show the new state immediately.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import partial
from typing import Any, Mapping

import anyio

from ..config import Plan, Settings
from ..db import Database
from ..usage import service as usage_service
from . import mirror, processor
from .stripe_client import (
    StripeClient,
    StripeInvalidRequestError,
    StripeNotFoundError,
    default_client,
    object_id,
)

log = logging.getLogger(__name__)

CHECKOUT_TTL_S = 30 * 60
# Stripe measures `expires_at` from its own clock (minimum 30 minutes); a small margin keeps a
# gateway clock that runs slightly behind from being refused.
CHECKOUT_CLOCK_MARGIN_S = 60
# A new checkout is refused (the portal is offered instead) while any of these exists.
BLOCKING_STATUSES = ("active", "trialing", "past_due", "incomplete", "paused")
SESSION_ID_RE = re.compile(r"^cs_[A-Za-z0-9_]{1,250}$")


class BillingError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class CheckoutResult:
    kind: str  # "checkout", "invoice" or "portal"
    url: str
    session_id: str | None = None


account_lock = mirror.account_lock  # one lock per account, shared with the processor and resyncs


# --------------------------------------------------------------------------- helpers


def _plan(settings: Settings, plan_code: str | None) -> Plan:
    plan = settings.plans.get(plan_code or "")
    if plan is None or not plan.is_paid or not plan.public:
        raise BillingError("unknown_plan", "Choose one of the listed plans.")
    return plan


def _account(db: Database, account_id: str):
    with db.read() as conn:
        row = conn.execute(
            "SELECT id, email, email_verified_at, status FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    if row is None:
        raise BillingError("not_found", "Unknown account.", 404)
    return row


def _https(url: Any) -> str:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BillingError("stripe_unavailable", "Billing is not available right now. Try again shortly.", 502)
    return url


def _customer_id(db: Database, account_id: str) -> str:
    with db.read() as conn:
        row = mirror.customer_row_for_account(conn, account_id)
    if row is None or row["deleted_at"] is not None:
        raise BillingError("no_subscription", "There is no active subscription to change.", 409)
    return row["customer_id"]


def price_for_plan(db: Database, settings: Settings, client: StripeClient, plan: Plan) -> str:
    """The active Stripe price selling `plan` at the plans.toml amount (looked up by lookup key)."""
    with db.read() as conn:
        price_id = mirror.price_id_for_plan(conn, settings, plan)
    if price_id:
        return price_id
    for price in client.list_prices(lookup_keys=[plan.stripe_lookup_key], active=True):
        if (
            price.get("lookup_key") == plan.stripe_lookup_key
            and not mirror.price_problems(price, plan)
            and bool(price.get("livemode")) == mirror.livemode(settings)
        ):
            db.write(lambda conn: mirror.upsert_price(conn, price, plan.code))
            return price["id"]
    db.write(lambda conn: mirror.raise_alert(
        conn, "price_missing", plan.code, f"No active Stripe price matches plan {plan.code}; run stripe-verify.",
        mirror.now_or(None),
    ))
    raise BillingError("plan_unavailable", "This plan cannot be bought right now.", 503)


def _customer_still_at_stripe(client: StripeClient, customer_id: str) -> bool:
    try:
        return not client.retrieve_customer(customer_id).get("deleted")
    except StripeNotFoundError:
        return False


def ensure_customer(db: Database, settings: Settings, client: StripeClient, account_id: str, now: int) -> str:
    """The account's Stripe customer, created once (idempotency key `cust-<account_id>`)."""
    live = mirror.livemode(settings)
    with db.read() as conn:
        row = mirror.customer_row_for_account(conn, account_id)
    if row is not None and row["deleted_at"] is None:
        if bool(row["livemode"]) != live:
            raise BillingError("billing_mode_mismatch", "Billing is being reconfigured. Try again later.", 503)
        if _customer_still_at_stripe(client, row["customer_id"]):
            return row["customer_id"]
        # Stripe no longer has the customer (deleted in the Dashboard, say) and the
        # `customer.deleted` webhook has not been processed yet. Without this the account could
        # never buy anything again: every Checkout Session would be refused with "no such
        # customer". Record the deletion here so a fresh customer is created below, under a
        # different idempotency key, and so late events for the old one stay ignored.
        db.write(lambda conn, gone=row["customer_id"]: mirror.tombstone_customer(conn, account_id, gone, now))
        with db.read() as conn:
            row = mirror.customer_row_for_account(conn, account_id)
    account = _account(db, account_id)
    # Keyed on the customer being replaced, not on the second it was deleted: two Stripe-side
    # deletions inside one second would otherwise share a key, and Stripe would replay the
    # first (already deleted) customer, leaving the account unable to check out.
    key = f"cust-{account_id}" if row is None else f"cust-{account_id}-{row['customer_id']}"
    customer = client.create_customer(
        {"email": account["email"], "metadata": {"account_id": account_id}}, idempotency_key=key
    )
    customer_id = customer["id"]

    def write(conn: Any) -> str:
        current = mirror.customer_row_for_account(conn, account_id)
        if current is None:
            conn.execute(
                "INSERT INTO stripe_customers (account_id, customer_id, livemode, created_at, deleted_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (account_id, customer_id, 1 if live else 0, now),
            )
            return customer_id
        if current["deleted_at"] is not None and current["customer_id"] != customer_id:
            mirror.tombstone_customer(conn, account_id, current["customer_id"], now)
            conn.execute(
                "UPDATE stripe_customers SET customer_id = ?, livemode = ?, created_at = ?, deleted_at = NULL "
                "WHERE account_id = ?",
                (customer_id, 1 if live else 0, now, account_id),
            )
            return customer_id
        return current["customer_id"]

    return db.write(write)


def _live_subscriptions(db: Database, settings: Settings, client: StripeClient, customer_id: str, now: int) -> list[dict[str, Any]]:
    subscriptions = client.list_subscriptions(customer=customer_id, status="all")
    for subscription in subscriptions:
        processor.store_subscription(db, settings, client, subscription, now)
    return subscriptions


def _current_subscription(
    db: Database, settings: Settings, client: StripeClient, customer_id: str, now: int
) -> tuple[dict[str, Any], Plan]:
    candidates: list[tuple[Plan, dict[str, Any]]] = []
    for subscription in _live_subscriptions(db, settings, client, customer_id, now):
        if subscription.get("status") not in mirror.ENTITLING_STATUSES:
            continue
        code = processor.resolve_plan(db, settings, client, mirror.item_price_id(mirror.first_item(subscription)))
        if code is not None:
            candidates.append((settings.plans.get(code), subscription))
    if not candidates:
        raise BillingError("no_subscription", "There is no active subscription to change.", 409)
    plan, subscription = max(candidates, key=lambda pair: mirror.plan_rank(pair[0]))
    return subscription, plan


def unpaid_first_invoice_url(client: StripeClient, subscriptions: list[dict[str, Any]]) -> str | None:
    """The hosted payment page of a first invoice that was never paid, or None.

    A subscription stays `incomplete` until its first invoice is paid: the card was declined,
    or the customer has not finished authenticating. The Customer Portal cannot rescue either
    case, because saving a new payment method there does not pay an invoice that is already
    open, so a customer sent to the portal has no way to buy until Stripe expires the
    subscription about a day later. The invoice's own hosted page does pay it, with a new card
    or by finishing authentication, and it activates the subscription they already started, so
    no second subscription can appear and nothing is granted before `invoice.paid`.
    """
    for subscription in subscriptions:
        if subscription.get("status") != "incomplete":
            continue
        invoice_id = object_id(subscription.get("latest_invoice"))
        if not invoice_id:
            continue
        try:
            invoice = client.retrieve_invoice(invoice_id)
        except StripeNotFoundError:
            continue
        url = invoice.get("hosted_invoice_url")
        if invoice.get("status") == "open" and isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def _abandon_unfinished_for_another_plan(
    db: Database, settings: Settings, client: StripeClient, subscriptions: list[dict[str, Any]], plan: Plan, now: int
) -> bool:
    """End first purchases that were never paid when the customer now asks for a DIFFERENT plan.

    A customer whose card was declined on Starter and who comes back and chooses Pro must not be
    sent to pay Starter's invoice. Deliberately narrow, so that nothing anybody paid for, and
    nothing Starling did not sell, can be ended here. It acts only when every blocking
    subscription is `incomplete` and for one of Starling's own plans, none of them is for the
    plan being asked for (that one is finished on its own invoice page instead), and none has a
    first invoice Stripe already shows as paid (the payment landed a moment ago and the
    subscription is about to become active). Returns True when something was cancelled.

    The cancelled subscription's Checkout Session is expired by the caller straight afterwards
    (`_expire_open_sessions`), so the abandoned attempt cannot be completed later from an old tab.
    """
    blocking = [s for s in subscriptions if s.get("status") in BLOCKING_STATUSES]
    if not blocking or any(s.get("status") != "incomplete" for s in blocking):
        return False
    for subscription in blocking:
        code = processor.resolve_plan(db, settings, client, mirror.item_price_id(mirror.first_item(subscription)))
        if code is None or code == plan.code:
            return False
        invoice = subscription.get("latest_invoice")
        if isinstance(invoice, dict) and invoice.get("status") == "paid":
            return False
    for subscription in blocking:
        client.cancel_subscription(subscription["id"], prorate=False, invoice_now=False)
        processor.sync_subscription(db, settings, client, subscription["id"], now)
    return True


def _expire_open_sessions(db: Database, client: StripeClient, account_id: str, customer_id: str) -> bool:
    """Expire the account's other open Checkout Sessions. Returns True if one had completed."""
    with db.read() as conn:
        local = [
            row["session_id"] for row in conn.execute(
                "SELECT session_id FROM checkout_sessions WHERE account_id = ? AND status = 'open'", (account_id,)
            )
        ]
    remote = [session["id"] for session in client.list_checkout_sessions(customer=customer_id, status="open")]
    completed = False
    for session_id in dict.fromkeys(local + remote):
        try:
            status = client.expire_checkout_session(session_id).get("status") or "expired"
        except StripeInvalidRequestError:
            try:
                status = client.retrieve_checkout_session(session_id).get("status") or "expired"
            except StripeNotFoundError:
                status = "expired"
        completed = completed or status == "complete"
        db.write(lambda conn, sid=session_id, value=status: mirror.set_session_status(conn, sid, value))
    return completed


# --------------------------------------------------------------------------- actions


def create_checkout(
    db: Database, settings: Settings, client: StripeClient, account_id: str, plan_code: str, now: int | None = None
) -> CheckoutResult:
    now = mirror.now_or(now)
    plan = _plan(settings, plan_code)
    account = _account(db, account_id)
    if account["email_verified_at"] is None:
        raise BillingError("email_unverified", "Confirm your email address before subscribing.", 403)
    if account["status"] != "active":
        raise BillingError("account_suspended", "This account cannot start a subscription.", 403)
    with account_lock(account_id):
        price_id = price_for_plan(db, settings, client, plan)
        customer_id = ensure_customer(db, settings, client, account_id, now)
        subscriptions = _live_subscriptions(db, settings, client, customer_id, now)
        if _abandon_unfinished_for_another_plan(db, settings, client, subscriptions, plan, now):
            subscriptions = _live_subscriptions(db, settings, client, customer_id, now)
        if not any(s.get("status") in BLOCKING_STATUSES for s in subscriptions):
            if _expire_open_sessions(db, client, account_id, customer_id):
                subscriptions = _live_subscriptions(db, settings, client, customer_id, now)
        blocking = [s for s in subscriptions if s.get("status") in BLOCKING_STATUSES]
        if blocking:
            if not any(s.get("status") in mirror.ENTITLING_STATUSES for s in blocking):
                # Nothing of theirs is live yet: let them finish the payment they started
                # (the same plan; a different plan was dealt with above) rather than sending
                # them to a portal that cannot complete it.
                invoice_url = unpaid_first_invoice_url(client, blocking)
                if invoice_url is not None:
                    return CheckoutResult("invoice", invoice_url)
            return CheckoutResult("portal", create_portal(db, settings, client, account_id, now=now))
        public = settings.public_url
        params: dict[str, Any] = {
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "client_reference_id": account_id,
            "metadata": {"account_id": account_id, "plan_code": plan.code},
            "subscription_data": {"metadata": {"account_id": account_id}},
            "success_url": f"{public}/billing/return?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{public}/pricing?checkout=cancelled",
            "expires_at": now + CHECKOUT_TTL_S + CHECKOUT_CLOCK_MARGIN_S,
            "allow_promotion_codes": settings.stripe_allow_promo_codes,
        }
        if settings.stripe_block_prepaid:
            params["payment_method_options"] = {"card": {"restrictions": {"funding_types_blocked": ["prepaid"]}}}
        if settings.stripe_automatic_tax:
            params["automatic_tax"] = {"enabled": True}
            params["customer_update"] = {"address": "auto"}
        session = client.create_checkout_session(params)
        url = _https(session.get("url"))
        expires_at = int(session.get("expires_at") or params["expires_at"])
        db.write(lambda conn: conn.execute(
            "INSERT INTO checkout_sessions (session_id, account_id, price_id, status, created_at, expires_at) "
            "VALUES (?, ?, ?, 'open', ?, ?) ON CONFLICT(session_id) DO NOTHING",
            (session["id"], account_id, price_id, now, expires_at),
        ))
        return CheckoutResult("checkout", url, session["id"])


PORTAL_FLOWS = ("payment_method_update", "subscription_cancel")


def create_portal(
    db: Database,
    settings: Settings,
    client: StripeClient,
    account_id: str,
    flow: str | None = None,
    now: int | None = None,
) -> str:
    """The management portal (never Stripe's default configuration, which may allow plan
    switching), optionally deep-linked to `payment_method_update` or `subscription_cancel`."""
    now = mirror.now_or(now)
    flow = flow or None
    if flow is not None and flow not in PORTAL_FLOWS:
        raise BillingError("bad_request", "Unknown billing action.")
    with db.read() as conn:
        row = mirror.customer_row_for_account(conn, account_id)
    if row is None or row["deleted_at"] is not None:
        raise BillingError("no_billing_account", "There is no subscription to manage yet.", 409)
    if not settings.stripe_portal_configuration_id:
        raise BillingError("portal_unavailable", "Billing management is not available right now.", 503)
    public = settings.public_url
    # The way back says where the customer went, so the Billing page can fetch the subscription at
    # once (instead of showing the old plan until the webhook lands) and say what changed.
    back = {None: "home", "payment_method_update": "card", "subscription_cancel": "cancel"}[flow]
    params: dict[str, Any] = {
        "customer": row["customer_id"],
        "configuration": settings.stripe_portal_configuration_id,
        "return_url": f"{public}/account/billing?portal={back}",
    }
    if flow is not None:
        flow_data: dict[str, Any] = {
            "type": flow,
            "after_completion": {"type": "redirect", "redirect": {"return_url": f"{public}/account/billing?portal={back}"}},
        }
        if flow == "subscription_cancel":
            with account_lock(account_id):
                subscription, _plan_ = _current_subscription(db, settings, client, row["customer_id"], now)
            flow_data["subscription_cancel"] = {"subscription": subscription["id"]}
        params["flow_data"] = flow_data
    return _https(client.create_portal_session(params).get("url"))


def start_upgrade(
    db: Database, settings: Settings, client: StripeClient, account_id: str, plan_code: str, now: int | None = None
) -> str:
    """Portal deep link confirming the switch to a higher price; Stripe invoices the proration
    at once (`always_invoice` on the upgrade configuration). The new plan applies only after
    that invoice is paid."""
    now = mirror.now_or(now)
    target = _plan(settings, plan_code)
    customer_id = _customer_id(db, account_id)
    if not settings.stripe_portal_upgrade_configuration_id:
        raise BillingError("portal_unavailable", "Upgrades are not available right now.", 503)
    with account_lock(account_id):
        subscription, current = _current_subscription(db, settings, client, customer_id, now)
        if mirror.plan_rank(target) <= mirror.plan_rank(current):
            raise BillingError("not_an_upgrade", "Choose a higher plan to upgrade.")
        schedule_id = object_id(subscription.get("schedule"))
        if schedule_id:
            schedule = client.retrieve_subscription_schedule(schedule_id)
            if not (schedule.get("metadata") or {}).get("starling_downgrade"):
                raise BillingError("subscription_scheduled", "This subscription already has scheduled changes.", 409)
            if schedule.get("status") in ("not_started", "active"):
                client.release_subscription_schedule(schedule_id, preserve_cancel_date=True)
            subscription, _ = processor.sync_subscription(db, settings, client, subscription["id"], now)
            if subscription is None:
                raise BillingError("no_subscription", "There is no active subscription to change.", 409)
        price_id = price_for_plan(db, settings, client, target)
        item = mirror.first_item(subscription)
        if item is None:
            raise BillingError("no_subscription", "There is no active subscription to change.", 409)
        public = settings.public_url
        session = client.create_portal_session({
            "customer": customer_id,
            "configuration": settings.stripe_portal_upgrade_configuration_id,
            "return_url": f"{public}/account/billing",
            "flow_data": {
                "type": "subscription_update_confirm",
                "after_completion": {"type": "redirect", "redirect": {"return_url": f"{public}/account/billing?upgrade=confirmed"}},
                "subscription_update_confirm": {
                    "subscription": subscription["id"],
                    "items": [{"id": item["id"], "price": price_id, "quantity": 1}],
                },
            },
        })
        return _https(session.get("url"))


def _phase_extras(phase: Mapping[str, Any], settings: Settings) -> dict[str, Any]:
    """Settings a schedule update would otherwise unset: discounts and automatic tax."""
    extras: dict[str, Any] = {}
    discounts = []
    for entry in phase.get("discounts") or []:
        if not isinstance(entry, Mapping):
            continue
        for key in ("discount", "coupon", "promotion_code"):
            value = object_id(entry.get(key))
            if value:
                discounts.append({key: value})
                break
    if discounts:
        extras["discounts"] = discounts
    if settings.stripe_automatic_tax or (phase.get("automatic_tax") or {}).get("enabled"):
        extras["automatic_tax"] = {"enabled": True}
    return extras


def schedule_downgrade(
    db: Database, settings: Settings, client: StripeClient, account_id: str, plan_code: str, now: int | None = None
) -> dict[str, Any]:
    """Subscription schedule: phase 1 keeps the current price until the period end, phase 2
    uses the lower price; the schedule then releases the subscription."""
    now = mirror.now_or(now)
    target = _plan(settings, plan_code)
    customer_id = _customer_id(db, account_id)
    with account_lock(account_id):
        subscription, current = _current_subscription(db, settings, client, customer_id, now)
        if mirror.plan_rank(target) >= mirror.plan_rank(current):
            raise BillingError("not_a_downgrade", "Choose a lower plan to downgrade.")
        if subscription.get("cancel_at_period_end") or subscription.get("cancel_at"):
            raise BillingError("subscription_ending", "This subscription is set to end. Renew it before changing plans.", 409)
        item = mirror.first_item(subscription)
        if item is None or item.get("current_period_end") is None:
            raise BillingError("no_subscription", "There is no active subscription to change.", 409)
        period_end = int(item["current_period_end"])
        price_id = price_for_plan(db, settings, client, target)
        schedule_id = object_id(subscription.get("schedule"))
        if schedule_id:
            schedule = client.retrieve_subscription_schedule(schedule_id)
            if not (schedule.get("metadata") or {}).get("starling_downgrade"):
                raise BillingError("subscription_scheduled", "This subscription already has scheduled changes.", 409)
        else:
            schedule = client.create_schedule_from_subscription(subscription["id"])
        phases = schedule.get("phases") or []
        start = (schedule.get("current_phase") or {}).get("start_date") or (phases[0].get("start_date") if phases else None)
        start = int(start if start is not None else item.get("current_period_start") or now)
        extras = _phase_extras(phases[0] if phases else {}, settings)
        client.update_subscription_schedule(schedule["id"], {
            "end_behavior": "release",
            "proration_behavior": "none",
            "metadata": {"starling_downgrade": target.code, "account_id": account_id},
            "phases": [
                {"items": [{"price": mirror.item_price_id(item), "quantity": 1}], "start_date": start,
                 "end_date": period_end, "proration_behavior": "none", **extras},
                {"items": [{"price": price_id, "quantity": 1}], "duration": {"interval": "month", "interval_count": 1},
                 "proration_behavior": "none", **extras},
            ],
        })
        processor.sync_subscription(db, settings, client, subscription["id"], now)
        return {"plan_code": target.code, "effective_at": period_end, "schedule_id": schedule["id"]}


def resume_subscription(db: Database, settings: Settings, client: StripeClient, account_id: str, now: int | None = None) -> None:
    """"Keep Starter": take back a cancellation that has not happened yet, without a trip to Stripe's pages."""
    now = mirror.now_or(now)
    customer_id = _customer_id(db, account_id)
    with account_lock(account_id):
        subscription, _plan_ = _current_subscription(db, settings, client, customer_id, now)
        if not (subscription.get("cancel_at_period_end") or subscription.get("cancel_at")):
            raise BillingError("not_cancelling", "Your plan is not set to end.", 409)
        client.update_subscription(subscription["id"], {"cancel_at_period_end": False})
        processor.sync_subscription(db, settings, client, subscription["id"], now)


def refresh_after_stripe(db: Database, settings: Settings, client: StripeClient, account_id: str, now: int | None = None) -> None:
    """Back from Stripe's pages: read the subscription now rather than wait for its webhook.

    Webhooks stay the source of truth and nothing is granted here that a paid invoice does not
    already justify; this only means the page the customer lands on is not a minute out of date.
    """
    now = mirror.now_or(now)
    with db.read() as conn:
        row = mirror.customer_row_for_account(conn, account_id)
    if row is None or row["deleted_at"] is not None:
        return
    with account_lock(account_id):
        _live_subscriptions(db, settings, client, row["customer_id"], now)


def undo_downgrade(db: Database, settings: Settings, client: StripeClient, account_id: str, now: int | None = None) -> None:
    now = mirror.now_or(now)
    customer_id = _customer_id(db, account_id)
    with account_lock(account_id):
        subscription, _plan_ = _current_subscription(db, settings, client, customer_id, now)
        schedule_id = object_id(subscription.get("schedule"))
        schedule = client.retrieve_subscription_schedule(schedule_id) if schedule_id else None
        if schedule is None or not (schedule.get("metadata") or {}).get("starling_downgrade"):
            raise BillingError("no_scheduled_change", "There is no scheduled plan change to undo.", 409)
        if schedule.get("status") in ("not_started", "active"):
            client.release_subscription_schedule(schedule["id"], preserve_cancel_date=True)
        processor.sync_subscription(db, settings, client, subscription["id"], now)


def cancel_for_deletion(
    db: Database, settings: Settings, account_id: str, *, client: StripeClient | None = None, now: int | None = None
) -> list[str]:
    """Account deletion: cancel every live subscription of the account immediately (no
    proration, no final invoice), expire open Checkout Sessions and mirror the result.
    Returns the canceled subscription ids. Raises on Stripe errors so deletion never
    proceeds while the customer can still be charged. Async callers: `cancel_for_deletion_async`."""
    now = mirror.now_or(now)
    with db.read() as conn:
        row = mirror.customer_row_for_account(conn, account_id)
    if row is None or row["deleted_at"] is not None:
        return []
    own_client = client is None
    client = client or default_client(settings)
    try:
        with account_lock(account_id):
            customer_id = row["customer_id"]
            try:
                subscriptions = client.list_subscriptions(customer=customer_id, status="all")
            except StripeNotFoundError:
                subscriptions = []
            canceled = []
            for subscription in subscriptions:
                if subscription.get("status") in mirror.TERMINAL_STATUSES:
                    continue
                client.cancel_subscription(subscription["id"], prorate=False, invoice_now=False)
                canceled.append(subscription["id"])
            for subscription_id in canceled:
                processor.sync_subscription(db, settings, client, subscription_id, now)
            _expire_open_sessions(db, client, account_id, customer_id)
            return canceled
    finally:
        if own_client:
            client.close()


async def cancel_for_deletion_async(db: Database, settings: Settings, account_id: str) -> list[str]:
    return await anyio.to_thread.run_sync(partial(cancel_for_deletion, db, settings, account_id))


# --------------------------------------------------------------------------- status


RETURN_EXPIRED_AFTER_S = 3600


def return_session_status(db: Database, account_id: str, session_id: str | None, now: int | None = None) -> str:
    """Status shown by `billing/return.html` for the signed-in account's own Checkout Session.

    `pending`: still open here, so the customer came back from Checkout and Stripe's
    confirmation has not been processed yet (the page polls); `complete`; `expired` (also an
    open session an hour past its expiry); `not_found` for unknown, malformed or another
    account's ids. Reads only the local table, never Stripe, and grants nothing."""
    now = mirror.now_or(now)
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return "not_found"
    with db.read() as conn:
        row = conn.execute(
            "SELECT status, expires_at FROM checkout_sessions WHERE session_id = ? AND account_id = ?",
            (session_id, account_id),
        ).fetchone()
    if row is None:
        return "not_found"
    if row["status"] == "open":
        return "expired" if now >= int(row["expires_at"]) + RETURN_EXPIRED_AFTER_S else "pending"
    return row["status"]


def billing_status(db: Database, settings: Settings, account_id: str, now: int | None = None) -> dict[str, Any]:
    """`GET /billing/status`: the account state of CONTRACTS 6.2 (polled by the return page)."""
    return {"state": usage_service.account_state(db, settings, account_id, now)}
