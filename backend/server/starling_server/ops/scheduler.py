"""Nightly backups, started by the supervisor.

    from starling_server.ops.scheduler import start_backup_thread

    thread, stop = start_backup_thread(settings)   # run_backup_loop(settings, stop, hour_utc=3)
    ...
    stop.set()
    thread.join(timeout=30)

Every day at `hour_utc`:00 UTC the loop writes an archive, verifies it and prunes old ones (7 daily,
4 weekly, 3 monthly), then records the outcome for the owner dashboard in the `service_state` key
`backup:last` (status.py). When the loop starts and the newest archive is more than 26 hours old
(the machine was down at the scheduled time), a catch-up run starts after 5 minutes. Errors are
logged and recorded, never raised. A run in progress is not interrupted by `stop_event`; if the
process exits mid-run only a `.partial` file remains, which the next prune removes.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..config import Settings
from .archive import create_backup
from .common import BackupConfigError, exclusive_lock
from .retention import (
    DEFAULT_KEEP_DAILY,
    DEFAULT_KEEP_MONTHLY,
    DEFAULT_KEEP_WEEKLY,
    list_archives,
    prune_archives,
)
from .status import build_status, record_status

log = logging.getLogger(__name__)

CATCH_UP_AFTER_S = 26 * 3600
CATCH_UP_DELAY_S = 300
MAX_SLEEP_S = 300  # re-read the clock regularly (suspend, resume, clock corrections)


@dataclass(frozen=True)
class ScheduledRun:
    ok: bool
    archive: Path | None
    deleted: int
    error: str | None
    status_recorded: bool = False


def next_run_at(now: float, hour_utc: int) -> float:
    moment = datetime.fromtimestamp(now, timezone.utc)
    candidate = moment.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def first_run_at(settings: Settings, now: float, hour_utc: int) -> float:
    scheduled = next_run_at(now, hour_utc)
    if not settings.control_db_path.is_file():
        return scheduled
    archives = list_archives(settings.backup_dir)
    if not archives or now - archives[0].created_at.timestamp() > CATCH_UP_AFTER_S:
        return min(scheduled, now + CATCH_UP_DELAY_S)
    return scheduled


def run_scheduled_backup(
    settings: Settings,
    *,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    keep_weekly: int = DEFAULT_KEEP_WEEKLY,
    keep_monthly: int = DEFAULT_KEEP_MONTHLY,
    clock: Callable[[], float] = time.time,
) -> ScheduledRun:
    """One backup, verification and prune, then the `backup:last` status. Never raises."""
    archive: Path | None = None
    size: int | None = None
    verified = False
    deleted = 0
    error: str | None = None
    try:
        result = create_backup(settings, verify=True)
        archive, size, verified = result.path, result.archive_bytes, True
        with exclusive_lock(settings.backup_dir):
            plan = prune_archives(settings.backup_dir, keep_daily, keep_weekly, keep_monthly)
        deleted = len(plan.delete)
        log.info("nightly backup ok: %s (%d files); pruned %d old archive(s)", archive.name, result.files, deleted)
    except BackupConfigError as exc:
        log.error("nightly backup did not run: %s", exc)
        error = str(exc)
    except Exception as exc:  # keep the loop alive; the log carries the traceback
        log.exception("nightly backup failed")
        error = f"{type(exc).__name__}: {exc}"
    status = build_status(at=clock(), ok=error is None, archive=archive, size_bytes=size, verified=verified, error=error)
    try:
        recorded = record_status(settings, status)
    except Exception:  # the status is best effort; the run itself is already logged
        log.exception("backup status could not be recorded")
        recorded = False
    return ScheduledRun(error is None, archive, deleted, error, recorded)


def run_backup_loop(
    settings: Settings,
    stop_event: threading.Event,
    hour_utc: int = 3,
    *,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    keep_weekly: int = DEFAULT_KEEP_WEEKLY,
    keep_monthly: int = DEFAULT_KEEP_MONTHLY,
    clock: Callable[[], float] = time.time,
) -> None:
    """Block until `stop_event` is set, running a backup every day at `hour_utc`:00 UTC."""
    if not 0 <= hour_utc <= 23:
        raise ValueError("hour_utc must be between 0 and 23")
    if not settings.backup_passphrase:
        if not settings.is_production:
            log.warning("nightly backups are off: BACKUP_PASSPHRASE is not set")
            return
        log.error("nightly backups will fail: BACKUP_PASSPHRASE is not set")
    due = first_run_at(settings, clock(), hour_utc)
    log.info("next backup at %s", datetime.fromtimestamp(due, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    while not stop_event.is_set():
        remaining = due - clock()
        if remaining > 0:
            stop_event.wait(min(remaining, MAX_SLEEP_S))
            continue
        run_scheduled_backup(settings, keep_daily=keep_daily, keep_weekly=keep_weekly, keep_monthly=keep_monthly)
        due = next_run_at(clock(), hour_utc)


def start_backup_thread(settings: Settings, hour_utc: int = 3) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(
        target=run_backup_loop, args=(settings, stop, hour_utc), name="backup-scheduler", daemon=True
    )
    thread.start()
    return thread, stop
