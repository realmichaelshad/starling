"""Owner alerts: one shape for the dashboard, derived backup alerts, and alert emails.

Stored alerts live in `service_state`; each writer owns its format:

- billing (`billing/mirror.py` `raise_alert`): key `alert:billing:<kind>:<ref>`, value
  {"source", "level", "kind", "ref", "message", "details", "first_at", "last_at", "count"}
- engines (`engines/pool.py`): key `alert.engine_circuit.<tenant_seq>`, value
  {"kind", "severity", "account_id", "tenant_seq", "crashes", "window_s", "opened_at", "retry_after_s"}
- services (`services/metering.py` `record_alert`, raised from `services/upstream.py`): key
  `alert:ai_key_rejected`, value {"at", "key", "upstream_status", "retried_with"}; `key` names the
  slot (primary or secondary), never key material

Derived alerts, production only, from `backup:last` (`ops/status.py`): `backup:failed` when the last
scheduled run failed, and `backup:missing` when no run was recorded for more than 26 hours (counted
from installation, migration 1, while no run was ever recorded).

One more derived alert, in every environment, from the volume that holds the data directory:
`disk:low` when less than 15% is free (an error below 5%). Nothing else checks free space; `/readyz`
does not. It is measured now, so its `created_at` is `now` and `notify` re-sends it every 6 hours
while the volume stays full.

Every alert becomes {"level": "warning" | "error", "text", "created_at", "key"}. `created_at` is when
the alert last fired, so an alert that fires again gets a newer `created_at`. Texts carry metadata
only (kinds, references, tenant numbers, HTTP statuses), never customer content.

`notify` emails new and re-fired alerts to ALERT_EMAIL through `email.sender`, at most once per alert
key every 6 hours, and remembers what it sent in `service_state` `admin:alert_notifications`. Without
ALERT_EMAIL it does nothing.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..db import Database
from ..email import sender
from .common import ALERT_NOTIFICATIONS_KEY, as_int, format_time, read_state, text, write_state

log = logging.getLogger(__name__)

LEVELS = ("warning", "error")
BILLING_PREFIX = "alert:billing:"
ENGINE_PREFIX = "alert.engine_circuit."
SERVICES_PREFIX = "alert:ai_"
BACKUP_STATE_KEY = "backup:last"
BACKUP_FAILED_KEY = "backup:failed"
BACKUP_MISSING_KEY = "backup:missing"
BACKUP_MAX_AGE_S = 26 * 3600
DISK_LOW_KEY = "disk:low"
DISK_WARN_PCT = 15.0
DISK_ERROR_PCT = 5.0
GIB = 1024 ** 3
RECENT_S = 7 * 86_400
RESEND_AFTER_S = 6 * 3600
EMAIL_TEMPLATE = "owner_alert"


def _when(value: Any, fallback: int) -> int:
    number = as_int(value)
    return number if number is not None and number > 0 else int(fallback)


def make_alert(key: str, level: Any, message: str, created_at: int) -> dict[str, Any]:
    return {"level": level if level in LEVELS else "warning", "text": text(message), "created_at": int(created_at), "key": key}


def normalize(key: str, value_json: Any, updated_at: int) -> dict[str, Any]:
    """The dashboard shape of one stored alert; an unreadable value still shows up."""
    try:
        value = json.loads(value_json) if isinstance(value_json, str) else None
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return make_alert(key, "warning", f"An owner alert could not be read ({key}).", updated_at)
    if key.startswith(BILLING_PREFIX):
        return _billing(key, value, updated_at)
    if key.startswith(ENGINE_PREFIX):
        return _engine(key, value, updated_at)
    if key.startswith(SERVICES_PREFIX):
        return _services(key, value, updated_at)
    message = text(value.get("message")) or f"Owner alert {key}."
    level = value.get("level", value.get("severity"))
    return make_alert(key, level, message, _when(value.get("last_at", value.get("at")), updated_at))


def _billing(key: str, value: dict[str, Any], updated_at: int) -> dict[str, Any]:
    parts = [f"Billing: {text(value.get('message')) or 'a billing event needs review.'}"]
    ref = text(value.get("ref"), 120)
    if ref and ref != "-":
        parts.append(f"Reference {ref}.")
    count = as_int(value.get("count"))
    if count is not None and count > 1:
        parts.append(f"Seen {count} times.")
    return make_alert(key, value.get("level"), " ".join(parts), _when(value.get("last_at"), updated_at))


def _engine(key: str, value: dict[str, Any], updated_at: int) -> dict[str, Any]:
    seq = as_int(value.get("tenant_seq"))
    crashes, window_s, retry_s = (as_int(value.get(name)) for name in ("crashes", "window_s", "retry_after_s"))
    who = f"tenant {seq}" if seq is not None else "a customer"
    message = f"The engine for {who}" + (f" crashed {crashes} times" if crashes else " kept crashing")
    if window_s:
        message += f" within {max(1, window_s // 60)} minutes"
    message += f"; new starts are paused for {retry_s} seconds." if retry_s else "; new starts are paused."
    return make_alert(key, value.get("severity"), message, _when(value.get("opened_at"), updated_at))


def _services(key: str, value: dict[str, Any], updated_at: int) -> dict[str, Any]:
    name = key[len("alert:"):]
    if name != "ai_key_rejected":
        return make_alert(key, value.get("level"), f"AI service alert: {name}.", _when(value.get("at"), updated_at))
    slot = value.get("key") if value.get("key") in ("primary", "secondary") else "configured"
    status = as_int(value.get("upstream_status"))
    fallback = value.get("retried_with")
    message = f"The AI provider rejected the {slot} API key" + (f" (HTTP {status})" if status else "")
    if fallback in ("primary", "secondary"):
        message += f"; requests were retried with the {fallback} key."
    else:
        message += "; no other key was available."
    message += " Check the keys in the services environment."
    return make_alert(key, "error", message, _when(value.get("at"), updated_at))


def stored_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key, value_json, updated_at FROM service_state WHERE substr(key, 1, 6) IN ('alert:', 'alert.')"
    ).fetchall()
    return [normalize(row["key"], row["value_json"], int(row["updated_at"])) for row in rows]


def backup_status(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """`backup:last` for the overview: {"at", "ok", "archive", "size_bytes", "verified", "error"}, or None."""
    value, updated_at = read_state(conn, BACKUP_STATE_KEY)
    if updated_at is None:
        return None
    if not isinstance(value, dict):
        return {"at": updated_at, "ok": False, "archive": None, "size_bytes": None, "verified": False,
                "error": "The backup status could not be read."}
    archive = re.split(r"[\\/]", text(value.get("archive")))[-1][:200]
    size = as_int(value.get("size_bytes"))
    return {
        "at": _when(value.get("at"), updated_at),
        "ok": value.get("ok") is True,
        "archive": archive or None,
        "size_bytes": size if size is not None and size >= 0 else None,
        "verified": value.get("verified") is True,
        "error": text(value.get("error"), 200) or None,
    }


def backup_alerts(conn: sqlite3.Connection, settings: Settings, now: int) -> list[dict[str, Any]]:
    """Production only: the last backup run failed, or no run was recorded for more than 26 hours."""
    if not settings.is_production:
        return []
    status = backup_status(conn)
    if status is None:
        row = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = 1").fetchone()
        installed_at = as_int(row["applied_at"]) if row is not None else None
        if installed_at is None or now - installed_at <= BACKUP_MAX_AGE_S:
            return []
        return [make_alert(
            BACKUP_MISSING_KEY, "error",
            "No backup has been recorded, and the server was set up more than 26 hours ago. "
            "Check the nightly backup job and BACKUP_PASSPHRASE.",
            installed_at + BACKUP_MAX_AGE_S,
        )]
    found = []
    if not status["ok"]:
        detail = f": {status['error']}" if status["error"] else "."
        found.append(make_alert(BACKUP_FAILED_KEY, "error", f"The backup run on {format_time(status['at'])} failed{detail}",
                                status["at"]))
    if now - status["at"] > BACKUP_MAX_AGE_S:
        found.append(make_alert(
            BACKUP_MISSING_KEY, "error",
            f"No backup has been recorded for more than 26 hours; the last run was on {format_time(status['at'])}.",
            status["at"] + BACKUP_MAX_AGE_S,
        ))
    return found


PAYMENTS_REHEARSAL_KEY = "payments:test_mode"


def rehearsal_alerts(settings: Settings, now: int) -> list[dict[str, Any]]:
    """Production running on Stripe's sandbox (`STRIPE_ALLOW_TEST_MODE`): nobody can be charged."""
    if not settings.payments_rehearsal:
        return []
    return [make_alert(
        PAYMENTS_REHEARSAL_KEY, "warning",
        "Payments are in test mode: the site runs on Stripe's sandbox, nobody can be charged, and every page says so. "
        "Before launch, switch to live keys (STRIPE_MODE=live) and remove STRIPE_ALLOW_TEST_MODE.",
        now,
    )]


