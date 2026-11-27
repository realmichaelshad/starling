"""Backup, wipe, restore: the data comes back exactly, even when captured under concurrent writes."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time

import pytest

from starling_server import __version__
from starling_server.config import load_settings
from starling_server.db import Database, migrations
from starling_server.ops.archive import create_backup, verify_archive
from starling_server.ops.common import BackupConfigError
from starling_server.ops.restore import restore_archive

from .conftest import FAST_KDF, FIXED_NOW, dump, make_tenant_db, populate_control, write_tenant_files


def _wipe(settings) -> None:
    for folder in (settings.data_dir / "control", settings.tenants_dir):
        if folder.exists():
            shutil.rmtree(folder)


def test_backup_wipe_restore_reproduces_databases_and_files(ops_settings):
    populate_control(ops_settings)
    files = write_tenant_files(ops_settings)
    tenant_db = ops_settings.tenants_dir / "1" / "starling.db"
    make_tenant_db(tenant_db)
    (ops_settings.tenants_dir / "not-a-tenant").mkdir()
    # Keep uncheckpointed rows in the tenant's WAL file while the backup runs.
    live = sqlite3.connect(tenant_db)
    try:
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.executemany("INSERT INTO posts (body) VALUES (?)", [(f"late {i}",) for i in range(50)])
        live.commit()
        assert (tenant_db.parent / "starling.db-wal").stat().st_size > 0
        result = create_backup(ops_settings, kdf_log2_n=FAST_KDF, now=FIXED_NOW)
        tenant_dump = list(live.iterdump())
    finally:
        live.close()
    control_dump = dump(ops_settings.control_db_path)

    assert result.path.name == "starling-backup-20260914T030000Z.sbk"
    assert result.path.parent == ops_settings.backup_dir
    assert result.files == 2 + len(files)
    assert {"path": "tenants/not-a-tenant", "reason": "not a tenant folder"} in result.skipped
    assert not list(ops_settings.backup_dir.glob("*.partial")) and not list(ops_settings.backup_dir.glob(".staging-*"))

    _wipe(ops_settings)
    restored = restore_archive(ops_settings, result.path, ops_settings.data_dir)

    assert restored.files == result.files and restored.moved_aside is None
    names = {p.name for p in ops_settings.data_dir.rglob("*")}
    assert not any(n.endswith(("-wal", "-shm", "-journal")) for n in names)
    assert dump(ops_settings.control_db_path) == control_dump
    assert dump(tenant_db) == tenant_dump
    assert sum(1 for line in tenant_dump if line.startswith('INSERT INTO "posts"')) == 250
    for rel, data in files.items():
        assert ops_settings.data_dir.joinpath(*rel.split("/")).read_bytes() == data
    assert (ops_settings.tenants_dir / "2" / "empty-folder").is_dir()
    assert not (ops_settings.tenants_dir / "not-a-tenant").exists()
    assert not any(p.name.startswith(".restore-staging-") for p in ops_settings.data_dir.iterdir())

    manifest = verify_archive(result.path, ops_settings.backup_passphrase).manifest
    assert manifest["schema_version"] == migrations.latest_version()
    assert manifest["app_version"] == __version__
    assert manifest["created_at"] == int(FIXED_NOW.timestamp())
    entry = next(item for item in manifest["files"] if item["path"] == "tenants/2/notes.txt")
    assert entry["size"] == len(files["tenants/2/notes.txt"]) and len(entry["sha256"]) == 64


def test_backup_taken_during_concurrent_writes_is_consistent(ops_settings):
    database = Database(ops_settings.control_db_path)
    database.write(lambda c: c.execute("INSERT INTO service_state (key, value_json, updated_at) VALUES ('n', '0', 0)"))
    committed = [0]
    stop = threading.Event()

    def writer() -> None:
        n = 0
        while not stop.is_set():
            n += 1

            def step(conn, n=n):
                conn.execute(
                    "INSERT INTO admin_audit (admin_id, action, params_json, created_at) VALUES ('a', 'x', ?, ?)",
                    (json.dumps({"pad": "x" * 400}), n),
                )
                conn.execute("UPDATE service_state SET value_json = ? WHERE key = 'n'", (str(n),))

            database.write(step)
            committed[0] = n

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while committed[0] < 200 and time.monotonic() < deadline:
            time.sleep(0.01)
        before = committed[0]
        result = create_backup(ops_settings, kdf_log2_n=FAST_KDF)
        during = committed[0] - before
    finally:
        stop.set()
        thread.join()
        database.close()
    assert before >= 200 and during > 0, "the writer must keep committing while the backup runs"

    _wipe(ops_settings)
    restore_archive(ops_settings, result.path, ops_settings.data_dir)
    conn = sqlite3.connect(ops_settings.control_db_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        counter = int(conn.execute("SELECT value_json FROM service_state WHERE key = 'n'").fetchone()[0])
        rows = [r[0] for r in conn.execute("SELECT created_at FROM admin_audit ORDER BY id")]
    finally:
        conn.close()
    assert rows == list(range(1, counter + 1)), "snapshot must fall exactly on a transaction boundary"
    assert before <= counter <= committed[0]


def test_symbolic_links_are_never_followed(ops_settings, tmp_path):
    populate_control(ops_settings)
    secret = tmp_path / "outside.txt"
    secret.write_text("must not be archived")
    tenant = ops_settings.tenants_dir / "1"
    tenant.mkdir(parents=True)
    try:
        os.symlink(secret, tenant / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("creating symbolic links is not permitted here")
    result = create_backup(ops_settings, kdf_log2_n=FAST_KDF)
    assert {"path": "tenants/1/link.txt", "reason": "symbolic link"} in result.skipped
    manifest = verify_archive(result.path, ops_settings.backup_passphrase).manifest
    assert all(item["path"] != "tenants/1/link.txt" for item in manifest["files"])


def test_a_damaged_customer_database_does_not_stop_the_backup(ops_settings):
    populate_control(ops_settings)
    write_tenant_files(ops_settings)
    damaged = ops_settings.tenants_dir / "3" / "starling.db"
    damaged.parent.mkdir(parents=True)
    damaged.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)  # valid magic, impossible page size

    result = create_backup(ops_settings, kdf_log2_n=FAST_KDF)

    assert any(
        item["path"] == "tenants/3/starling.db" and "raw file archived" in item["reason"] for item in result.skipped
    )
    manifest = verify_archive(result.path, ops_settings.backup_passphrase).manifest
    entry = next(item for item in manifest["files"] if item["path"] == "tenants/3/starling.db")
    assert entry["kind"] == "file" and entry["size"] == 4112
    assert any(item["path"] == "tenants/1/cache/blob.bin" for item in manifest["files"])


def test_production_refuses_to_back_up_without_a_strong_passphrase(ops_env):
    env = dict(ops_env, STARLING_ENV="production", STARLING_ISOLATION="os", BACKUP_PASSPHRASE="")
    settings = load_settings(env)
    with pytest.raises(BackupConfigError, match="production"):
        create_backup(settings)
    weak = load_settings(dict(env, BACKUP_PASSPHRASE="short"))
    with pytest.raises(BackupConfigError, match="shorter than"):
        create_backup(weak)
    assert not settings.backup_dir.exists()


def test_backup_requires_a_control_database(ops_settings):
    with pytest.raises(BackupConfigError, match="control database not found"):
        create_backup(ops_settings, kdf_log2_n=FAST_KDF)
