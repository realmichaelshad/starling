"""Nightly scheduling: timing, catch-up after downtime, a full scheduled run, and clean stopping."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from starling_server.config import load_settings
from starling_server.ops import scheduler
from starling_server.ops.retention import archive_name, list_archives

from .conftest import populate_control, write_tenant_files

UTC = timezone.utc


def ts(*parts: int) -> float:
    return datetime(*parts, tzinfo=UTC).timestamp()


def test_next_run_is_the_coming_hour_in_utc():
    assert scheduler.next_run_at(ts(2026, 9, 14, 2, 59, 59), 3) == ts(2026, 9, 14, 3, 0)
    assert scheduler.next_run_at(ts(2026, 9, 14, 3, 0), 3) == ts(2026, 9, 15, 3, 0)
    assert scheduler.next_run_at(ts(2026, 12, 31, 23, 30), 3) == ts(2027, 1, 1, 3, 0)


def test_first_run_catches_up_when_the_newest_archive_is_old(ops_settings):
    now = ts(2026, 9, 14, 12, 0)
    assert scheduler.first_run_at(ops_settings, now, 3) == ts(2026, 9, 15, 3, 0)  # no data yet
    populate_control(ops_settings)
    assert scheduler.first_run_at(ops_settings, now, 3) == now + scheduler.CATCH_UP_DELAY_S
    ops_settings.backup_dir.mkdir(parents=True)
    (ops_settings.backup_dir / archive_name(datetime(2026, 9, 14, 3, 0, tzinfo=UTC))).write_bytes(b"x")
    assert scheduler.first_run_at(ops_settings, now, 3) == ts(2026, 9, 15, 3, 0)


def test_loop_runs_when_due_then_stops(ops_settings, monkeypatch):
    calls = []
    stop = threading.Event()

    def fake_run(settings, **kwargs):
        calls.append(kwargs)
        stop.set()
        return scheduler.ScheduledRun(True, None, 0, None)

    monkeypatch.setattr(scheduler, "run_scheduled_backup", fake_run)
    times = iter([ts(2026, 9, 14, 2, 0), ts(2026, 9, 14, 3, 0, 1)])
    thread = threading.Thread(
        target=scheduler.run_backup_loop,
        args=(ops_settings, stop, 3),
        kwargs={"clock": lambda: next(times, ts(2026, 9, 14, 3, 0, 2)), "keep_daily": 5},
    )
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert calls == [{"keep_daily": 5, "keep_weekly": 4, "keep_monthly": 3}]


def test_loop_stops_promptly_while_waiting(ops_settings):
    stop = threading.Event()
    thread = threading.Thread(target=scheduler.run_backup_loop, args=(ops_settings, stop, 3))
    thread.start()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_loop_is_off_in_development_without_a_passphrase(ops_env):
    settings = load_settings(dict(ops_env, BACKUP_PASSPHRASE=""))
    scheduler.run_backup_loop(settings, threading.Event())  # returns immediately instead of blocking


def test_scheduled_run_writes_verifies_and_prunes(ops_settings):
    populate_control(ops_settings)
    write_tenant_files(ops_settings)
    ops_settings.backup_dir.mkdir(parents=True)
    for day in range(1, 11):
        (ops_settings.backup_dir / archive_name(datetime(2026, 1, day, 3, 0, tzinfo=UTC))).write_bytes(b"old")

    outcome = scheduler.run_scheduled_backup(ops_settings, keep_daily=3, keep_weekly=0, keep_monthly=0)

    assert outcome.ok and outcome.error is None and outcome.archive is not None
    names = [a.path.name for a in list_archives(ops_settings.backup_dir)]
    assert names[0] == outcome.archive.name
    assert len(names) == 3 and outcome.deleted == 8


def test_scheduled_run_reports_problems_instead_of_raising(ops_env):
    settings = load_settings(dict(ops_env, BACKUP_PASSPHRASE=""))
    outcome = scheduler.run_scheduled_backup(settings)
    assert not outcome.ok and "BACKUP_PASSPHRASE" in outcome.error