# --------------------------------------------------------------------------- free disk space


def disk_status(settings: Settings) -> dict[str, Any] | None:
    """Free space on the volume holding the data directory, or None when it cannot be measured.

    `settings.data_dir` may not exist yet, so the nearest existing parent is measured instead: it is
    on the same volume. Returns {"path", "total_bytes", "free_bytes", "free_pct"}.
    """
    target: Path | None = next((path for path in (settings.data_dir, *settings.data_dir.parents) if path.exists()), None)
    if target is None:
        return None
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        log.warning("free disk space could not be read for %s", target)
        return None
    total, free = int(usage.total), int(usage.free)
    return {
        "path": str(target),
        "total_bytes": total,
        "free_bytes": free,
        "free_pct": round(free * 100 / total, 2) if total > 0 else 0.0,
    }


def disk_alerts(status: dict[str, Any] | None, now: int) -> list[dict[str, Any]]:
    """`disk:low` below 15% free; an error below 5%. Nothing when the volume cannot be measured."""
    if status is None or status["free_pct"] >= DISK_WARN_PCT:
        return []
    level = "error" if status["free_pct"] < DISK_ERROR_PCT else "warning"
    return [make_alert(
        DISK_LOW_KEY, level,
        f"The data volume ({status['path']}) is {status['free_pct']}% free: "
        f"{status['free_bytes'] / GIB:.1f} GB of {status['total_bytes'] / GIB:.1f} GB. "
        "Engines, backups and the control database all write there.",
        now,
    )]


