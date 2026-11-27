"""Owner dashboard figures from the control database (CONTRACTS 7, 8 and 11).

Every figure is an estimate for the owner; Stripe and the AI provider's invoices are the final record.
Queries read metadata, counts, costs and error codes only: never `usage_reservations.result_json`,
`billing_events.payload_json`, prompts, drafts or samples.

- MRR: subscriptions in `active` or `past_due` for the configured Stripe mode, priced from `price_map`
  (what the customer actually pays) and normalized to a month (year / 12, week x 52 / 12,
  day x 365 / 12); an unmapped price falls back to the plans.toml amount. Only prices in the plans'
  currency are added.
- Subscribers per plan (the same subscriptions): `suspended` (open dispute or suspended account), else
  `past_due`, else `active`; `canceling` counts cancel_at_period_end or cancel_at on top of that.
  `paid_accounts` hold such a subscription; `free_accounts` are the other active or suspended accounts.
- Credits: `credit_ledger` rows of kind `commit` since 00:00 UTC, and in the last 7 and 30 days.
- AI: calls from `ai_calls.started_at`; spend from `budget_counters`, the numbers the budgets trip on.
- Failure rate (`db/request_errors.py`), on the metered routes: failed = SUM(count) and
  requests = reservations created in the window + SUM(count) WHERE reserved = 0.
- Service status follows `usage.service`: the owner pause (`ai_paused`, CONTRACTS 11; a malformed value
  counts as paused), then the global day and month budgets. The free-plan trip is reported on its own
  because paid accounts keep drafting.
- Backups (`service_state['backup:last']`) and free space on the data volume (`alerts.disk_status`):
  nothing else watches either, and `/readyz` checks neither.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from fractions import Fraction
from typing import Any, Iterator
from urllib.parse import urlsplit

from ..config import Plan, Settings
from ..db import Database, request_errors
from ..usage import service as usage_service
from . import alerts
from .common import AI_TEST_KEY, MALFORMED, as_int, read_state, text

DAY_S = 86_400
FAILURE_WINDOW_S = DAY_S
FAILURE_WINDOW_LABEL = "last 24 hours"
TOP_CODES = 5
MAX_ALERTS = 20
MAX_FAILURE_ROWS = 200
MAX_EVENTS = 100
AUDIT_PAGE_SIZE = 50
LANES = ("quality", "fast", "vision")
MONTHLY_FACTORS = {"month": Fraction(1), "year": Fraction(1, 12), "week": Fraction(52, 12), "day": Fraction(365, 12)}
WEBHOOK_FILTERS: dict[str, tuple[str, ...]] = {
    "all": (),
    "received": ("received", "processing"),
    "processed": ("processed", "ignored"),
    "failed": ("failed",),
}
DAY_BUDGET_MESSAGE = "The daily AI budget is used up. Drafting resumes at 00:00 UTC."
MONTH_BUDGET_MESSAGE = "The monthly AI budget is used up. Drafting resumes when the next month starts (UTC)."


@contextmanager
def snapshot(db: Database) -> Iterator[sqlite3.Connection]:
    """One consistent read transaction on this thread's reader connection."""
    with db.read() as conn:
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("ROLLBACK")


def day_start(now: int) -> int:
    return now - now % DAY_S


def percent(part: int, whole: int) -> float:
    return min(100.0, round(part * 100 / whole, 2)) if whole > 0 else 0.0


# --------------------------------------------------------------------------- service status and budgets


def ai_pause(conn: sqlite3.Connection) -> dict[str, Any]:
    """The owner pause read the way `usage.service` reads it: {"paused", "message", "since"}."""
    value, since = read_state(conn, usage_service.PAUSE_KEY)
    running = {"paused": False, "message": None, "since": None}
    if since is None:
        return running
    if value is MALFORMED:
        return {"paused": True, "message": usage_service.PAUSED_MESSAGE, "since": since}
    if isinstance(value, dict):
        if not value.get("paused"):
            return running
        message = value.get("message")
        shown = message.strip() if isinstance(message, str) and message.strip() else usage_service.PAUSED_MESSAGE
        return {"paused": True, "message": shown, "since": since}
    if isinstance(value, int) and value == 1:  # JSON true (or 1)
        return {"paused": True, "message": usage_service.PAUSED_MESSAGE, "since": since}
    return running


def _budget_row(conn: sqlite3.Connection, scope: str, window: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT cost_micros, limit_micros, tripped_at FROM budget_counters WHERE scope = ? AND window_key = ?",
        (scope, window),
    ).fetchone()


