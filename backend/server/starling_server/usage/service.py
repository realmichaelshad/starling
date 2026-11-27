"""Usage reservations and the authoritative account state.

Interface fixed by docs/architecture/CONTRACTS.md sections 6.5, 7 and 14; implemented by the
usage workstream. All writes go through `Database.write` (one writer thread per process).

Every function here is synchronous and may wait for the writer thread. Async code calls them
from a worker thread (`asyncio.to_thread`, `run_in_threadpool`), never on the event loop.
Inside write transactions the entitlement comes from `entitlements.entitlement_in_transaction`.
Columns maintained by the services proxy (`usage_reservations.cost_micros`, `ai_calls`,
`heartbeat_at`; `usage_counters.cost_micros`, `background_micros`) are never overwritten here
(`activate` only fills an empty `heartbeat_at`).

Accounting model, per account and entitlement period (table `usage_counters`):

- `reserved`  = credits of reservations in state `reserved` or `active`
- `committed` = sum of `credit_ledger` rows of kind `commit` (at most one per reservation id)
- `granted`   = `admin_grant` minus `admin_debit` ledger amounts (see `usage.grants`)
- available   = allowance + granted - committed - reserved

A reservation is *charged* when it has a `commit` ledger row: state `committed`, or `expired`
with no `release_reason` (rows that an older sweep settled as charged). A draft is charged only when
the customer received one (`finish` with a usable result from a successful AI call), or stopped it
themselves after text had appeared (`CUSTOMER_ENDED`). A preview that was then withheld, and every
failure on Starling's side, is released even after text was shown (CONTRACTS section 7).
Each state change and its counter change happen in the same writer transaction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from ..billing import entitlements
from ..config import Settings, get_settings
from ..db import Database

log = logging.getLogger(__name__)

UPGRADE_PATH = "/pricing?from=extension&reason=limit"
RESERVATION_TTL_S = 300  # hard wall for one metered request
RESULT_TTL_S = 900  # stored results are kept for 15 minutes
CONSENT_KIND = "extension_disclosure"
WEB_CONSENT_KIND = "web_disclosure"  # the website's writing page shows its own disclosure (web/write.py)
PAUSE_KEY = "ai_paused"
DEFAULT_PAUSE_RETRY_S = 300
LIVE_STATES = ("reserved", "active")
GLOBAL_BUDGET_SCOPES = ("global_day", "global_month")
FREE_BUDGET_SCOPE = "free_day"
MAX_IDEMPOTENCY_KEY_LENGTH = 200
ARCHIVED_KEY_MARK = "#"  # a finished attempt keeps its row under "<key>#<reservation id>"
MAX_REASON_LENGTH = 64
MINUTE_S = 60
DAY_S = 86_400

PAUSED_MESSAGE = "Drafting is paused for a short while. Please try again later."
# The Free plan's own daily AI budget (scope `free_day`) ran out: paid plans keep drafting.
FREE_PAUSED_MESSAGE = "Drafting on the Free plan is paused for the rest of the day. Paid plans keep drafting."
# The customer ended the request after text had appeared: they saw part of it, so it is used.
CUSTOMER_ENDED = ("cancelled", "client_disconnected")


class UsageError(Exception):
    """A request that must be refused; maps to the API error body in CONTRACTS section 6.3."""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        retry_after_s: int | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.retry_after_s = retry_after_s
        self.usage = usage

    @property
    def upgrade_path(self) -> str | None:
        """Every 402 carries the same-origin upgrade path (CONTRACTS section 6.3)."""
        return UPGRADE_PATH if self.http_status == 402 else None

    def body(self) -> dict[str, Any]:
        """The API error body: `{"error": {"code", "message", "retry_after_s"?, "usage"?, "upgrade_path"?}}`."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after_s is not None:
            error["retry_after_s"] = self.retry_after_s
        if self.usage is not None:
            error["usage"] = self.usage
        if self.upgrade_path is not None:
            error["upgrade_path"] = self.upgrade_path
        return {"error": error}


@dataclass(frozen=True)
class Reservation:
    id: str
    account_id: str
    idempotency_key: str
    credits: int
    period_id: str
    state: str


@dataclass(frozen=True)
class Replay:
    """A completed request with the same idempotency key: return this instead of running again."""

    result_json: str
    usage: dict[str, Any]