# --------------------------------------------------------------------------- collecting and emailing


def collect(conn: sqlite3.Connection, settings: Settings, now: int, recent_s: int = RECENT_S) -> list[dict[str, Any]]:
    """Stored alerts that fired within `recent_s`, plus the derived backup and disk alerts; newest first."""
    found = [item for item in stored_alerts(conn) if item["created_at"] >= now - recent_s]
    found.extend(backup_alerts(conn, settings, now))
    found.extend(rehearsal_alerts(settings, now))
    found.extend(disk_alerts(disk_status(settings), now))
    return sorted(found, key=lambda item: (-item["created_at"], item["key"]))


def notify(
    db: Database,
    settings: Settings,
    now: int,
    send: Callable[[Database, Settings, str, str, str, str], None] = sender.send_email,
) -> list[str]:
    """Email the alerts that are new or fired again since their last email (at most once per key every
    6 hours) to ALERT_EMAIL; returns the keys sent. Blocking: run it in a thread."""
    address = settings.alert_email.strip()
    if not address:
        return []
    with db.read() as conn:
        current = collect(conn, settings, now)
        stored, _ = read_state(conn, ALERT_NOTIFICATIONS_KEY)
    sent = _sent_log(stored)
    due = [item for item in current if _due(item, sent.get(item["key"]), now)]
    if due:
        send(db, settings, address, EMAIL_TEMPLATE, _subject(due), _body(settings, due))
        log.info("owner alert email queued for %d alert(s)", len(due))
    active = {item["key"] for item in current}
    kept = {key: entry for key, entry in sent.items() if key in active or now - entry["sent_at"] < RESEND_AFTER_S}
    kept.update({item["key"]: {"sent_at": int(now), "fired_at": item["created_at"]} for item in due})
    if kept != sent:
        db.write(lambda conn: write_state(conn, ALERT_NOTIFICATIONS_KEY, kept, now))
    return [item["key"] for item in due]


def _sent_log(stored: Any) -> dict[str, dict[str, int]]:
    if not isinstance(stored, dict):
        return {}
    entries: dict[str, dict[str, int]] = {}
    for key, entry in stored.items():
        if isinstance(entry, dict) and as_int(entry.get("sent_at")) is not None and as_int(entry.get("fired_at")) is not None:
            entries[str(key)] = {"sent_at": entry["sent_at"], "fired_at": entry["fired_at"]}
    return entries


def _due(item: dict[str, Any], entry: dict[str, int] | None, now: int) -> bool:
    if entry is None:
        return True
    return item["created_at"] > entry["fired_at"] and now - entry["sent_at"] >= RESEND_AFTER_S


def _subject(due: list[dict[str, Any]]) -> str:
    return f"Starling: {len(due)} owner alert{'' if len(due) == 1 else 's'}"


def _body(settings: Settings, due: list[dict[str, Any]]) -> str:
    lines = ["The Starling owner dashboard has new or repeated alerts:", ""]
    lines += [f"- [{item['level'].upper()}] {item['text']} ({format_time(item['created_at'])})" for item in due]
    lines += [
        "",
        f"Review them at {settings.public_url}/admin",
        f"Each alert is emailed at most once every {RESEND_AFTER_S // 3600} hours.",
    ]
    return "\n".join(lines)