def _tripped(row: sqlite3.Row | None) -> bool:
    return row is not None and (
        row["tripped_at"] is not None or (row["limit_micros"] > 0 and row["cost_micros"] >= row["limit_micros"])
    )


def ai_budgets(conn: sqlite3.Connection, settings: Settings, now: int) -> dict[str, Any]:
    day_key, month_key = usage_service.budget_window_keys(now)
    day = _budget_row(conn, "global_day", day_key)
    month = _budget_row(conn, "global_month", month_key)
    free = _budget_row(conn, "free_day", day_key)
    return {
        "cost_today_micros": int(day["cost_micros"]) if day is not None else 0,
        "daily_budget_micros": settings.ai_daily_budget_micros,
        "cost_month_micros": int(month["cost_micros"]) if month is not None else 0,
        "monthly_budget_micros": settings.ai_monthly_budget_micros,
        "day_tripped": _tripped(day),
        "month_tripped": _tripped(month),
        "free_tripped": _tripped(free),
    }


def service_status(conn: sqlite3.Connection, settings: Settings, now: int) -> dict[str, Any]:
    pause = ai_pause(conn)
    budgets = ai_budgets(conn, settings, now)
    if pause["paused"]:
        status, message = "paused", pause["message"]
    elif budgets["day_tripped"]:
        status, message = "paused", DAY_BUDGET_MESSAGE
    elif budgets["month_tripped"]:
        status, message = "paused", MONTH_BUDGET_MESSAGE
    else:
        status, message = "ok", None
    return {
        "status": status,
        "message": message,
        "owner_paused": pause["paused"],
        "free_paused": status == "ok" and budgets["free_tripped"],
    }


def current_service_status(db: Database, settings: Settings, now: int) -> dict[str, Any]:
    with snapshot(db) as conn:
        return service_status(conn, settings, now)


# --------------------------------------------------------------------------- subscribers and revenue


def _plans_currency(settings: Settings) -> str:
    paid = settings.plans.paid_plans()
    return (paid[0].currency or "usd") if paid else "usd"


def _monthly_cents(row: sqlite3.Row, plan: Plan | None, currency: str) -> Fraction | None:
    if row["unit_amount"] is not None:
        factor = MONTHLY_FACTORS.get(row["interval"] or "")
        if factor is None or (row["currency"] or "").lower() != currency:
            return None
        return int(row["unit_amount"]) * factor
    if plan is not None and plan.is_paid and plan.amount_cents and plan.currency == currency:
        return Fraction(plan.amount_cents)  # plans.toml prices are monthly
    return None


def _subscriber_row(code: str, name: str) -> dict[str, Any]:
    return {"plan_code": code, "plan_name": name, "active": 0, "past_due": 0, "suspended": 0, "canceling": 0}


def subscriber_metrics(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    livemode = 1 if settings.stripe_mode == "live" else 0
    rows = conn.execute(
        "SELECT s.account_id, s.status, s.plan_code, s.cancel_at_period_end, s.cancel_at, s.disputed_at, "
        "a.status AS account_status, p.unit_amount, p.currency, p.interval "
        "FROM subscriptions s JOIN accounts a ON a.id = s.account_id "
        "LEFT JOIN price_map p ON p.price_id = s.price_id "
        "WHERE s.status IN ('active', 'past_due') AND s.livemode = ?",
        (livemode,),
    ).fetchall()
    currency = _plans_currency(settings)
    known = {plan.code: _subscriber_row(plan.code, plan.name) for plan in settings.plans.paid_plans()}
    other: dict[str, dict[str, Any]] = {}
    mrr = Fraction(0)
    paid_accounts: set[str] = set()
    for row in rows:
        code = row["plan_code"] or ""
        plan = settings.plans.get(code) if code else None
        if code in known:
            entry = known[code]
        else:
            entry = other.setdefault(code, _subscriber_row(code, plan.name if plan is not None else (code or "Unknown plan")))
        if row["disputed_at"] is not None or row["account_status"] == "suspended":
            entry["suspended"] += 1
        elif row["status"] == "past_due":
            entry["past_due"] += 1
        else:
            entry["active"] += 1
        if row["cancel_at_period_end"] or row["cancel_at"] is not None:
            entry["canceling"] += 1
        paid_accounts.add(row["account_id"])
        monthly = _monthly_cents(row, plan, currency)
        if monthly is not None:
            mrr += monthly
    free = conn.execute(
        "SELECT COUNT(*) FROM accounts a WHERE a.status IN ('active', 'suspended') AND NOT EXISTS ("
        "SELECT 1 FROM subscriptions s WHERE s.account_id = a.id AND s.status IN ('active', 'past_due') AND s.livemode = ?)",
        (livemode,),
    ).fetchone()[0]
    return {
        "paid_accounts": len(paid_accounts),
        "free_accounts": int(free),
        "mrr_cents": round(mrr),
        "currency": currency,
        "subscribers": [*known.values(), *(other[code] for code in sorted(other))],
    }


# --------------------------------------------------------------------------- credits and AI


def credit_metrics(conn: sqlite3.Connection, now: int) -> dict[str, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN created_at >= ? THEN amount END), 0) AS today, "
        "COALESCE(SUM(CASE WHEN created_at >= ? THEN amount END), 0) AS last_7d, "
        "COALESCE(SUM(amount), 0) AS last_30d FROM credit_ledger WHERE kind = 'commit' AND created_at >= ?",
        (day_start(now), now - 7 * DAY_S, now - 30 * DAY_S),
    ).fetchone()
    return {"today": int(row["today"]), "last_7d": int(row["last_7d"]), "last_30d": int(row["last_30d"])}


