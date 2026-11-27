"""Owner actions for the dashboard routes and the CLI. Every call writes exactly one `admin_audit` row.

The row names who acted (`admin_id`: the owner's account id, or "cli"), the action, the target account
and a params summary: the outcome, counts and the owner's own reason, never secrets, tokens or customer
content. When the action owns its database change, the audit row is written in the same transaction.
Credit grants and billing resyncs change state through the usage and billing workstreams, so their
row follows in a second transaction.

Outcomes: "done", "invalid" (a form value is missing or out of range), "not_found", "refused_self"
(an owner may not suspend their own account or disconnect its browsers), and per action
"not_active", "not_suspended", "not_allowed", "not_paused", "failed", "not_configured", "unconfirmed",
"no_customer", "stripe_error"; the AI test records "passed" or "failed".
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..auth import accounts
from ..auth.admin import audit
from ..billing import reconcile
from ..config import Settings
from ..db import Database
from ..devices import tokens as device_tokens
from ..usage import grants
from ..usage import service as usage_service
from .common import AI_TEST_KEY, as_int, text, write_state

log = logging.getLogger(__name__)

MAX_REASON_LENGTH = 200
MAX_MESSAGE_LENGTH = 200
MAX_LEDGER_ACTOR_LENGTH = 200
PAUSE_RETRY_AFTER_S = usage_service.DEFAULT_PAUSE_RETRY_S
MAX_PAUSE_RETRY_AFTER_S = 86_400
REVOKE_REASON = "admin_revoked"
RESYNC_COUNTERS = (
    "subscriptions", "mirror_changed", "missing", "periods_granted", "upgrades_applied", "periods_created",
    "canceled_duplicates",
)
_CREDITS_RE = re.compile(r"^\d{1,6}$")


@dataclass(frozen=True)
class Actor:
    """Who acts: `admin_id` goes to `admin_audit`, `name` (the owner's email) to the credit ledger."""

    admin_id: str
    name: str
    ip_prefix: str | None = None


CLI_ACTOR = Actor("cli", "cli")


@dataclass(frozen=True)
class Result:
    outcome: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.outcome == "done"


def clean_reason(raw: Any, *, required: bool = True, limit: int = MAX_REASON_LENGTH) -> str | None:
    """The one-line reason; "" when optional and empty; None when required and empty, or too long."""
    value = " ".join(raw.split()) if isinstance(raw, str) else ""
    if len(value) > limit or (required and not value):
        return None
    return value


def parse_credits(raw: Any) -> int | None:
    value = raw.strip() if isinstance(raw, str) else ""
    if not _CREDITS_RE.match(value):
        return None
    amount = int(value)
    return amount if 1 <= amount <= grants.MAX_CREDIT_CHANGE else None


def parse_retry_after_s(raw: Any) -> int | None:
    """The `Retry-After` customers get while paused: 1 second to a day; the default for None or ""."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return PAUSE_RETRY_AFTER_S
    value = as_int(raw) if not isinstance(raw, str) else (int(raw.strip()) if raw.strip().isdigit() else None)
    return value if value is not None and 1 <= value <= MAX_PAUSE_RETRY_AFTER_S else None


def _audit(conn: sqlite3.Connection, actor: Actor, action: str, target: str | None, params: dict[str, Any], now: int) -> None:
    audit(conn, actor.admin_id, action, target, now, params, actor.ip_prefix)


def _record(db: Database, actor: Actor, action: str, target: str | None, params: dict[str, Any], now: int) -> None:
    db.write(lambda conn: _audit(conn, actor, action, target, params, now))


def _account_status(conn: sqlite3.Connection, account_id: str) -> str | None:
    row = conn.execute("SELECT status FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return row["status"] if row is not None else None


# --------------------------------------------------------------------------- customers


def grant_credits(
    db: Database, settings: Settings, actor: Actor, account_id: str, raw_credits: Any, raw_reason: Any, now: int
) -> Result:
    """Ledger kind `admin_grant` into the account's current period (`usage.grants`)."""
    amount, reason = parse_credits(raw_credits), clean_reason(raw_reason)
    params: dict[str, Any] = {
        "credits": amount if amount is not None else text(raw_credits, 16),
        "reason": reason if reason is not None else text(raw_reason, MAX_REASON_LENGTH),
    }
    with db.read() as conn:
        status = _account_status(conn, account_id)
    details: dict[str, Any] = {}
    if status is None:
        outcome = "not_found"
    elif amount is None or reason is None:
        outcome = "invalid"
    elif status in ("deleting", "deleted"):
        outcome = "not_allowed"
    else:
        ledger_actor = actor.name if 0 < len(actor.name) <= MAX_LEDGER_ACTOR_LENGTH else actor.admin_id
        try:
            change = grants.grant_credits(db, settings, account_id, amount, ledger_actor, reason, now)
        except usage_service.UsageError as exc:
            outcome, details = "failed", {"message": exc.message}
            params["code"] = exc.code
        else:
            outcome, details = "done", {"credits": amount, "remaining": change["usage"]["remaining"]}
            params["ledger_id"] = change["ledger_id"]
    _record(db, actor, "grant_credits", account_id, {"outcome": outcome, **params}, now)
    return Result(outcome, details)


def suspend_account(db: Database, actor: Actor, account_id: str, raw_reason: Any, now: int) -> Result:
    """Status `suspended` and a new `auth_epoch`, which signs out every web session and device."""
    reason = clean_reason(raw_reason)

    def transaction(conn: sqlite3.Connection) -> str:
        status = _account_status(conn, account_id)
        if status is None:
            outcome = "not_found"
        elif account_id == actor.admin_id:
            outcome = "refused_self"
        elif reason is None:
            outcome = "invalid"
        elif status != "active":
            outcome = "not_active"
        else:
            conn.execute("UPDATE accounts SET status = 'suspended' WHERE id = ?", (account_id,))
            accounts.bump_auth_epoch(conn, account_id)
            outcome = "done"
        shown = reason if reason is not None else text(raw_reason, MAX_REASON_LENGTH)
        _audit(conn, actor, "suspend_account", account_id, {"outcome": outcome, "reason": shown}, now)
        return outcome

    return Result(db.write(transaction))


def unsuspend_account(db: Database, actor: Actor, account_id: str, raw_reason: Any, now: int) -> Result:
    """Status back to `active`. A billing dispute (`subscriptions.disputed_at`) is never touched, so its
    suspension of drafting stays until the dispute closes."""
    reason = clean_reason(raw_reason)

    def transaction(conn: sqlite3.Connection) -> tuple[str, bool]:
        status = _account_status(conn, account_id)
        if status is None:
            outcome = "not_found"
        elif reason is None:
            outcome = "invalid"
        elif status != "suspended":
            outcome = "not_suspended"
        else:
            conn.execute("UPDATE accounts SET status = 'active' WHERE id = ?", (account_id,))
            outcome = "done"
        dispute_open = conn.execute(
            "SELECT 1 FROM subscriptions WHERE account_id = ? AND disputed_at IS NOT NULL LIMIT 1", (account_id,)
        ).fetchone() is not None
        shown = reason if reason is not None else text(raw_reason, MAX_REASON_LENGTH)
        params = {"outcome": outcome, "reason": shown, "dispute_open": dispute_open}
        _audit(conn, actor, "unsuspend_account", account_id, params, now)
        return outcome, dispute_open

    outcome, dispute_open = db.write(transaction)
    return Result(outcome, {"dispute_open": dispute_open})


def revoke_devices(db: Database, actor: Actor, account_id: str, now: int) -> Result:
    """Revoke every device token of the account; web sessions stay signed in."""

    def transaction(conn: sqlite3.Connection) -> tuple[str, int]:
        revoked = 0
        if _account_status(conn, account_id) is None:
            outcome = "not_found"
        elif account_id == actor.admin_id:
            outcome = "refused_self"
        else:
            revoked = device_tokens.revoke_all(conn, account_id, now, REVOKE_REASON)
            outcome = "done"
        _audit(conn, actor, "revoke_devices", account_id, {"outcome": outcome, "revoked": revoked}, now)
        return outcome, revoked

    outcome, revoked = db.write(transaction)
    return Result(outcome, {"revoked": revoked})


def resync_billing(
    db: Database, settings: Settings, client: Any, actor: Actor, account_id: str, confirmed: bool, now: int
) -> Result:
    """`billing.reconcile.resync_account` for one account. No Stripe client (Stripe not configured) or no
    confirmation: nothing is called. The resync takes the account's billing lock itself."""
    params: dict[str, Any] = {}
    details: dict[str, Any] = {}
    if client is None:
        outcome = "not_configured"
    elif not confirmed:
        outcome = "unconfirmed"
    else:
        try:
            result = reconcile.resync_account(db, settings, client, account_id, now=now)
        except Exception:  # it never raises for Stripe problems; anything else is a bug worth a trace
            log.exception("billing resync failed unexpectedly for account=%s", account_id)
            result = None
        if not isinstance(result, dict):
            outcome = "failed"
        elif result.get("ok") is True:
            outcome = "done"
            params.update({name: result[name] for name in RESYNC_COUNTERS if as_int(result.get(name)) is not None})
            params["entitlement_changed"] = result.get("entitlement_changed") is True
            details.update(params)
        elif result.get("reason") == "no_customer":
            outcome = "no_customer"
        else:
            outcome = "stripe_error"
            params["message"] = details["message"] = text(result.get("message"), 120) or "no details"
    _record(db, actor, "resync_billing", account_id, {"outcome": outcome, **params}, now)
    return Result(outcome, details)


# --------------------------------------------------------------------------- AI provider


def pause_ai(db: Database, actor: Actor, raw_reason: Any, raw_message: Any, now: int, raw_retry_after_s: Any = None) -> Result:
    """`service_state` `ai_paused` = {"paused": true, "message", "retry_after_s"} (CONTRACTS 11).
    `message` is what customers see; the reason stays in the audit log. `retry_after_s` is the
    `Retry-After` on the 503 `service_paused` refusals (CONTRACTS 6.3); the dashboard leaves it at the
    default and the CLI can set it."""
    reason = clean_reason(raw_reason)
    message = clean_reason(raw_message, required=False, limit=MAX_MESSAGE_LENGTH)
    retry_after_s = parse_retry_after_s(raw_retry_after_s)

    def transaction(conn: sqlite3.Connection) -> str:
        if reason is None or message is None or retry_after_s is None:
            outcome = "invalid"
            params: dict[str, Any] = {"reason": text(raw_reason, MAX_REASON_LENGTH)}
        else:
            outcome = "done"
            shown = message or usage_service.PAUSED_MESSAGE
            value = {"paused": True, "message": shown, "retry_after_s": retry_after_s}
            write_state(conn, usage_service.PAUSE_KEY, value, now)
            params = {"reason": reason, "message": shown, "retry_after_s": retry_after_s}
        _audit(conn, actor, "ai_pause", None, {"outcome": outcome, **params}, now)
        return outcome

    return Result(db.write(transaction))


def resume_ai(db: Database, actor: Actor, raw_reason: Any, now: int) -> Result:
    """Clear `ai_paused`. The global budgets can still keep drafting paused."""
    reason = clean_reason(raw_reason, required=False)

    def transaction(conn: sqlite3.Connection) -> str:
        if reason is None:
            outcome = "invalid"
        else:
            removed = conn.execute("DELETE FROM service_state WHERE key = ?", (usage_service.PAUSE_KEY,)).rowcount
            outcome = "done" if removed else "not_paused"
        params = {"outcome": outcome, **({"reason": reason} if reason else {})}
        _audit(conn, actor, "ai_resume", None, params, now)
        return outcome

    return Result(db.write(transaction))


def summarize_ai_check(result: Any, now: int) -> dict[str, Any]:
    """What `admin:ai_last_test` keeps of an `ai_check` answer: codes, models and latency, never messages."""
    payload = result if isinstance(result, dict) else {}
    raw_checks = payload.get("results")
    checks = [item for item in raw_checks if isinstance(item, dict)] if isinstance(raw_checks, list) else []
    ok = payload.get("ok") is True and bool(checks)
    failing = next((item for item in checks if item.get("ok") is not True), None)
    chosen = failing or (checks[0] if checks else {})
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    error_code = None
    if not ok:
        error_code = text((failing or {}).get("error_code"), 60) or text(error.get("code"), 60) or "check_failed"
    return {
        "at": int(now),
        "ok": ok,
        "latency_ms": as_int(chosen.get("latency_ms")) or 0,
        "model": text(chosen.get("model"), 100),
        "error_code": error_code,
        "models": [
            {
                "model": text(item.get("model"), 100),
                "ok": item.get("ok") is True,
                "latency_ms": as_int(item.get("latency_ms")) or 0,
                "error_code": text(item.get("error_code"), 60) or None,
            }
            for item in checks
        ],
    }


def store_ai_test(db: Database, actor: Actor, summary: dict[str, Any], now: int) -> Result:
    outcome = "passed" if summary.get("ok") else "failed"

    def transaction(conn: sqlite3.Connection) -> None:
        write_state(conn, AI_TEST_KEY, summary, now)
        _audit(conn, actor, "ai_test", None, {"outcome": outcome, "error_code": summary.get("error_code")}, now)

    db.write(transaction)
    return Result(outcome, {"error_code": summary.get("error_code")})