def payload_hash(body: bytes) -> str:
    """SHA-256 of the canonical JSON form of a request body (key order and spacing ignored)."""
    try:
        canonical = json.dumps(json.loads(body or b"null"), sort_keys=True, separators=(",", ":"))
    except (ValueError, UnicodeDecodeError):
        return hashlib.sha256(body).hexdigest()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- helpers


_bound_settings: "weakref.WeakKeyDictionary[Database, Settings]" = weakref.WeakKeyDictionary()
_bound_lock = threading.Lock()


def bind_settings(db: Database, settings: Settings) -> None:
    """Remember the settings used with `db`. `replay()` has no settings parameter and needs them
    for the usage block; `usage.lifecycle.startup` binds the app's settings."""
    with _bound_lock:
        if _bound_settings.get(db) is not settings:
            _bound_settings[db] = settings


def _settings_for(db: Database) -> Settings:
    with _bound_lock:
        settings = _bound_settings.get(db)
    return settings if settings is not None else get_settings()


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


def credits_per_request(settings: Settings) -> int:
    """One credit per metered request, times `AI_CREDIT_MULTIPLIER`."""
    return 1 * settings.ai_credit_multiplier


def _upgradable(settings: Settings, plan_code: str) -> bool:
    """Whether a public paid plan ranks above this one (price, then allowance; billing.mirror.plan_rank)."""
    current = settings.plans.get(plan_code) or settings.plans.free
    rank = lambda plan: (plan.amount_cents or 0, plan.monthly_credits)  # noqa: E731
    return any(plan.is_paid and rank(plan) > rank(current) for plan in settings.plans.public_plans())


@contextmanager
def _snapshot(db: Database) -> Iterator[sqlite3.Connection]:
    """A consistent read transaction on this thread's reader connection."""
    with db.read() as conn:
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("ROLLBACK")


