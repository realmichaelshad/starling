"""Small helpers shared by the admin modules: `service_state` JSON rows and value cleaning.

`service_state` keys the dashboard itself writes:

- `admin:ai_last_test`: the last `POST /admin/ai/test` summary ({"at", "ok", "latency_ms", "model",
  "error_code", "models"}); codes, models and latency only, never provider messages.
- `admin:alert_notifications`: {alert key: {"sent_at", "fired_at"}} for the owner alert emails.
- `admin:request_errors_pruned_at`: unix seconds of the last `request_errors` prune.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

AI_TEST_KEY = "admin:ai_last_test"
ALERT_NOTIFICATIONS_KEY = "admin:alert_notifications"
PRUNED_AT_KEY = "admin:request_errors_pruned_at"

MALFORMED = object()
_SPACES = re.compile(r"\s+")


def read_state(conn: sqlite3.Connection, key: str) -> tuple[Any, int | None]:
    """`(value, updated_at)`; `(None, None)` without a row and `(MALFORMED, updated_at)` for invalid JSON."""
    row = conn.execute("SELECT value_json, updated_at FROM service_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None, None
    try:
        return json.loads(row["value_json"]), int(row["updated_at"])
    except (TypeError, ValueError):
        return MALFORMED, int(row["updated_at"])


def write_state(conn: sqlite3.Connection, key: str, value: Any, now: int) -> None:
    """Insert or replace one row; run it inside a writer transaction."""
    conn.execute(
        "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
        (key, json.dumps(value, sort_keys=True), int(now)),
    )


def text(value: Any, limit: int = 300) -> str:
    """A string collapsed to one line of at most `limit` characters; "" for anything else."""
    if not isinstance(value, str):
        return ""
    return _SPACES.sub(" ", value).strip()[:limit]


def as_int(value: Any) -> int | None:
    """An integer (never a bool), or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def format_time(ts: int) -> str:
    """Unix seconds as "Sep 14, 2026, 03:00 UTC" (the website's `datetime` filter format)."""
    moment = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return f"{moment:%b} {moment.day}, {moment.year}, {moment:%H:%M} UTC"
