"""Stripe webhook intake (CONTRACTS sections 8 and 10: `POST /webhooks/stripe`).

The route reads the raw body, verifies the signature against the current or previous
secret, rejects events whose `livemode` does not match `STRIPE_MODE`, stores the event in
`billing_events` (status `received`, payload kept) idempotently on `event_id`, and
answers 200 at once. The serial processor does the work later. A duplicate delivery of
an event that is already `processed` (or `ignored`) is answered 200 without any work.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..db import Database
from . import mirror, signatures
from .stripe_client import object_id

log = logging.getLogger(__name__)

SUBSCRIBED_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.expired",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.pending_update_applied",
        "customer.subscription.pending_update_expired",
        "invoice.paid",
        "invoice.payment_failed",
        "invoice.payment_action_required",
        "charge.refunded",
        "charge.dispute.created",
        "charge.dispute.closed",
        "customer.deleted",
        "subscription_schedule.released",
        "subscription_schedule.canceled",
    }
)
MAX_BODY_BYTES = 1_000_000
REQUEUE_STATUSES = ("received", "failed")


class WebhookRejected(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IntakeResult:
    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None
    enqueue: bool = False


def verify_event(settings: Settings, raw_body: bytes, signature_header: str | None, now: int) -> dict[str, Any]:
    """Return the verified event or raise `WebhookRejected`."""
    secrets = [s for s in (settings.stripe_webhook_secret, settings.stripe_webhook_secret_previous) if s]
    if not secrets:
        raise WebhookRejected(503, "billing_unconfigured", "Stripe webhooks are not configured.")
    if len(raw_body) > MAX_BODY_BYTES:
        raise WebhookRejected(413, "payload_too_large", "Webhook payload is too large.")
    try:
        signatures.verify(raw_body, signature_header, secrets, tolerance_s=signatures.DEFAULT_TOLERANCE_S, now=now)
    except signatures.SignatureError as exc:
        raise WebhookRejected(400, "bad_signature", "Invalid Stripe signature.") from exc
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # A deeply nested body raises RecursionError rather than a ValueError; it is still
        # only a malformed payload, so it gets the same shaped 400 as any other.
        raise WebhookRejected(400, "bad_request", "Webhook payload is not JSON.") from exc
    if not is_event(event):
        raise WebhookRejected(400, "bad_request", "Webhook payload is not a Stripe event.")
    if event["livemode"] != mirror.livemode(settings):
        raise WebhookRejected(400, "livemode_mismatch", "Event mode does not match STRIPE_MODE.")
    return event


def is_event(event: Any) -> bool:
    return (
        isinstance(event, dict)
        and isinstance(event.get("id"), str)
        and event["id"].startswith("evt_")
        and len(event["id"]) <= 255
        and isinstance(event.get("type"), str)
        and isinstance(event.get("livemode"), bool)
        and isinstance(event.get("data"), dict)
        and isinstance(event["data"].get("object"), dict)
    )


def payload_customer(event: dict[str, Any]) -> str | None:
    obj = event["data"]["object"]
    if obj.get("object") == "customer":
        return object_id(obj.get("id"))
    return object_id(obj.get("customer"))


def record_event(conn: sqlite3.Connection, event: dict[str, Any], now: int) -> tuple[bool, str]:
    """Insert the event once. Returns (inserted, current status)."""
    row = mirror.customer_row(conn, payload_customer(event))
    cursor = conn.execute(
        "INSERT INTO billing_events (event_id, type, livemode, object_id, account_id, payload_json, status, "
        "attempts, received_at) VALUES (?, ?, ?, ?, ?, ?, 'received', 0, ?) ON CONFLICT(event_id) DO NOTHING",
        (
            event["id"],
            event["type"],
            1 if event["livemode"] else 0,
            object_id(event["data"]["object"].get("id")),
            row["account_id"] if row is not None else None,
            json.dumps(event, separators=(",", ":"), sort_keys=True),
            int(now),
        ),
    )
    if cursor.rowcount == 1:
        return True, "received"
    existing = conn.execute("SELECT status FROM billing_events WHERE event_id = ?", (event["id"],)).fetchone()
    return False, existing["status"]


def _stored(event_id: str, inserted: bool, status: str) -> IntakeResult:
    body: dict[str, Any] = {"received": True}
    if not inserted:
        body["duplicate"] = True
    return IntakeResult(200, body, event_id, enqueue=status in REQUEUE_STATUSES)


def _rejection(exc: WebhookRejected) -> IntakeResult:
    return IntakeResult(exc.status_code, {"error": {"code": exc.code, "message": exc.message}})


def _alert_mode(conn: sqlite3.Connection, now: int) -> None:
    mirror.raise_alert(
        conn, "livemode_mismatch", "webhook",
        "A signed webhook event from the other Stripe mode reached this endpoint; it was rejected.", now,
    )


def intake(db: Database, settings: Settings, raw_body: bytes, signature_header: str | None, now: int) -> IntakeResult:
    try:
        event = verify_event(settings, raw_body, signature_header, now)
    except WebhookRejected as exc:
        if exc.code == "livemode_mismatch":
            db.write(lambda conn: _alert_mode(conn, now))
        return _rejection(exc)
    if event["type"] not in SUBSCRIBED_EVENT_TYPES:
        return IntakeResult(200, {"received": True, "ignored": True}, event["id"])
    inserted, status = db.write(lambda conn: record_event(conn, event, now))
    return _stored(event["id"], inserted, status)


async def intake_async(
    db: Database, settings: Settings, raw_body: bytes, signature_header: str | None, now: int
) -> IntakeResult:
    try:
        event = verify_event(settings, raw_body, signature_header, now)
    except WebhookRejected as exc:
        if exc.code == "livemode_mismatch":
            await db.write_async(lambda conn: _alert_mode(conn, now))
        return _rejection(exc)
    if event["type"] not in SUBSCRIBED_EVENT_TYPES:
        return IntakeResult(200, {"received": True, "ignored": True}, event["id"])
    inserted, status = await db.write_async(lambda conn: record_event(conn, event, now))
    return _stored(event["id"], inserted, status)