def _bump(
    conn: sqlite3.Connection,
    account_id: str,
    period_id: str,
    *,
    committed: int = 0,
    reserved: int = 0,
    granted: int = 0,
    released_with_spend: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO usage_counters (account_id, period_id, committed, reserved, granted, released_with_spend)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_id, period_id) DO UPDATE SET
            committed = committed + excluded.committed,
            reserved = reserved + excluded.reserved,
            granted = granted + excluded.granted,
            released_with_spend = released_with_spend + excluded.released_with_spend
        """,
        (account_id, period_id, committed, reserved, granted, released_with_spend),
    )


def _counters(conn: sqlite3.Connection, account_id: str, period_id: str) -> tuple[int, int, int, int]:
    row = conn.execute(
        "SELECT committed, reserved, granted, cost_micros FROM usage_counters WHERE account_id = ? AND period_id = ?",
        (account_id, period_id),
    ).fetchone()
    if row is None:
        return 0, 0, 0, 0
    return int(row["committed"]), int(row["reserved"]), int(row["granted"]), int(row["cost_micros"])


def usage_block(
    settings: Settings,
    entitlement: entitlements.Entitlement,
    committed: int,
    reserved: int,
    granted: int,
    cost_micros: int,
) -> dict[str, Any]:
    """The `usage` object of CONTRACTS section 6.2."""
    allowance = int(entitlement.allowance)
    remaining = max(0, allowance + granted - committed - reserved)
    return {
        "unit": settings.plans.unit_name,
        "unit_label": settings.plans.unit_label,
        "allowance": allowance,
        "used": committed,
        "reserved": reserved,
        "remaining": remaining,
        "resets_at": int(entitlement.period_end),
        # used + reserved >= 0.8 x allowance, in integers.
        "near_limit": (committed + reserved) * 5 >= allowance * 4 and remaining > 0,
        "fair_use_blocked": cost_micros >= int(entitlement.cost_ceiling_micros),
        # Credits one press uses, so clients never hard-code "1 draft".
        "per_request": credits_per_request(settings),
    }


def _usage_from_conn(conn, settings: Settings, entitlement, account_id: str) -> dict[str, Any]:
    return usage_block(settings, entitlement, *_counters(conn, account_id, entitlement.period_id))


def usage_for_account(db: Database, settings: Settings, account_id: str, now: int | None = None) -> dict[str, Any]:
    """The current usage block for an account (same numbers as `account_state`)."""
    now = _now(now)
    entitlement = entitlements.current_entitlement(db, settings, account_id, now)
    with _snapshot(db) as conn:
        return _usage_from_conn(conn, settings, entitlement, account_id)


def _usage_in_transaction(conn: sqlite3.Connection, settings: Settings, account_id: str, now: int) -> dict[str, Any]:
    entitlement = entitlements.entitlement_in_transaction(conn, settings, account_id, now)
    return _usage_from_conn(conn, settings, entitlement, account_id)


def mask_email(email: str | None) -> str:
    local, sep, domain = (email or "").strip().rpartition("@")
    if not sep or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


# --------------------------------------------------------------------------- pause and budgets


@dataclass(frozen=True)
class _Pause:
    message: str
    retry_after_s: int
    scope: str = "all"  # "all", or "free" when only the Free plan is paused


def budget_window_keys(now: int) -> tuple[str, str]:
    """`budget_counters.window_key` for the UTC day (`YYYY-MM-DD`) and month (`YYYY-MM`)."""
    moment = datetime.fromtimestamp(now, timezone.utc)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%Y-%m")


def _seconds_to_next_month(now: int) -> int:
    moment = datetime.fromtimestamp(now, timezone.utc)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return max(1, int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp()) - now)


def _owner_pause(conn: sqlite3.Connection) -> _Pause | None:
    """`service_state['ai_paused']`: JSON `true`, or `{"paused": true, "message"?, "retry_after_s"?}`."""
    row = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (PAUSE_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["value_json"])
    except ValueError:
        log.warning("service_state %s is not valid JSON; treating the AI service as paused", PAUSE_KEY)
        return _Pause(PAUSED_MESSAGE, DEFAULT_PAUSE_RETRY_S)
    if isinstance(value, dict):
        if not value.get("paused"):
            return None
        message = value.get("message")
        retry = value.get("retry_after_s")
        return _Pause(
            message.strip() if isinstance(message, str) and message.strip() else PAUSED_MESSAGE,
            retry if isinstance(retry, int) and not isinstance(retry, bool) and retry > 0 else DEFAULT_PAUSE_RETRY_S,
        )
    if isinstance(value, int) and value == 1:  # JSON true (or 1)
        return _Pause(PAUSED_MESSAGE, DEFAULT_PAUSE_RETRY_S)
    return None


def _budget_pause(conn: sqlite3.Connection, now: int, scopes: tuple[str, ...]) -> _Pause | None:
    day_key, month_key = budget_window_keys(now)
    for scope in scopes:
        row = conn.execute(
            "SELECT cost_micros, limit_micros, tripped_at FROM budget_counters WHERE scope = ? AND window_key = ?",
            (scope, month_key if scope == "global_month" else day_key),
        ).fetchone()
        if row is None:
            continue
        # Same rule as services.metering: a window stays tripped once set, and a zero limit allows no spend.
        tripped = row["tripped_at"] is not None or row["cost_micros"] >= row["limit_micros"]
        if tripped:
            retry = _seconds_to_next_month(now) if scope == "global_month" else DAY_S - (now % DAY_S)
            if scope == FREE_BUDGET_SCOPE:
                return _Pause(FREE_PAUSED_MESSAGE, retry, "free")
            return _Pause(PAUSED_MESSAGE, retry)
    return None


def _service_pause(conn: sqlite3.Connection, now: int, is_free_plan: bool) -> _Pause | None:
    pause = _owner_pause(conn) or _budget_pause(conn, now, GLOBAL_BUDGET_SCOPES)
    if pause is None and is_free_plan:
        pause = _budget_pause(conn, now, (FREE_BUDGET_SCOPE,))
    return pause


# --------------------------------------------------------------------------- reserve


def _replayable(row, now: int) -> bool:
    return (
        row["state"] == "committed"
        and row["result_json"] is not None
        and row["result_expires_at"] is not None
        and row["result_expires_at"] > now
    )


def _check_idempotency(conn, account_id: str, idempotency_key: str, body_hash: str, now: int):
    """Step 1: the existing row for this key (a replay or a finished attempt to reuse), or None."""
    row = conn.execute(
        "SELECT id, state, payload_hash, result_json, result_expires_at FROM usage_reservations "
        "WHERE account_id = ? AND idempotency_key = ?",
        (account_id, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if row["payload_hash"] != body_hash:
        raise UsageError("idempotency_mismatch", 409, "This request id was already used for a different request.")
    if _replayable(row, now):
        return row
    if row["state"] in LIVE_STATES:
        raise UsageError("duplicate_request", 409, "This request is still running.")
    return row  # released, expired, or committed without a stored result: a new attempt


def _check_account(conn, settings: Settings, account_id: str, now: int,
                   consent: tuple[str, str] | None = None) -> None:
    """Steps 2 to 5: account active, email verified, consent current, service not paused.

    `consent` is the (kind, version) of the disclosure the caller's surface shows: None for the
    extension's (`extension_disclosure` at STARLING_CONSENT_VERSION); the website's writing page
    passes its own (`web_disclosure` at `web.write.WEB_DISCLOSURE_VERSION`).
    """
    consent_kind, consent_version = consent or (CONSENT_KIND, settings.consent_version)
    account = conn.execute("SELECT status, email_verified_at FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None or account["status"] in ("deleting", "deleted"):
        raise UsageError("signed_out", 401, "Sign in to Starling again.")
    if account["status"] != "active":
        raise UsageError("account_suspended", 403, "This account is suspended. Contact support for help.")
    if account["email_verified_at"] is None:
        raise UsageError("email_unverified", 403, "Verify your email address to start drafting.")
    row = conn.execute(
        "SELECT 1 FROM consents WHERE account_id = ? AND kind = ? AND version = ? AND revoked_at IS NULL",
        (account_id, consent_kind, consent_version),
    ).fetchone()
    if row is None:
        if consent_kind == WEB_CONSENT_KIND:
            raise UsageError("consent_required", 403, "Read what Starling sends and agree to it before your first draft.")
        raise UsageError("consent_required", 403, "Review and accept the extension disclosure to continue.")
    pause = _owner_pause(conn) or _budget_pause(conn, now, GLOBAL_BUDGET_SCOPES)
    if pause is not None:
        raise UsageError("service_paused", 503, pause.message, retry_after_s=pause.retry_after_s)


def _check_rate(conn, account_id: str, now: int, limit: int, window_s: int) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM usage_reservations WHERE account_id = ? AND created_at > ?",
        (account_id, now - window_s),
    ).fetchone()[0]
    if count < limit:
        return
    retry_after = window_s
    if limit > 0:
        # A new request fits once the (count - limit + 1) oldest reservations leave the window.
        row = conn.execute(
            "SELECT created_at FROM usage_reservations WHERE account_id = ? AND created_at > ? "
            "ORDER BY created_at LIMIT 1 OFFSET ?",
            (account_id, now - window_s, count - limit),
        ).fetchone()
        if row is not None:
            retry_after = max(1, int(row["created_at"]) + window_s - now)
    raise UsageError("rate_limited", 429, "Too many requests. Please wait a moment.", retry_after_s=retry_after)


def _check_entitlement_and_limits(conn, settings: Settings, entitlement, account_id: str, now: int, credits: int) -> None:
    """Steps 6 to 9: entitlement, rate limits, available credits, fair-use ceiling."""
    if entitlement.status == "suspended":
        raise UsageError(
            "payment_required", 402, "There is a problem with this account's payment.",
            usage=_usage_from_conn(conn, settings, entitlement, account_id),
        )
    if entitlement.plan_code == settings.plans.free.code:
        pause = _budget_pause(conn, now, (FREE_BUDGET_SCOPE,))
        if pause is not None:
            raise UsageError("service_paused", 503, pause.message, retry_after_s=pause.retry_after_s)
    plan = settings.plans.get(entitlement.plan_code) or settings.plans.free
    _check_rate(conn, account_id, now, plan.rate_per_minute, MINUTE_S)
    _check_rate(conn, account_id, now, plan.rate_per_day, DAY_S)
    committed, reserved, granted, cost_micros = _counters(conn, account_id, entitlement.period_id)
    usage = usage_block(settings, entitlement, committed, reserved, granted, cost_micros)
    if int(entitlement.allowance) + granted - committed - reserved < credits:
        raise UsageError("limit_reached", 402, "You have used all drafts in this period.", usage=usage)
    if cost_micros >= int(entitlement.cost_ceiling_micros):
        raise UsageError("fair_use_exceeded", 402, "This account reached its fair-use limit for the period.", usage=usage)


def reserve(
    db: Database,
    settings: Settings,
    account_id: str,
    idempotency_key: str,
    body_hash: str,
    route: str,
    mode: str,
    now: int | None = None,
    *,
    consent: tuple[str, str] | None = None,
) -> Reservation | Replay:
    """Atomically reserve credits or return a replay; raises UsageError when refused.

    `consent` is the (kind, version) of the disclosure that must be current; None is the
    extension's (see `_check_account`)."""
    now = _now(now)
    bind_settings(db, settings)
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH
        or ARCHIVED_KEY_MARK in idempotency_key
    ):
        raise UsageError("bad_request", 400, "A valid Idempotency-Key header is required.")
    if not isinstance(body_hash, str) or not body_hash:
        raise UsageError("bad_request", 400, "The request body hash is missing.")
    credits = credits_per_request(settings)

    # Read pass: refusals that come before the entitlement check surface here without taking the
    # writer. Everything is decided again, atomically, inside the write transaction below.
    with _snapshot(db) as conn:
        existing = _check_idempotency(conn, account_id, idempotency_key, body_hash, now)
        if existing is None or not _replayable(existing, now):
            _check_account(conn, settings, account_id, now, consent)

    def transaction(conn: sqlite3.Connection) -> Reservation | Replay:
        existing = _check_idempotency(conn, account_id, idempotency_key, body_hash, now)
        if existing is not None and _replayable(existing, now):
            return Replay(existing["result_json"], _usage_in_transaction(conn, settings, account_id, now))
        _check_account(conn, settings, account_id, now, consent)
        entitlement = entitlements.entitlement_in_transaction(conn, settings, account_id, now)
        _check_entitlement_and_limits(conn, settings, entitlement, account_id, now, credits)
        if existing is not None:
            # The finished attempt stays as history under an archived key: its spend, AI calls and
            # any commit ledger row remain attached to its id, and it still counts for rate limits.
            conn.execute(
                "UPDATE usage_reservations SET idempotency_key = ? WHERE id = ?",
                (f"{idempotency_key}{ARCHIVED_KEY_MARK}{existing['id']}", existing["id"]),
            )
        reservation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO usage_reservations (id, account_id, idempotency_key, payload_hash, route, mode, state, "
            "period_id, credits, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)",
            (reservation_id, account_id, idempotency_key, body_hash, route, mode,
             entitlement.period_id, credits, now, now + RESERVATION_TTL_S),
        )
        _bump(conn, account_id, entitlement.period_id, reserved=credits)
        return Reservation(reservation_id, account_id, idempotency_key, credits, entitlement.period_id, "reserved")

    return db.write(transaction)


