"""The `backup:last` status for the owner dashboard: shape, failures, safety and file ownership checks."""
from __future__ import annotations

import json
import os
import sqlite3
import stat

from starling_server.config import load_settings
from starling_server.ops import runas, scheduler, status

from .conftest import populate_control

AT = 1_757_822_400


def _row(settings):
    conn = sqlite3.connect(settings.control_db_path)
    try:
        return conn.execute("SELECT value_json, updated_at FROM service_state WHERE key = 'backup:last'").fetchone()
    finally:
        conn.close()


def test_successful_scheduled_run_records_the_status(ops_settings):
    populate_control(ops_settings)

    run = scheduler.run_scheduled_backup(ops_settings, clock=lambda: AT)

    assert run.ok and run.status_recorded
    value_json, updated_at = _row(ops_settings)
    assert json.loads(value_json) == {
        "at": AT,
        "ok": True,
        "archive": run.archive.name,
        "size_bytes": run.archive.stat().st_size,
        "verified": True,
        "error": None,
    }
    assert updated_at == AT
    assert status.read_status(ops_settings) == json.loads(value_json)


def test_failed_run_records_a_short_error(ops_env):
    settings = load_settings(dict(ops_env, BACKUP_PASSPHRASE=""))
    populate_control(settings)

    run = scheduler.run_scheduled_backup(settings, clock=lambda: AT)

    assert not run.ok and run.status_recorded
    value = json.loads(_row(settings)[0])
    assert value["ok"] is False and value["archive"] is None and value["size_bytes"] is None
    assert value["verified"] is False and value["at"] == AT
    assert "BACKUP_PASSPHRASE" in value["error"] and len(value["error"]) <= status.MAX_ERROR_CHARS


def test_unexpected_failures_are_recorded_without_the_passphrase(ops_settings, monkeypatch):
    populate_control(ops_settings)

    def broken(settings, **kwargs):
        raise RuntimeError("disk full\ntraceback details that do not belong in the dashboard")

    monkeypatch.setattr(scheduler, "create_backup", broken)
    run = scheduler.run_scheduled_backup(ops_settings, clock=lambda: AT)

    value_json = _row(ops_settings)[0]
    assert not run.ok and json.loads(value_json)["error"] == "RuntimeError: disk full"
    assert ops_settings.backup_passphrase not in value_json


def test_nothing_is_written_before_the_control_database_exists(ops_settings):
    assert status.record_status(ops_settings, status.build_status(at=AT, ok=True)) is False
    assert not ops_settings.control_db_path.exists()
    assert status.read_status(ops_settings) is None


def test_error_text_is_one_short_line():
    assert status.short_error("first line\nsecond line") == "first line"
    assert len(status.short_error("x" * 1000)) == status.MAX_ERROR_CHARS
    assert status.short_error("") is None and status.short_error(None) is None


def test_status_goes_through_the_helper_process_when_privileged(ops_settings, monkeypatch):
    populate_control(ops_settings)
    monkeypatch.setattr(runas, "privileged", lambda settings: True)
    monkeypatch.setattr(runas, "control_identity", lambda: runas.CURRENT_USER)
    calls = []
    real_run_helper = runas.run_helper

    def spy(identity, args, **kwargs):
        calls.append(args[0])
        return real_run_helper(identity, args, **kwargs)

    monkeypatch.setattr(runas, "run_helper", spy)
    value = status.build_status(at=AT, ok=False, error="RuntimeError: disk full")

    assert status.record_status(ops_settings, value)
    assert status.read_status(ops_settings) == value
    assert calls == ["state-write", "state-read"]


def test_ownership_check_reports_every_difference_from_the_contract(tmp_path):
    files = {
        "control.db": (stat.S_IFREG | 0o660, 10001, 10020),
        "control.db-wal": (stat.S_IFREG | 0o644, 0, 0),
        "control.db-shm": (stat.S_IFLNK | 0o777, 10001, 10020),
    }

    def fake_lstat(path):
        if path.name not in files:
            raise FileNotFoundError(path)
        mode, uid, gid = files[path.name]
        return os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))

    assert status.file_problems(tmp_path / "control.db", lstat=fake_lstat) == [
        "control.db-wal is owned by 0:0, expected 10001:10020",
        "control.db-wal has mode 644, expected 660",
        "control.db-shm is not a regular file",
    ]
