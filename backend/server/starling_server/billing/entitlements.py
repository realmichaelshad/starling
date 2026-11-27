"""Entitlement: which plan and allowance an account has right now.

Interface fixed by docs/architecture/CONTRACTS.md sections 7.1 and 14; implemented by the
billing workstream. The usage workstream reads it; it never writes billing tables.

Rules (7.1):
* Free periods are monthly anniversaries of `email_verified_at` (or `created_at` while
  unverified), created lazily in `entitlement_periods` (source `free`).
* A paid period's allowance exists only once `invoice.paid` granted it
  (`granted_by_invoice_id`). The granted row's plan is authoritative: an upgrade shows the
  new plan only after its proration invoice is paid (`apply_upgrade`).
* Between a period rollover and the payment of its invoice (renewal in flight, or
  `past_due` within `PAST_DUE_GRACE_DAYS`) the account keeps paid access with a provisional
  row whose allowance is the unused part of the last paid period, capped at 10% of the
  plan allowance. When the invoice is paid that row becomes the paid period, so usage in
  the gap counts against the new allowance.
* `incomplete`, `incomplete_expired`, `unpaid`, `canceled`, `paused`, unknown prices and
  subscriptions past their cancellation time give the free plan. An open dispute gives
  status `suspended` with the free allowance.
* `payment_problem` says THAT a payment needs the customer; `payment_issue` says WHICH one,
  because the three cases need different words and a different button (6.2):
  `first_payment` (an `incomplete` subscription: never paid, still on Free, finished on the
  invoice's own page), `renewal` (`past_due` inside the grace window: paid access continues)
  and `lapsed` (`past_due` after the grace window: on Free until the payment succeeds).
"""
from __future__ import annotations

import calendar
import sqlite3
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..db import Database
from . import mirror

DAY_S = 86_400
CARRY_OVER_DIVISOR = 10  # carry-over is capped at 10% of the plan allowance
GRANT_REASONS = ("subscription_create", "subscription_cycle", "subscription_update")


@dataclass(frozen=True)
class Entitlement:
    plan_code: str
    plan_name: str
    status: str  # "free" | "active" | "past_due" | "suspended"
    interval: str | None
    period_id: str
    period_start: int
    period_end: int
    allowance: int
    cost_ceiling_micros: int
    cancel_at: int | None
    scheduled_change: dict[str, Any] | None
    payment_problem: bool
    # None, or {"kind": "first_payment" | "renewal" | "lapsed", "plan_code": str | None, "plan_name": str | None}
    payment_issue: dict[str, Any] | None = None


def current_entitlement(db: Database, settings: Settings, account_id: str, now: int | None = None) -> Entitlement:
    """Resolve the account's entitlement, creating the current free period row lazily if needed.

    Must not be called from inside `Database.write` (the writer is not re-entrant); use
    `entitlement_in_transaction(conn, ...)` there instead.
    """
    now = mirror.now_or(now)
    if _on_writer_thread(db):
        raise RuntimeError("current_entitlement() inside Database.write; use entitlement_in_transaction(conn, ...)")
    with db.read() as conn:
        result = _resolve(conn, settings, account_id, now)
    if isinstance(result, Entitlement):
        return result
    return db.write(lambda conn: entitlement_in_transaction(conn, settings, account_id, now))


def entitlement_in_transaction(
    conn: sqlite3.Connection, settings: Settings, account_id: str, now: int | None = None
) -> Entitlement:
    """Same as `current_entitlement`, for code already running inside a write transaction."""
    now = mirror.now_or(now)
    result = _resolve(conn, settings, account_id, now)
    if isinstance(result, Entitlement):
        return result
    return _materialize(conn, settings, account_id, result, now)


def preview_entitlement(db: Database, settings: Settings, account_id: str, now: int | None = None) -> Entitlement:
    """The entitlement as it resolves now, without creating any period row (`period_id` is
    empty while the row does not exist yet)."""
    now = mirror.now_or(now)
    with db.read() as conn:
        result = _resolve(conn, settings, account_id, now)
    return result if isinstance(result, Entitlement) else result.template


# --------------------------------------------------------------------------- calendar


def add_months(ts: int, months: int) -> int:
    base = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    index = base.month - 1 + months
    year, month = base.year + index // 12, index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return int(base.replace(year=year, month=month, day=day).timestamp())


