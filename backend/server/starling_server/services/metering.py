"""Admission checks and spend recording for every upstream AI attempt.

CONTRACTS 4.1 (metering), 7 (usage accounting) and 11 (tables). All writes go through the services
process's own database writer thread. The meaning of shared rows follows the usage and billing
workstreams: `usage_counters.cost_micros` is the account's total AI cost for the period (background
included; `background_micros` is the background part) and is incremented only here, while
`reserved`, `committed`, `granted` and `released_with_spend` belong to `usage.service`. A budget
window (`window_key` UTC `YYYY-MM-DD` for `global_day` and `free_day`, `YYYY-MM` for `global_month`)
is tripped when `tripped_at` is set or `cost_micros >= limit_micros` (CONTRACTS 11).

Checks before each attempt, in one writer transaction:

1. the account behind the token exists and is active (else 401 `unauthorized`);
2. owner pause (`service_state` key `ai_paused`) -> 503 `budget_exhausted` "paused by owner";
3. press calls: the reservation belongs to this account, is `active` and not past its hard wall
   (else 409 `reservation_inactive`), and fewer than `AI_MAX_CALLS_PER_REQUEST` attempts are
   recorded for it (else 429 `call_cap_exceeded`);
4. the account's AI cost for the period is below the period's `cost_ceiling_micros` (else 402
   `fair_use_exceeded`); background calls must also be below 10% of that ceiling. Press calls use the
   reservation's period; background calls resolve the current period through
   `billing.entitlements.entitlement_in_transaction` (which creates the free period row if missing);
5. global day and month budgets, and the free-plan trip once the day's PRESS spend (every
   account's; background work excluded) reaches `AI_FREE_TRIP_PCT` of the daily budget (503
   `budget_exhausted` with `Retry-After`).

BACKGROUND WORK NEVER TRIPS THE FREE PLAN (product review U5, 2026-09-22). A launch day of Free
activations starts one voice card per account; counted in the free trip, that background spend
alone would pause every Free account's presses. Background spend still counts in the global day
and month budgets (it is real money) and in the account's own counters; it is kept out of the
`free_day` window, and the day's background total (`service_state` key `background_spend_day`,
`{"day": "YYYY-MM-DD", "cost_micros": n}`) is subtracted from the day's spend the trip compares.

An admitted attempt inserts its `ai_calls` row at once (status `cancelled`, `finished_at` NULL, meaning
"in flight"), so concurrent attempts for one request count against the cap exactly. When the attempt
ends the row is completed, and its cost is added to `usage_counters`, `budget_counters` and the
reservation in the same transaction. A row left with `finished_at` NULL means the process stopped
during the call. Budget checks compare recorded spend; attempts already in flight can overshoot a
budget by at most their own cost.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..billing import entitlements
from ..config import Settings
from ..db import Database
from .errors import ServiceError

log = logging.getLogger(__name__)

BUCKET_PRESS = "press"
BUCKET_BACKGROUND = "background"
BUCKET_SYSTEM = "system"
BUCKET_ADMIN = "admin"
CUSTOMER_BUCKETS = frozenset({BUCKET_PRESS, BUCKET_BACKGROUND})

BACKGROUND_SHARE_PCT = 10
PAUSE_KEY = "ai_paused"
PAUSE_MESSAGE = "paused by owner"
FREE_PAUSED_MESSAGE = "drafting is paused for free accounts until the daily AI budget resets"
PAUSE_RETRY_AFTER_S = 300
ALERT_PREFIX = "alert:"
BACKGROUND_DAY_KEY = "background_spend_day"


@dataclass(frozen=True)
class CallContext:
    bucket: str
    lane: str
    model: str
    account_id: str | None = None
    reservation_id: str | None = None


@dataclass(frozen=True)
class Admission:
    ctx: CallContext
    call_id: int
    period_id: str | None
    plan_code: str | None
    started_at: int


@dataclass(frozen=True)
class AttemptRecord:
    status: str  # ok | error | cancelled
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False
    error_code: str | None = None
    # A provider tool's per-call fee on top of the tokens (the web search tool, W4): priced by the
    # caller from the `tools` rows of ai_pricing.json and added to the attempt's cost everywhere.
    tool_cost_micros: int = 0


@dataclass(frozen=True)
class OwnerPause:
    retry_after_s: int


@dataclass(frozen=True)
class _Refusal:
    http_status: int
    code: str
    message: str
    retry_after_s: int | None = None


@dataclass(frozen=True)
class _BudgetRow:
    cost_micros: int
    limit_micros: int
    tripped_at: int | None

    @property
    def tripped(self) -> bool:
        return self.tripped_at is not None or self.cost_micros >= self.limit_micros


def day_key(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))


def month_key(ts: int) -> str:
    return time.strftime("%Y-%m", time.gmtime(int(ts)))


def seconds_until_next_day(ts: int) -> int:
    return 86_400 - (int(ts) % 86_400)


def background_day_micros(conn: sqlite3.Connection, day: str) -> int:
    """Every account's background spend on UTC `day` (kept out of the free trip)."""
    row = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (BACKGROUND_DAY_KEY,)).fetchone()
    if row is None:
        return 0
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError):
        return 0
    if not isinstance(value, dict) or value.get("day") != day:
        return 0
    cost = value.get("cost_micros")
    return int(cost) if isinstance(cost, int) and not isinstance(cost, bool) and cost > 0 else 0