# --------------------------------------------------------------------------- lifecycle of one reservation


def _load(conn: sqlite3.Connection, reservation_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM usage_reservations WHERE id = ?", (reservation_id,)).fetchone()
    if row is None:
        raise UsageError("not_found", 404, "Unknown request.")
    return row


def activate(db: Database, reservation_id: str, now: int | None = None) -> None:
    """`reserved` -> `active` when dispatched to the engine; raises if the reservation already ended."""
    now = _now(now)

    def transaction(conn: sqlite3.Connection) -> None:
        row = _load(conn, reservation_id)
        if row["state"] == "reserved":
            # From here on the services proxy owns heartbeat_at: only fill it when empty.
            conn.execute(
                "UPDATE usage_reservations SET state = 'active', heartbeat_at = COALESCE(heartbeat_at, ?) WHERE id = ?",
                (now, reservation_id),
            )
        elif row["state"] != "active":
            raise UsageError("reservation_inactive", 409, "This request is no longer running.")

    db.write(transaction)


def mark_exposed(db: Database, reservation_id: str, now: int | None = None) -> None:
    """Record the first token shown to the user (set once; ignored after settlement)."""
    now = _now(now)

    def transaction(conn: sqlite3.Connection) -> None:
        row = _load(conn, reservation_id)
        if row["state"] not in LIVE_STATES:
            log.warning("exposure ignored for reservation %s in state %s", reservation_id, row["state"])
            return
        if row["exposure_at"] is None:
            conn.execute("UPDATE usage_reservations SET exposure_at = ? WHERE id = ?", (now, reservation_id))

    db.write(transaction)


def _commit(conn: sqlite3.Connection, row: sqlite3.Row, now: int, result_json: str | None, state: str = "committed") -> None:
    conn.execute(
        "UPDATE usage_reservations SET state = ?, finished_at = ?, result_json = ?, result_expires_at = ?, "
        "release_reason = NULL WHERE id = ?",
        (state, now, result_json, now + RESULT_TTL_S if result_json is not None else None, row["id"]),
    )
    # The partial unique index makes a second commit row for this reservation impossible.
    conn.execute(
        "INSERT INTO credit_ledger (account_id, period_id, kind, amount, reservation_id, actor, reason, created_at) "
        "VALUES (?, ?, 'commit', ?, ?, 'system', NULL, ?)",
        (row["account_id"], row["period_id"], row["credits"], row["id"], now),
    )
    _bump(conn, row["account_id"], row["period_id"], committed=row["credits"], reserved=-row["credits"])


def _release(conn: sqlite3.Connection, row: sqlite3.Row, now: int, reason: str, state: str = "released") -> None:
    conn.execute(
        "UPDATE usage_reservations SET state = ?, finished_at = ?, release_reason = ? WHERE id = ?",
        (state, now, reason, row["id"]),
    )
    _bump(
        conn, row["account_id"], row["period_id"],
        reserved=-row["credits"], released_with_spend=1 if row["cost_micros"] > 0 else 0,
    )


def finish(
    db: Database,
    settings: Settings,
    reservation_id: str,
    usable: bool,
    result_json: str | None,
    now: int | None = None,
    customer_stopped: bool = False,
) -> dict[str, Any]:
    """Commit or release per CONTRACTS section 7; return the usage block of section 6.2.

    Committed when the result is usable and an AI call succeeded, or when the customer stopped the
    request (`customer_stopped`) after text had been shown; released otherwise, previews included.
    """
    now = _now(now)
    bind_settings(db, settings)

    def transaction(conn: sqlite3.Connection) -> dict[str, Any]:
        row = _load(conn, reservation_id)
        if row["state"] in LIVE_STATES:  # otherwise already finished: change nothing
            # `usage_reservations.ai_calls` counts every attempt, refused and failed ones included, so
            # it cannot tell whether the customer received anything an AI call produced.
            # A streamed call is recorded by the services process just after its last line, so the
            # engine's final can arrive first: a call still unrecorded (`finished_at` empty) that
            # ended in a usable result counts as the success it is about to be recorded as.
            succeeded = conn.execute(
                "SELECT 1 FROM ai_calls WHERE reservation_id = ? AND (status = 'ok' OR finished_at IS NULL) LIMIT 1"
                if usable else
                "SELECT 1 FROM ai_calls WHERE reservation_id = ? AND status = 'ok' LIMIT 1",
                (reservation_id,),
            ).fetchone() is not None
            if (usable and succeeded) or (customer_stopped and row["exposure_at"] is not None):
                _commit(conn, row, now, result_json if usable else None)
            else:
                _release(conn, row, now, "unusable" if not usable else "no_ai")
        return _usage_in_transaction(conn, settings, row["account_id"], now)

    return db.write(transaction)


def release(db: Database, settings: Settings, reservation_id: str, reason: str, now: int | None = None) -> dict[str, Any]:
    """Release for errors, timeouts and cancellation. Commits instead only when the customer ended
    it (`CUSTOMER_ENDED`) after text had been shown; a failure on Starling's side never uses a draft."""
    now = _now(now)
    bind_settings(db, settings)
    reason = (str(reason or "").strip() or "released")[:MAX_REASON_LENGTH]

    def transaction(conn: sqlite3.Connection) -> dict[str, Any]:
        row = _load(conn, reservation_id)
        if row["state"] in LIVE_STATES:
            if row["exposure_at"] is not None and reason in CUSTOMER_ENDED:
                _commit(conn, row, now, None)
            else:
                _release(conn, row, now, reason)
        return _usage_in_transaction(conn, settings, row["account_id"], now)

    return db.write(transaction)


def settled_as(db: Database, reservation_id: str) -> str | None:
    """The reservation's state after `finish` or `release` (`committed`, `released`, ...)."""
    with _snapshot(db) as conn:
        row = conn.execute("SELECT state FROM usage_reservations WHERE id = ?", (reservation_id,)).fetchone()
    return row["state"] if row is not None else None


def replay(db: Database, account_id: str, idempotency_key: str, now: int | None = None) -> Replay | None:
    now = _now(now)
    with _snapshot(db) as conn:
        row = conn.execute(
            "SELECT state, result_json, result_expires_at FROM usage_reservations WHERE account_id = ? AND idempotency_key = ?",
            (account_id, idempotency_key),
        ).fetchone()
        if row is None or not _replayable(row, now):
            return None
        result_json = row["result_json"]
    return Replay(result_json, usage_for_account(db, _settings_for(db), account_id, now))


def _settle_abandoned(conn: sqlite3.Connection, rows: list[sqlite3.Row], now: int, reason: str) -> list[str]:
    """End reservations nobody is going to finish. Released, text shown or not: the request died on
    Starling's side (a crash, a deploy, a request past its wall), so it never uses a draft."""
    settled: list[str] = []
    for row in rows:
        conn.execute("SAVEPOINT usage_sweep_row")
        try:
            _release(conn, row, now, reason, state="expired")
        except sqlite3.Error:
            # One inconsistent row must not stop every later sweep; verify_counters reports it.
            conn.execute("ROLLBACK TO usage_sweep_row")
            conn.execute("RELEASE usage_sweep_row")
            log.exception("could not expire reservation %s", row["id"])
            continue
        conn.execute("RELEASE usage_sweep_row")
        settled.append(row["id"])
    return settled


def settle_orphans(db: Database, settings: Settings, now: int | None = None) -> list[str]:
    """Settle every reservation that is still live when the gateway starts; return their ids.

    A live reservation belongs to a request running inside one gateway process, and there is only
    ever one (CONTRACTS section 2). When that process starts, nothing can be running yet: whatever
    is still `reserved` or `active` was left behind by a deploy, a crash or a kill, and the engines
    that were serving it have been stopped. Without this the rows wait out their five-minute wall,
    and until then their credits count as in use: a customer one draft from the limit is told
    `limit_reached` by a request that died with the old process, and the account page shows drafts
    "in progress" that are not. Same rule as the sweep: released, even if text had reached the
    customer.
    """
    now = _now(now)
    bind_settings(db, settings)

    def transaction(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT * FROM usage_reservations WHERE state IN ('reserved', 'active') ORDER BY created_at"
        ).fetchall()
        return _settle_abandoned(conn, rows, now, "gateway_restart")

    return db.write(transaction)


def sweep_expired(db: Database, settings: Settings, now: int | None = None) -> list[str]:
    """Expire overdue reservations; return reservation ids whose engines must be cancelled."""
    now = _now(now)
    bind_settings(db, settings)

    def transaction(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT * FROM usage_reservations WHERE state IN ('reserved', 'active') AND expires_at <= ? ORDER BY expires_at",
            (now,),
        ).fetchall()
        expired = _settle_abandoned(conn, rows, now, "expired")
        # Stored results (drafts) are kept for 15 minutes only.
        conn.execute(
            "UPDATE usage_reservations SET result_json = NULL "
            "WHERE state = 'committed' AND result_json IS NOT NULL AND result_expires_at <= ?",
            (now,),
        )
        return expired

    return db.write(transaction)


# --------------------------------------------------------------------------- account state


def account_state(db: Database, settings: Settings, account_id: str, now: int | None = None) -> dict[str, Any]:
    """The session JSON of CONTRACTS section 6.2, shared by /api/v1/session and account pages."""
    now = _now(now)
    bind_settings(db, settings)
    with _snapshot(db) as conn:
        known = conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if known is None:
        raise UsageError("not_found", 404, "Unknown account.")
    entitlement = entitlements.current_entitlement(db, settings, account_id, now)
    with _snapshot(db) as conn:
        account = conn.execute("SELECT email, email_verified_at, status FROM accounts WHERE id = ?", (account_id,)).fetchone()
        usage = _usage_from_conn(conn, settings, entitlement, account_id)
        consent = conn.execute(
            "SELECT version, passive_learning FROM consents WHERE account_id = ? AND kind = ? AND revoked_at IS NULL "
            "ORDER BY granted_at DESC, version DESC LIMIT 1",
            (account_id, CONSENT_KIND),
        ).fetchone()
        pause = _service_pause(conn, now, entitlement.plan_code == settings.plans.free.code)
    required = settings.consent_version
    return {
        "account": {
            "id": account_id,
            "email_masked": mask_email(account["email"]),
            "email_verified": account["email_verified_at"] is not None,
            "status": account["status"] if account["status"] in ("active", "suspended") else "deleting",
        },
        "plan": {
            "code": entitlement.plan_code,
            "name": entitlement.plan_name,
            "status": entitlement.status,
            "interval": entitlement.interval,
            "period_start": int(entitlement.period_start),
            "period_end": int(entitlement.period_end),
            "cancel_at": entitlement.cancel_at,
            "scheduled_change": dict(entitlement.scheduled_change) if entitlement.scheduled_change else None,
            "payment_problem": bool(entitlement.payment_problem),
            # Which payment needs the customer, and for which plan (None when none does).
            "payment_issue": dict(entitlement.payment_issue) if entitlement.payment_issue else None,
            # False on the top public plan: nothing to upgrade to, so clients offer support instead.
            "upgradable": _upgradable(settings, entitlement.plan_code),
        },
        "usage": usage,
        "consent": {
            "required_version": required,
            "accepted_version": consent["version"] if consent is not None else None,
            # Passive learning counts only under the current disclosure version.
            "passive_learning": bool(consent is not None and consent["version"] == required and consent["passive_learning"]),
        },
        "service": {
            "status": "paused" if pause else "ok",
            "scope": pause.scope if pause else None,
            "message": pause.message if pause else None,
            "retry_at": now + pause.retry_after_s if pause else None,
        },
        "extension": {"min_version": settings.extension_min_version},
    }


# --------------------------------------------------------------------------- nightly check


def verify_counters(db: Database) -> list[dict[str, Any]]:
    """Mismatches between `usage_counters` and the ledger and reservations; empty when consistent."""
    fields = ("committed", "reserved", "granted")
    expected: dict[tuple[str, str | None], dict[str, int]] = {}
    actual: dict[tuple[str, str | None], dict[str, int]] = {}

    def add(row, field: str) -> None:
        key = (row["account_id"], row["period_id"])
        expected.setdefault(key, dict.fromkeys(fields, 0))[field] += int(row["total"] or 0)

    with _snapshot(db) as conn:
        for row in conn.execute(
            "SELECT account_id, period_id, SUM(amount) AS total FROM credit_ledger WHERE kind = 'commit' "
            "GROUP BY account_id, period_id"
        ):
            add(row, "committed")
        for row in conn.execute(
            "SELECT account_id, period_id, SUM(credits) AS total FROM usage_reservations "
            "WHERE state IN ('reserved', 'active') GROUP BY account_id, period_id"
        ):
            add(row, "reserved")
        for row in conn.execute(
            "SELECT account_id, period_id, SUM(CASE kind WHEN 'admin_grant' THEN amount ELSE -amount END) AS total "
            "FROM credit_ledger WHERE kind IN ('admin_grant', 'admin_debit') GROUP BY account_id, period_id"
        ):
            add(row, "granted")
        for row in conn.execute("SELECT account_id, period_id, committed, reserved, granted FROM usage_counters"):
            actual[(row["account_id"], row["period_id"])] = {f: int(row[f]) for f in fields}
        unledgered = conn.execute(
            "SELECT r.id, r.account_id, r.period_id, r.credits, l.amount FROM usage_reservations r "
            "LEFT JOIN credit_ledger l ON l.kind = 'commit' AND l.reservation_id = r.id "
            "WHERE (r.state = 'committed' OR (r.state = 'expired' AND r.release_reason IS NULL)) "
            "AND (l.id IS NULL OR l.amount != r.credits)"
        ).fetchall()
        stray = conn.execute(
            "SELECT l.reservation_id, l.account_id, l.period_id, l.amount FROM credit_ledger l "
            "JOIN usage_reservations r ON r.id = l.reservation_id "
            "WHERE l.kind = 'commit' AND NOT (r.state = 'committed' OR (r.state = 'expired' AND r.release_reason IS NULL))"
        ).fetchall()

    problems: list[dict[str, Any]] = []
    zero = dict.fromkeys(fields, 0)
    for key in sorted(set(expected) | set(actual), key=lambda k: (k[0], k[1] or "")):
        want, have = expected.get(key, zero), actual.get(key, zero)
        for field in fields:
            if want[field] != have[field]:
                problems.append({"account_id": key[0], "period_id": key[1], "field": field,
                                 "counter": have[field], "expected": want[field]})
    for row in unledgered:
        problems.append({"account_id": row["account_id"], "period_id": row["period_id"], "field": "commit_ledger",
                         "reservation_id": row["id"], "counter": int(row["amount"] or 0), "expected": int(row["credits"])})
    for row in stray:
        problems.append({"account_id": row["account_id"], "period_id": row["period_id"], "field": "commit_ledger",
                         "reservation_id": row["reservation_id"], "counter": int(row["amount"]), "expected": 0})
    return problems