def monthly_period(anchor: int, now: int) -> tuple[int, int]:
    """The monthly anniversary period of `anchor` containing `now` (always computed from the
    anchor, so Jan 31 gives Feb 28 and then Mar 31)."""
    anchor, now = int(anchor), int(now)
    if now < anchor:
        return anchor, add_months(anchor, 1)
    a = datetime.fromtimestamp(anchor, tz=timezone.utc)
    n = datetime.fromtimestamp(now, tz=timezone.utc)
    k = max(0, (n.year - a.year) * 12 + (n.month - a.month))
    while k > 0 and add_months(anchor, k) > now:
        k -= 1
    while add_months(anchor, k + 1) <= now:
        k += 1
    return add_months(anchor, k), add_months(anchor, k + 1)


# --------------------------------------------------------------------------- resolution


@dataclass(frozen=True)
class _RowSpec:
    source: str
    subscription_id: str | None
    plan_code: str
    period_start: int
    period_end: int
    allowance: int
    cost_ceiling_micros: int


@dataclass(frozen=True)
class _Pending:
    spec: _RowSpec
    template: Entitlement


def _resolve(conn: sqlite3.Connection, settings: Settings, account_id: str, now: int) -> Entitlement | _Pending:
    account = conn.execute(
        "SELECT id, allowance_key, email_verified_at, created_at FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None:
        raise LookupError("unknown account")
    subscriptions = mirror.subscription_rows_for_account(conn, account_id)
    suspended = any(row["disputed_at"] is not None for row in subscriptions)
    payment_problem = any(row["status"] in ("past_due", "incomplete") for row in subscriptions)
    payment_issue = _free_payment_issue(settings, subscriptions)
    if not suspended:
        candidates = sorted(
            (row for row in subscriptions if _is_entitling(settings, row, now)),
            key=lambda row: (mirror.plan_rank(settings.plans.get(row["plan_code"])), -(row["current_period_start"] or 0)),
            reverse=True,
        )
        for chosen in candidates:
            # Highest plan first (the older one on a tie), but a subscription no invoice has
            # paid for yet entitles nothing: go on to the next one instead of dropping an
            # account that did pay for a lower plan down to free.
            paid = _paid(conn, settings, account_id, chosen, now)
            if paid is not None:
                return paid
    return _free(
        conn, settings, account, now, status="suspended" if suspended else "free",
        payment_problem=payment_problem, payment_issue=payment_issue,
    )


def _issue(settings: Settings, kind: str, plan_code: str | None) -> dict[str, Any]:
    plan = settings.plans.get(plan_code or "")
    known = plan is not None and plan.is_paid
    return {"kind": kind, "plan_code": plan.code if known else None, "plan_name": plan.name if known else None}


def _free_payment_issue(settings: Settings, subscriptions: list[sqlite3.Row]) -> dict[str, Any] | None:
    """The payment that needs the customer when the account resolves to the free plan.

    A `past_due` subscription that no longer entitles (the grace window is over) comes first:
    that customer had paid access and lost it. Otherwise an `incomplete` one: a first payment
    that was never finished. The newest of each kind speaks for the account.
    """
    newest = sorted(subscriptions, key=lambda row: row["current_period_start"] or 0, reverse=True)
    for status, kind in (("past_due", "lapsed"), ("incomplete", "first_payment")):
        for row in newest:
            if row["status"] == status:
                return _issue(settings, kind, row["plan_code"])
    return None


def _cancel_time(row: sqlite3.Row) -> int | None:
    if row["cancel_at_period_end"] and row["current_period_end"] is not None:
        return int(row["current_period_end"])
    return int(row["cancel_at"]) if row["cancel_at"] is not None else None


def _is_entitling(settings: Settings, row: sqlite3.Row, now: int) -> bool:
    plan = settings.plans.get(row["plan_code"] or "")
    if plan is None or not plan.is_paid:
        return False
    cancel_time = _cancel_time(row)
    if cancel_time is not None and now >= cancel_time:
        return False
    if row["ended_at"] is not None and now >= int(row["ended_at"]):
        return False
    if row["status"] in ("active", "trialing"):
        return True
    if row["status"] == "past_due":
        failed = row["first_payment_failed_at"] if row["first_payment_failed_at"] is not None else row["synced_at"]
        return now < int(failed) + settings.past_due_grace_days * DAY_S
    return False


def _scheduled_change(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> dict[str, Any] | None:
    if not row["schedule_id"] or not row["scheduled_price_id"] or row["current_period_end"] is None:
        return None
    code = mirror.plan_for_price(conn, settings, row["scheduled_price_id"])
    if code is None or code == row["plan_code"]:
        return None
    return {"plan_code": code, "effective_at": int(row["current_period_end"])}


def _from_period(settings: Settings, period: sqlite3.Row, **fields: Any) -> Entitlement:
    plan = settings.plans.get(period["plan_code"]) or settings.plans.free
    return Entitlement(
        plan_code=plan.code,
        plan_name=plan.name,
        interval=plan.interval,
        period_id=period["id"],
        period_start=int(period["period_start"]),
        period_end=int(period["period_end"]),
        allowance=int(period["allowance"]),
        cost_ceiling_micros=int(period["cost_ceiling_micros"]),
        **fields,
    )


def _paid(conn: sqlite3.Connection, settings: Settings, account_id: str, sub: sqlite3.Row, now: int) -> Entitlement | _Pending | None:
    plan = settings.plans.get(sub["plan_code"])
    fields = {
        "status": "past_due" if sub["status"] == "past_due" else "active",
        "cancel_at": _cancel_time(sub),
        "scheduled_change": _scheduled_change(conn, settings, sub),
        "payment_problem": sub["status"] == "past_due",
        "payment_issue": _issue(settings, "renewal", sub["plan_code"]) if sub["status"] == "past_due" else None,
    }
    covering = conn.execute(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'subscription' AND subscription_id = ? "
        "AND period_start <= ? AND period_end > ? ORDER BY (granted_by_invoice_id IS NULL), period_start DESC LIMIT 1",
        (account_id, sub["subscription_id"], now, now),
    ).fetchone()
    if covering is not None:
        return _from_period(settings, covering, **fields)
    last_paid = conn.execute(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'subscription' AND subscription_id = ? "
        "AND granted_by_invoice_id IS NOT NULL AND period_start <= ? ORDER BY period_start DESC LIMIT 1",
        (account_id, sub["subscription_id"], now),
    ).fetchone()
    if last_paid is None:
        return None  # the first period is not paid yet: nothing is granted
    start, end = _bridge_bounds(sub, last_paid, now)
    allowance = min(_unused(conn, last_paid), plan.monthly_credits // CARRY_OVER_DIVISOR)
    ceiling = plan.cost_ceiling_micros * allowance // max(plan.monthly_credits, 1)
    template = Entitlement(
        plan_code=plan.code, plan_name=plan.name, interval=plan.interval, period_id="", period_start=start,
        period_end=end, allowance=allowance, cost_ceiling_micros=ceiling, **fields,
    )
    return _Pending(_RowSpec("subscription", sub["subscription_id"], plan.code, start, end, allowance, ceiling), template)


def _bridge_bounds(sub: sqlite3.Row, last_paid: sqlite3.Row, now: int) -> tuple[int, int]:
    start, end = sub["current_period_start"], sub["current_period_end"]
    if start is not None and end is not None and int(start) <= now < int(end):
        return int(start), int(end)
    # The mirror has not seen the rollover yet: estimate the period from the last known end.
    anchors = [int(last_paid["period_end"])]
    if end is not None and int(end) <= now:
        anchors.append(int(end))
    return monthly_period(max(anchors), now)


def _unused(conn: sqlite3.Connection, period: sqlite3.Row) -> int:
    counters = conn.execute(
        "SELECT committed, granted FROM usage_counters WHERE account_id = ? AND period_id = ?",
        (period["account_id"], period["id"]),
    ).fetchone()
    committed = int(counters["committed"]) if counters else 0
    granted = int(counters["granted"]) if counters else 0
    return max(0, int(period["allowance"]) + granted - committed)


def _free_allowance(conn: sqlite3.Connection, settings: Settings, account: sqlite3.Row) -> tuple[int, int]:
    """The free plan's allowance and cost ceiling for this account.

    An address whose account was deleted gets no second free allowance (CONTRACTS 7.1): deletion leaves
    a `tombstones` row keyed by the HMAC of the allowance key, so signing up again with the same address
    (including its plus tags and, for Gmail, dots) starts at zero until the customer subscribes.
    """
    plan = settings.plans.free
    key = account["allowance_key"]
    if not key:
        return plan.monthly_credits, plan.cost_ceiling_micros
    from ..account.deletion import tombstone_hash  # imported late: account.deletion loads billing lazily

    tombstoned = conn.execute(
        "SELECT 1 FROM tombstones WHERE account_hash = ?", (tombstone_hash(settings, key),)
    ).fetchone()
    return (0, 0) if tombstoned is not None else (plan.monthly_credits, plan.cost_ceiling_micros)


def _free(
    conn: sqlite3.Connection, settings: Settings, account: sqlite3.Row, now: int, *, status: str,
    payment_problem: bool, payment_issue: dict[str, Any] | None = None,
) -> Entitlement | _Pending:
    plan = settings.plans.free
    anchor = account["email_verified_at"] if account["email_verified_at"] is not None else account["created_at"]
    start, end = monthly_period(int(anchor), now)
    allowance, ceiling = _free_allowance(conn, settings, account)
    template = Entitlement(
        plan_code=plan.code, plan_name=plan.name, status=status, interval=None, period_id="", period_start=start,
        period_end=end, allowance=allowance, cost_ceiling_micros=ceiling,
        cancel_at=None, scheduled_change=None, payment_problem=payment_problem, payment_issue=payment_issue,
    )
    row = conn.execute(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'free' AND period_start = ?",
        (account["id"], start),
    ).fetchone()
    if row is not None:
        return _with_free_row(template, row)
    return _Pending(_RowSpec("free", None, plan.code, start, end, allowance, ceiling), template)


def _with_free_row(template: Entitlement, row: sqlite3.Row) -> Entitlement:
    return replace(
        template,
        period_id=row["id"],
        period_end=int(row["period_end"]),
        allowance=int(row["allowance"]),
        cost_ceiling_micros=int(row["cost_ceiling_micros"]),
    )


def _materialize(conn: sqlite3.Connection, settings: Settings, account_id: str, pending: _Pending, now: int) -> Entitlement:
    spec = pending.spec
    start = spec.period_start
    for _ in range(60):
        conn.execute(
            "INSERT INTO entitlement_periods (id, account_id, source, subscription_id, plan_code, period_start, "
            "period_end, allowance, cost_ceiling_micros, granted_by_invoice_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) ON CONFLICT DO NOTHING",
            (str(uuid.uuid4()), account_id, spec.source, spec.subscription_id, spec.plan_code, start,
             spec.period_end, spec.allowance, spec.cost_ceiling_micros, int(now)),
        )
        row = conn.execute(
            "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = ? AND period_start = ?",
            (account_id, spec.source, start),
        ).fetchone()
        if spec.source == "free" or row["subscription_id"] in (spec.subscription_id, None):
            break
        start -= 1  # another subscription of this account starts in the same second: begin just before it
    if spec.source == "free":
        return _with_free_row(pending.template, row)
    t = pending.template
    return _from_period(
        settings, row, status=t.status, cancel_at=t.cancel_at, scheduled_change=t.scheduled_change,
        payment_problem=t.payment_problem, payment_issue=t.payment_issue,
    )


def _free_start(conn: sqlite3.Connection, account_id: str, start: int) -> int:
    """The latest second at or before `start` at which no subscription period of the account begins."""
    while conn.execute(
        "SELECT 1 FROM entitlement_periods WHERE account_id = ? AND source = 'subscription' AND period_start = ?",
        (account_id, start),
    ).fetchone():
        start -= 1
    return start


def _on_writer_thread(db: Database) -> bool:
    thread = getattr(getattr(db, "writer", None), "_thread", None)
    return thread is not None and thread is threading.current_thread()


# --------------------------------------------------------------------------- grants (billing writes)


def grant_paid_period(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    account_id: str,
    subscription_id: str,
    plan_code: str,
    period_start: int,
    period_end: int,
    invoice_id: str,
    now: int,
) -> str:
    """Grant a paid period's allowance once per invoice. Returns "granted" or "already"."""
    if conn.execute("SELECT 1 FROM entitlement_periods WHERE granted_by_invoice_id = ?", (invoice_id,)).fetchone():
        return "already"
    plan = settings.plans.get(plan_code)
    if plan is None or not plan.is_paid:
        raise ValueError(f"not a paid plan: {plan_code!r}")
    existing = conn.execute(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'subscription' AND period_start = ?",
        (account_id, int(period_start)),
    ).fetchone()
    if existing is not None and existing["subscription_id"] in (subscription_id, None):
        if existing["granted_by_invoice_id"] is not None:
            return "already"
        # The provisional row of the renewal gap becomes the paid period (its usage carries on).
        conn.execute(
            "UPDATE entitlement_periods SET subscription_id = ?, plan_code = ?, period_end = ?, allowance = ?, "
            "cost_ceiling_micros = ?, granted_by_invoice_id = ? WHERE id = ?",
            (subscription_id, plan.code, int(period_end), plan.monthly_credits, plan.cost_ceiling_micros, invoice_id, existing["id"]),
        )
        return "granted"
    if existing is not None:
        # Another subscription of this account starts in the same second (duplicate or
        # re-subscription): keep both periods by starting this one a second earlier, so it
        # still covers the moment it was paid.
        period_start = _free_start(conn, account_id, int(period_start) - 1)
    conn.execute(
        "INSERT INTO entitlement_periods (id, account_id, source, subscription_id, plan_code, period_start, "
        "period_end, allowance, cost_ceiling_micros, granted_by_invoice_id, created_at) "
        "VALUES (?, ?, 'subscription', ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), account_id, subscription_id, plan.code, int(period_start), int(period_end),
         plan.monthly_credits, plan.cost_ceiling_micros, invoice_id, int(now)),
    )
    # A provisional row that started earlier ends where the paid period starts.
    conn.execute(
        "UPDATE entitlement_periods SET period_end = ? WHERE account_id = ? AND source = 'subscription' "
        "AND subscription_id = ? AND granted_by_invoice_id IS NULL AND period_start < ? AND period_end > ?",
        (int(period_start), account_id, subscription_id, int(period_start), int(period_start)),
    )
    return "granted"


def apply_upgrade(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    account_id: str,
    subscription_id: str,
    new_plan_code: str,
    effective_at: int,
) -> str:
    """After a paid proration invoice: `allowance += (new - old) * remaining / period`.

    Idempotent by plan rank: a period already on the new (or a higher) plan is left alone.
    Returns "uplifted", "already" or "no_period" (the period itself is not granted yet).
    """
    new = settings.plans.get(new_plan_code)
    if new is None or not new.is_paid:
        raise ValueError(f"not a paid plan: {new_plan_code!r}")
    period = conn.execute(
        "SELECT * FROM entitlement_periods WHERE account_id = ? AND source = 'subscription' AND subscription_id = ? "
        "AND granted_by_invoice_id IS NOT NULL AND period_start <= ? AND period_end > ? "
        "ORDER BY period_start DESC LIMIT 1",
        (account_id, subscription_id, int(effective_at), int(effective_at)),
    ).fetchone()
    if period is None:
        return "no_period"
    old = settings.plans.get(period["plan_code"])
    if old is not None and mirror.plan_rank(new) <= mirror.plan_rank(old):
        return "already"
    old_credits = old.monthly_credits if old else 0
    old_ceiling = old.cost_ceiling_micros if old else 0
    length = max(1, int(period["period_end"]) - int(period["period_start"]))
    remaining = min(length, max(0, int(period["period_end"]) - int(effective_at)))
    uplift = max(0, (new.monthly_credits - old_credits) * remaining // length)
    ceiling_uplift = max(0, (new.cost_ceiling_micros - old_ceiling) * remaining // length)
    conn.execute(
        "UPDATE entitlement_periods SET plan_code = ?, allowance = allowance + ?, "
        "cost_ceiling_micros = cost_ceiling_micros + ? WHERE id = ?",
        (new.code, uplift, ceiling_uplift, period["id"]),
    )
    return "uplifted"


def end_subscription_periods(conn: sqlite3.Connection, subscription_id: str, now: int) -> None:
    """End the subscription's current period now (full refund)."""
    conn.execute(
        "UPDATE entitlement_periods SET period_end = ? WHERE source = 'subscription' AND subscription_id = ? "
        "AND period_start <= ? AND period_end > ?",
        (int(now), subscription_id, int(now), int(now)),
    )