def ai_metrics(conn: sqlite3.Connection, settings: Settings, now: int) -> dict[str, int]:
    budgets = ai_budgets(conn, settings, now)
    calls = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END), 0) AS today, COUNT(*) AS last_30d "
        "FROM ai_calls WHERE started_at >= ?",
        (day_start(now), now - 30 * DAY_S),
    ).fetchone()
    released = conn.execute(
        "SELECT COUNT(*) FROM usage_reservations WHERE (state = 'released' OR (state = 'expired' AND release_reason IS NOT NULL)) "
        "AND cost_micros > 0 AND finished_at >= ?",
        (now - 30 * DAY_S,),
    ).fetchone()[0]
    return {
        "calls_today": int(calls["today"]),
        "cost_today_micros": budgets["cost_today_micros"],
        "daily_budget_micros": budgets["daily_budget_micros"],
        "cost_month_micros": budgets["cost_month_micros"],
        "monthly_budget_micros": budgets["monthly_budget_micros"],
        "calls_30d": int(calls["last_30d"]),
        "released_with_spend_30d": int(released),
    }


# --------------------------------------------------------------------------- failures


def failure_summary(conn: sqlite3.Connection, now: int, window_s: int = FAILURE_WINDOW_S) -> dict[str, Any]:
    since = now - window_s
    routes = tuple(request_errors.METERED_ROUTES)
    marks = ", ".join("?" for _ in routes)
    totals = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS failed, COALESCE(SUM(CASE WHEN reserved = 0 THEN count ELSE 0 END), 0) AS refused "
        f"FROM request_errors WHERE last_at >= ? AND route IN ({marks})",
        (since, *routes),
    ).fetchone()
    reservations = conn.execute("SELECT COUNT(*) FROM usage_reservations WHERE created_at >= ?", (since,)).fetchone()[0]
    codes = conn.execute(
        f"SELECT code, SUM(count) AS total FROM request_errors WHERE last_at >= ? AND route IN ({marks}) "
        "GROUP BY code ORDER BY total DESC, code",
        (since, *routes),
    ).fetchall()
    requests = int(reservations) + int(totals["refused"])
    failed = int(totals["failed"])
    return {
        "window_label": FAILURE_WINDOW_LABEL,
        "requests": requests,
        "failed": failed,
        "rate_pct": percent(failed, requests),
        "codes": [{"code": row["code"], "count": int(row["total"])} for row in codes],
    }


def failures_page(db: Database, now: int) -> dict[str, Any]:
    with snapshot(db) as conn:
        summary = failure_summary(conn, now)
        rows = conn.execute(
            "SELECT last_at, account_id, route, mode, code, http_status, count FROM request_errors "
            "WHERE last_at >= ? ORDER BY last_at DESC, id DESC LIMIT ?",
            (now - FAILURE_WINDOW_S, MAX_FAILURE_ROWS),
        ).fetchall()
    return {
        "window_label": summary["window_label"],
        "totals": {"requests": summary["requests"], "failed": summary["failed"], "rate_pct": summary["rate_pct"]},
        "summary": [{**item, "rate_pct": percent(item["count"], summary["requests"])} for item in summary["codes"]],
        "failures": [
            {
                "at": int(row["last_at"]),
                "account_id": row["account_id"],
                "route": row["route"],
                "mode": row["mode"],
                "code": row["code"],
                "http_status": int(row["http_status"]),
                "count": int(row["count"]),
            }
            for row in rows
        ],
    }


# --------------------------------------------------------------------------- webhooks