def _add_background_day(conn: sqlite3.Connection, day: str, cost: int, now: int) -> None:
    total = background_day_micros(conn, day) + max(0, int(cost))
    conn.execute(
        "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
        (BACKGROUND_DAY_KEY, json.dumps({"day": day, "cost_micros": total}, sort_keys=True), now),
    )


def press_day_micros(conn: sqlite3.Connection, day: str, day_cost: int) -> int:
    """The day's spend the free trip compares: all of it but the background work."""
    return max(0, int(day_cost) - background_day_micros(conn, day))


def free_trip_limit(settings: Settings) -> int:
    """Daily spend (all accounts) at which free-plan accounts are paused: AI_FREE_TRIP_PCT of the day's budget."""
    return settings.ai_daily_budget_micros * settings.ai_free_trip_pct // 100


def clear_stale_trips(conn: sqlite3.Connection, settings: Settings, now: int) -> int:
    """Un-trip today's and this month's windows whose spend is below the limits configured now.

    A tripped window stays tripped for the rest of its window, so a budget raised in the configuration
    would otherwise only take effect at the next UTC day or month. The services process calls this at
    startup (changing the configuration restarts it). Returns the number of windows cleared.
    """
    day, month = day_key(now), month_key(now)
    cleared = 0
    for scope, window, limit in (
        ("global_day", day, settings.ai_daily_budget_micros),
        ("global_month", month, settings.ai_monthly_budget_micros),
    ):
        cleared += conn.execute(
            "UPDATE budget_counters SET tripped_at = NULL, limit_micros = ? "
            "WHERE scope = ? AND window_key = ? AND tripped_at IS NOT NULL AND cost_micros < ?",
            (int(limit), scope, window, int(limit)),
        ).rowcount
    free_limit = free_trip_limit(settings)
    row = conn.execute(
        "SELECT cost_micros FROM budget_counters WHERE scope = 'global_day' AND window_key = ?", (day,)
    ).fetchone()
    if press_day_micros(conn, day, int(row[0]) if row else 0) < free_limit:
        cleared += conn.execute(
            "UPDATE budget_counters SET tripped_at = NULL, limit_micros = ? "
            "WHERE scope = 'free_day' AND window_key = ? AND tripped_at IS NOT NULL",
            (free_limit, day),
        ).rowcount
    return cleared


def seconds_until_next_month(ts: int) -> int:
    moment = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return max(1, int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp()) - int(ts))


def read_pause(conn: sqlite3.Connection) -> OwnerPause | None:
    """`service_state['ai_paused']`: JSON `true`, or `{"paused": true, "retry_after_s"?: int, ...}`.

    Same reading as `usage.service`: a value that is not valid JSON counts as paused.
    """
    row = conn.execute("SELECT value_json FROM service_state WHERE key = ?", (PAUSE_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError):
        log.warning("service_state %s is not valid JSON; AI is treated as paused", PAUSE_KEY)
        return OwnerPause(PAUSE_RETRY_AFTER_S)
    if isinstance(value, dict):
        if not value.get("paused"):
            return None
        retry = value.get("retry_after_s")
        valid = isinstance(retry, int) and not isinstance(retry, bool) and retry > 0
        return OwnerPause(retry if valid else PAUSE_RETRY_AFTER_S)
    if isinstance(value, int) and value == 1:  # JSON true (or 1)
        return OwnerPause(PAUSE_RETRY_AFTER_S)
    return None


def _budget(conn: sqlite3.Connection, scope: str, window: str, limit: int) -> _BudgetRow:
    conn.execute(
        "INSERT OR IGNORE INTO budget_counters (scope, window_key, cost_micros, limit_micros) VALUES (?, ?, 0, ?)",
        (scope, window, int(limit)),
    )
    conn.execute(
        "UPDATE budget_counters SET limit_micros = ? WHERE scope = ? AND window_key = ? AND limit_micros != ?",
        (int(limit), scope, window, int(limit)),
    )
    row = conn.execute(
        "SELECT cost_micros, limit_micros, tripped_at FROM budget_counters WHERE scope = ? AND window_key = ?",
        (scope, window),
    ).fetchone()
    return _BudgetRow(int(row["cost_micros"]), int(row["limit_micros"]), row["tripped_at"])


def _trip(conn: sqlite3.Connection, scope: str, window: str, now: int) -> None:
    conn.execute(
        "UPDATE budget_counters SET tripped_at = ? WHERE scope = ? AND window_key = ? AND tripped_at IS NULL",
        (now, scope, window),
    )


def _log_write_failure(future: Future) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        log.error("could not record an AI attempt (%s)", type(exc).__name__)


class Metering:
    def __init__(self, db: Database, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._settings = settings
        self._clock = clock

    @property
    def free_trip_micros(self) -> int:
        return free_trip_limit(self._settings)

    # ------------------------------------------------------------------ admission

    async def admit(self, ctx: CallContext) -> Admission:
        now = int(self._clock())
        outcome = await self._db.write_async(lambda conn: self._admit(conn, ctx, now))
        if isinstance(outcome, _Refusal):
            log.info("ai call refused bucket=%s lane=%s code=%s", ctx.bucket, ctx.lane, outcome.code)
            raise ServiceError(outcome.http_status, outcome.code, outcome.message, outcome.retry_after_s)
        return outcome

    def _admit(self, conn: sqlite3.Connection, ctx: CallContext, now: int) -> Admission | _Refusal:
        settings = self._settings
        customer = ctx.bucket in CUSTOMER_BUCKETS
        if customer:
            account = conn.execute("SELECT status FROM accounts WHERE id = ?", (ctx.account_id,)).fetchone()
            if account is None or account["status"] != "active":
                return _Refusal(401, "unauthorized", "unknown services token")
        if ctx.bucket != BUCKET_ADMIN:
            pause = read_pause(conn)
            if pause is not None:
                return _Refusal(503, "budget_exhausted", PAUSE_MESSAGE, pause.retry_after_s)

        period: sqlite3.Row | None = None
        if ctx.bucket == BUCKET_PRESS:
            reservation = conn.execute(
                "SELECT state, period_id, expires_at FROM usage_reservations WHERE id = ? AND account_id = ?",
                (ctx.reservation_id, ctx.account_id),
            ).fetchone()
            if reservation is None or reservation["state"] != "active" or int(reservation["expires_at"]) <= now:
                return _Refusal(409, "reservation_inactive", "the request is not active")
            attempts = conn.execute(
                "SELECT COUNT(*) FROM ai_calls WHERE reservation_id = ? AND account_id = ?",
                (ctx.reservation_id, ctx.account_id),
            ).fetchone()[0]
            if int(attempts) >= settings.ai_max_calls_per_request:
                return _Refusal(429, "call_cap_exceeded", "this request has made the maximum number of AI calls")
            if reservation["period_id"]:
                period = conn.execute(
                    "SELECT id, plan_code, cost_ceiling_micros FROM entitlement_periods WHERE id = ? AND account_id = ?",
                    (reservation["period_id"], ctx.account_id),
                ).fetchone()

        period_id: str | None = None
        plan_code: str | None = None
        if customer:
            if period is not None:
                period_id, plan_code, ceiling = period["id"], period["plan_code"], int(period["cost_ceiling_micros"])
            else:
                try:
                    entitlement = entitlements.entitlement_in_transaction(conn, settings, str(ctx.account_id), now)
                except LookupError:
                    return _Refusal(401, "unauthorized", "unknown services token")
                period_id, plan_code = entitlement.period_id, entitlement.plan_code
                ceiling = int(entitlement.cost_ceiling_micros)
            counters = conn.execute(
                "SELECT cost_micros, background_micros FROM usage_counters WHERE account_id = ? AND period_id = ?",
                (ctx.account_id, period_id),
            ).fetchone()
            spent = int(counters["cost_micros"]) if counters else 0
            background = int(counters["background_micros"]) if counters else 0
            if spent >= ceiling:
                return _Refusal(402, "fair_use_exceeded", "this account's AI cost ceiling for the period is reached")
            if ctx.bucket == BUCKET_BACKGROUND and background >= ceiling * BACKGROUND_SHARE_PCT // 100:
                return _Refusal(
                    402, "fair_use_exceeded", "this account's background AI allowance for the period is used up"
                )

        if ctx.bucket != BUCKET_ADMIN:
            free_plan = customer and plan_code == settings.plans.free.code
            refusal = self._check_budgets(conn, now, free_plan=free_plan)
            if refusal is not None:
                return refusal

        cursor = conn.execute(
            "INSERT INTO ai_calls (account_id, reservation_id, bucket, lane, model, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'cancelled', ?)",
            (
                ctx.account_id,
                ctx.reservation_id if ctx.bucket == BUCKET_PRESS else None,
                ctx.bucket,
                ctx.lane,
                ctx.model,
                now,
            ),
        )
        return Admission(ctx=ctx, call_id=int(cursor.lastrowid), period_id=period_id, plan_code=plan_code, started_at=now)

    def _check_budgets(self, conn: sqlite3.Connection, now: int, free_plan: bool) -> _Refusal | None:
        settings = self._settings
        day, month = day_key(now), month_key(now)
        day_row = _budget(conn, "global_day", day, settings.ai_daily_budget_micros)
        if day_row.tripped:
            _trip(conn, "global_day", day, now)
            return _Refusal(503, "budget_exhausted", "the daily AI budget is used up", seconds_until_next_day(now))
        month_row = _budget(conn, "global_month", month, settings.ai_monthly_budget_micros)
        if month_row.tripped:
            _trip(conn, "global_month", month, now)
            return _Refusal(503, "budget_exhausted", "the monthly AI budget is used up", seconds_until_next_month(now))
        if free_plan:
            free_row = _budget(conn, "free_day", day, self.free_trip_micros)
            if free_row.tripped or press_day_micros(conn, day, day_row.cost_micros) >= free_row.limit_micros:
                _trip(conn, "free_day", day, now)
                return _Refusal(
                    503,
                    "budget_exhausted",
                    FREE_PAUSED_MESSAGE,
                    seconds_until_next_day(now),
                )
        return None

    # ------------------------------------------------------------------ recording

    def cost_micros(self, model: str, started_at: int, record: AttemptRecord) -> int:
        when = datetime.fromtimestamp(int(started_at), tz=timezone.utc).date()
        price = self._settings.ai_pricing.price_for(model, when)
        if price is None:
            return 0
        input_tokens = max(0, int(record.input_tokens))
        cached = max(0, min(int(record.cached_input_tokens), input_tokens))
        return price.cost_micros(input_tokens, cached, max(0, int(record.output_tokens)))

    def has_price(self, model: str) -> bool:
        when = datetime.fromtimestamp(int(self._clock()), tz=timezone.utc).date()
        return self._settings.ai_pricing.price_for(model, when) is not None

    async def record(self, admission: Admission, record: AttemptRecord) -> int:
        """Complete the attempt's `ai_calls` row and add its cost everywhere; returns the cost."""
        finished_at = int(self._clock())
        return await self._db.write_async(lambda conn: self._record(conn, admission, record, finished_at))

    def record_nowait(self, admission: Admission, record: AttemptRecord) -> None:
        """Queue the recording without awaiting (safe inside cancellation handlers)."""
        finished_at = int(self._clock())
        try:
            future = self._db.writer.submit(lambda conn: self._record(conn, admission, record, finished_at))
        except RuntimeError:
            log.error("could not record an AI attempt: the database writer is closed")
            return
        future.add_done_callback(_log_write_failure)

    def _record(self, conn: sqlite3.Connection, admission: Admission, record: AttemptRecord, finished_at: int) -> int:
        ctx = admission.ctx
        cost = self.cost_micros(ctx.model, admission.started_at, record) + max(0, int(record.tool_cost_micros))
        cursor = conn.execute(
            "UPDATE ai_calls SET input_tokens = ?, cached_input_tokens = ?, output_tokens = ?, estimated = ?, "
            "cost_micros = ?, status = ?, error_code = ?, finished_at = ? WHERE id = ? AND finished_at IS NULL",
            (
                max(0, int(record.input_tokens)),
                max(0, int(record.cached_input_tokens)),
                max(0, int(record.output_tokens)),
                1 if record.estimated else 0,
                cost,
                record.status,
                record.error_code,
                finished_at,
                admission.call_id,
            ),
        )
        if cursor.rowcount == 0:  # already recorded: never count spend twice
            return 0
        if ctx.bucket in CUSTOMER_BUCKETS and ctx.account_id and admission.period_id:
            conn.execute(
                "INSERT OR IGNORE INTO usage_counters (account_id, period_id) VALUES (?, ?)",
                (ctx.account_id, admission.period_id),
            )
            conn.execute(
                "UPDATE usage_counters SET cost_micros = cost_micros + ?, background_micros = background_micros + ? "
                "WHERE account_id = ? AND period_id = ?",
                (cost, cost if ctx.bucket == BUCKET_BACKGROUND else 0, ctx.account_id, admission.period_id),
            )
        if ctx.bucket == BUCKET_PRESS:
            conn.execute(
                "UPDATE usage_reservations SET cost_micros = cost_micros + ?, ai_calls = ai_calls + 1, heartbeat_at = ? "
                "WHERE id = ? AND account_id = ?",
                (cost, finished_at, ctx.reservation_id, ctx.account_id),
            )
        self._add_spend(conn, admission, cost, finished_at)
        return cost

    def _add_spend(self, conn: sqlite3.Connection, admission: Admission, cost: int, now: int) -> None:
        settings = self._settings
        day, month = day_key(admission.started_at), month_key(admission.started_at)
        scopes = [
            ("global_day", day, settings.ai_daily_budget_micros),
            ("global_month", month, settings.ai_monthly_budget_micros),
        ]
        # Only a Free account's PRESS spend is Free spend for the trip (U5): its background work is not.
        if admission.ctx.bucket == BUCKET_PRESS and admission.plan_code == settings.plans.free.code:
            scopes.append(("free_day", day, self.free_trip_micros))
        if admission.ctx.bucket == BUCKET_BACKGROUND and cost > 0:
            _add_background_day(conn, day, cost, now)
        for scope, window, limit in scopes:
            conn.execute(
                "INSERT OR IGNORE INTO budget_counters (scope, window_key, cost_micros, limit_micros) VALUES (?, ?, 0, ?)",
                (scope, window, int(limit)),
            )
            conn.execute(
                "UPDATE budget_counters SET cost_micros = cost_micros + ?, limit_micros = ?, "
                "tripped_at = CASE WHEN tripped_at IS NULL AND cost_micros + ? >= ? THEN ? ELSE tripped_at END "
                "WHERE scope = ? AND window_key = ?",
                (cost, int(limit), cost, int(limit), now, scope, window),
            )
        # Free-plan accounts trip once the day's press spend reaches the free share of the daily budget.
        free_limit = self.free_trip_micros
        day_cost = conn.execute(
            "SELECT cost_micros FROM budget_counters WHERE scope = 'global_day' AND window_key = ?", (day,)
        ).fetchone()[0]
        if press_day_micros(conn, day, int(day_cost)) >= free_limit:
            conn.execute(
                "INSERT OR IGNORE INTO budget_counters (scope, window_key, cost_micros, limit_micros) "
                "VALUES ('free_day', ?, 0, ?)",
                (day, free_limit),
            )
            _trip(conn, "free_day", day, now)

    # ------------------------------------------------------------------ owner alerts and status

    async def record_alert(self, name: str, details: dict[str, Any]) -> None:
        now = int(self._clock())
        value = json.dumps({"at": now, **details}, sort_keys=True)

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (ALERT_PREFIX + name, value, now),
            )

        await self._db.write_async(write)

    async def snapshot(self) -> dict[str, Any]:
        settings = self._settings
        now = int(self._clock())
        day, month = day_key(now), month_key(now)
        windows = {
            "global_day": (day, settings.ai_daily_budget_micros),
            "global_month": (month, settings.ai_monthly_budget_micros),
            "free_day": (day, self.free_trip_micros),
        }

        def read(conn: sqlite3.Connection) -> dict[str, Any]:
            budgets: dict[str, Any] = {}
            for scope, (window, limit) in windows.items():
                row = conn.execute(
                    "SELECT cost_micros, tripped_at FROM budget_counters WHERE scope = ? AND window_key = ?",
                    (scope, window),
                ).fetchone()
                budgets[scope] = {
                    "window": window,
                    "cost_micros": int(row["cost_micros"]) if row else 0,
                    "limit_micros": int(limit),
                    "tripped_at": row["tripped_at"] if row else None,
                }
            calls: dict[str, Any] = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(cost_micros), 0) AS cost FROM ai_calls "
                "WHERE started_at >= ? AND finished_at IS NOT NULL GROUP BY status",
                (now - now % 86_400,),
            ):
                calls[row["status"]] = {"count": int(row["n"]), "cost_micros": int(row["cost"])}
            in_flight = conn.execute(
                "SELECT COUNT(*) FROM ai_calls WHERE finished_at IS NULL AND started_at >= ?", (now - 600,)
            ).fetchone()[0]
            alerts = []
            for row in conn.execute(
                "SELECT key, value_json, updated_at FROM service_state WHERE substr(key, 1, 9) = 'alert:ai_' "
                "ORDER BY updated_at DESC"
            ):
                try:
                    value = json.loads(row["value_json"])
                except (TypeError, ValueError):
                    value = None
                alerts.append({"key": row["key"], "value": value, "updated_at": row["updated_at"]})
            return {
                "paused": read_pause(conn) is not None,
                "budgets": budgets,
                "calls_today": calls,
                "in_flight": int(in_flight),
                "alerts": alerts,
            }

        return await self._db.write_async(read)