def _webhook_counts(conn: sqlite3.Connection, now: int) -> dict[str, int | None]:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN received_at >= ? THEN 1 ELSE 0 END), 0) AS received_24h, "
        "COALESCE(SUM(CASE WHEN status IN ('received', 'processing') THEN 1 ELSE 0 END), 0) AS pending, "
        "COALESCE(SUM(CASE WHEN status IN ('processed', 'ignored') THEN 1 ELSE 0 END), 0) AS processed, "
        "COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed, "
        "MIN(CASE WHEN status IN ('received', 'processing') THEN received_at END) AS oldest_pending_at, "
        "MAX(processed_at) AS last_processed_at FROM billing_events",
        (now - DAY_S,),
    ).fetchone()
    return {key: (int(row[key]) if row[key] is not None else None) for key in row.keys()}


def webhook_summary(conn: sqlite3.Connection, now: int) -> dict[str, int | None]:
    counts = _webhook_counts(conn, now)
    return {name: counts[name] for name in ("received_24h", "pending", "failed", "oldest_pending_at", "last_processed_at")}


def webhooks_page(db: Database, status_filter: str, now: int) -> dict[str, Any]:
    statuses = WEBHOOK_FILTERS.get(status_filter)
    if statuses is None:
        status_filter, statuses = "all", ()
    sql = "SELECT event_id, type, status, attempts, last_error, received_at, processed_at, livemode FROM billing_events"
    params: list[Any] = []
    if statuses:
        sql += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
        params.extend(statuses)
    sql += " ORDER BY received_at DESC, event_id DESC LIMIT ?"
    params.append(MAX_EVENTS)
    with snapshot(db) as conn:
        counts = _webhook_counts(conn, now)
        rows = conn.execute(sql, params).fetchall()
    return {
        "status_filter": status_filter,
        "summary": {
            "received": counts["pending"],
            "processed": counts["processed"],
            "failed": counts["failed"],
            "oldest_pending_at": counts["oldest_pending_at"],
            "last_processed_at": counts["last_processed_at"],
        },
        "events": [
            {
                "event_id": row["event_id"],
                "type": row["type"],
                "status": row["status"],
                "attempts": int(row["attempts"]),
                "last_error": text(row["last_error"], 200) or None,
                "received_at": int(row["received_at"]),
                "processed_at": row["processed_at"],
                "livemode": bool(row["livemode"]),
            }
            for row in rows
        ],
    }


# --------------------------------------------------------------------------- audit log


def params_summary(params_json: str | None) -> str:
    try:
        params = json.loads(params_json or "{}")
    except ValueError:
        return ""
    if not isinstance(params, dict):
        return ""
    parts = []
    for key in sorted(params):
        value = params[key]
        shown = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        parts.append(f"{key}={text(shown, 80)}")
    return text(", ".join(parts), 300)


def audit_page(db: Database, page: int) -> dict[str, Any]:
    with snapshot(db) as conn:
        rows = conn.execute(
            "SELECT a.admin_id, a.action, a.target_account_id, a.params_json, a.ip_prefix, a.created_at, "
            "acc.email AS admin_email FROM admin_audit a LEFT JOIN accounts acc ON acc.id = a.admin_id "
            "ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?",
            (AUDIT_PAGE_SIZE + 1, (page - 1) * AUDIT_PAGE_SIZE),
        ).fetchall()
    entries = [
        {
            "created_at": int(row["created_at"]),
            "admin_email": row["admin_email"] or ("Command line" if row["admin_id"] == "cli" else row["admin_id"]),
            "action": row["action"],
            "target_account_id": row["target_account_id"],
            "params_summary": params_summary(row["params_json"]),
            "ip_prefix": row["ip_prefix"] or "",
        }
        for row in rows[:AUDIT_PAGE_SIZE]
    ]
    return {"page": {"number": page, "has_prev": page > 1, "has_next": len(rows) > AUDIT_PAGE_SIZE}, "entries": entries}


# --------------------------------------------------------------------------- pages


def overview(db: Database, settings: Settings, now: int) -> dict[str, Any]:
    """The `metrics` context of admin/overview.html."""
    with snapshot(db) as conn:
        failures = failure_summary(conn, now)
        return {
            "generated_at": int(now),
            "service": service_status(conn, settings, now),
            "alerts": alerts.collect(conn, settings, now)[:MAX_ALERTS],
            **subscriber_metrics(conn, settings),
            "credits": credit_metrics(conn, now),
            "ai": ai_metrics(conn, settings, now),
            "failures": {
                "window_label": failures["window_label"],
                "requests": failures["requests"],
                "failed": failures["failed"],
                "rate_pct": failures["rate_pct"],
                "top_codes": failures["codes"][:TOP_CODES],
            },
            "webhooks": webhook_summary(conn, now),
            "backup": alerts.backup_status(conn),
            "disk": alerts.disk_status(settings),
        }


def ai_page(db: Database, settings: Settings, status: Any, now: int) -> dict[str, Any]:
    """The `ai` context of admin/ai.html from the services status answer (`services.client`)."""
    ok = isinstance(status, dict) and status.get("ok") is True
    payload: dict[str, Any] = status if ok else {}
    error = status.get("error") if isinstance(status, dict) and isinstance(status.get("error"), dict) else {}
    with snapshot(db) as conn:
        budgets = ai_budgets(conn, settings, now)
        pause = ai_pause(conn)
        service = service_status(conn, settings, now)
        last_test, _ = read_state(conn, AI_TEST_KEY)
        search = _search_view(conn, settings, now)
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    base_url = upstream.get("base_url") if isinstance(upstream.get("base_url"), str) else settings.ai_base_url
    effort = payload.get("reasoning_effort") if isinstance(payload.get("reasoning_effort"), str) else settings.ai_reasoning_effort
    return {
        "base_url_host": _host(base_url),
        "reasoning_effort": effort or "none",
        "services": {"ok": ok, "error_code": None if ok else (text(error.get("code"), 60) or "services_error")},
        "keys": _key_views(payload.get("keys")) if ok else [],
        "models": _model_views(payload.get("models"), settings),
        "budgets": budgets,
        "pause": pause,
        "service": service,
        "last_test": _last_test(last_test),
        "search": search,
    }


def _search_view(conn: sqlite3.Connection, settings: Settings, now: int) -> dict[str, Any]:
    """Web search since UTC midnight (`ai_calls` lane `starling-search`) and whether it can be priced.

    A search costs as much as many ordinary drafts, so the owner sees how many ran and what they cost,
    beside the budgets they count toward. A retried search is one search.
    """
    from ..services.world.search import SEARCH_LANE, TOOL, load_tool_prices, tool_price_for

    day_start = now - now % 86_400
    row = conn.execute(
        "SELECT COUNT(DISTINCT reservation_id) AS searches, COALESCE(SUM(cost_micros), 0) AS cost "
        "FROM ai_calls WHERE lane = ? AND started_at >= ?",
        (SEARCH_LANE, day_start),
    ).fetchone()
    try:
        price = tool_price_for(load_tool_prices(settings.ai_pricing_file), TOOL)
    except Exception:  # noqa: BLE001  an unreadable price file shows as "no price", never a broken page
        price = None
    return {
        "enabled": bool(settings.web_search_enabled),
        "model": text(settings.web_search_model, 100) or "not set",
        "per_account_day": int(settings.web_search_per_account_day),
        "searches_today": int(row["searches"] or 0),
        "cost_today_micros": int(row["cost"] or 0),
        "price_per_1k_micros": price.micros_per_call * 1000 if price is not None else None,
    }


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "not set"
    except ValueError:
        return "not set"


def _key_views(keys: Any) -> list[dict[str, Any]]:
    """Whether each key is set and its last 4 characters, as the services process reports them."""
    keys = keys if isinstance(keys, dict) else {}
    views = []
    for slot in ("primary", "secondary"):
        info = keys.get(slot) if isinstance(keys.get(slot), dict) else {}
        last4 = info.get("last4") if isinstance(info.get("last4"), str) and len(info["last4"]) == 4 else None
        views.append({
            "slot": slot,
            "configured": info.get("configured") is True,
            "masked": f"…{last4}" if last4 else None,
            "in_use": keys.get("active") == slot,
        })
    return views


def _model_views(models: Any, settings: Settings) -> list[dict[str, str]]:
    configured = {"quality": settings.ai_model_quality, "fast": settings.ai_model_fast, "vision": settings.ai_model_vision}
    source = models if isinstance(models, dict) and models else configured
    lanes = [lane for lane in source if isinstance(lane, str)] or list(LANES)
    return [{"lane": text(lane, 40), "model": text(source.get(lane), 100) or "not set"} for lane in lanes]


def _last_test(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or as_int(value.get("at")) is None:
        return None
    return {
        "at": value["at"],
        "ok": value.get("ok") is True,
        "latency_ms": as_int(value.get("latency_ms")) or 0,
        "model": text(value.get("model"), 100),
        "error_code": text(value.get("error_code"), 60) or None,
    }
