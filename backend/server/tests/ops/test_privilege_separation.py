"""Real privilege separation on Linux as root. Runs in the container test image; skipped elsewhere."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from starling_server.config import load_settings
from starling_server.ops import runas, status
from starling_server.ops.archive import create_backup, verify_archive

from .conftest import FAST_KDF, PASSPHRASE, make_tenant_db, populate_control

pytestmark = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0, reason="needs root on Linux (the container test image)"
)

GATEWAY, DB_GROUP, ENGINE_GROUP = runas.GATEWAY_UID, runas.DB_GROUP, runas.ENGINE_GROUP


@pytest.fixture
def root_settings(ops_env):
    base = Path(tempfile.mkdtemp(prefix="starling-ops-", dir="/tmp"))
    os.chmod(base, 0o755)  # pytest's own temporary folders are private to root
    env = dict(ops_env, STARLING_ISOLATION="os", STARLING_DATA_DIR=str(base / "data"), BACKUP_DIR=str(base / "archives"))
    try:
        yield load_settings(env)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _prepare_control(settings) -> None:
    populate_control(settings)
    os.chmod(settings.data_dir, 0o755)
    control = settings.control_db_path.parent
    os.chown(control, GATEWAY, DB_GROUP)
    os.chmod(control, 0o2770)
    for path in control.iterdir():
        os.chown(path, GATEWAY, DB_GROUP)
        os.chmod(path, 0o660)


def test_status_write_keeps_control_database_files_as_the_contract_says(root_settings):
    _prepare_control(root_settings)
    db = root_settings.control_db_path
    holder_code = (
        "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
        "c.execute('SELECT COUNT(*) FROM service_state').fetchone(); print('ready', flush=True); sys.stdin.read()"
    )
    # Like the running gateway: a connection as starling-gw keeps -wal and -shm on disk.
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        user=GATEWAY, group=GATEWAY, extra_groups=[ENGINE_GROUP, DB_GROUP], umask=0o007,
    )
    try:
        assert holder.stdout.readline().strip() == b"ready"
        value = status.build_status(at=1, ok=True, archive="a.sbk", size_bytes=5, verified=True)
        assert status.record_status(root_settings, value)
        assert (db.parent / "control.db-wal").is_file() and (db.parent / "control.db-shm").is_file()
        assert status.file_problems(db) == []
        assert status.read_status(root_settings) == value
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    assert status.file_problems(db) == []


def test_customer_data_is_read_as_the_engine_user_and_planted_links_are_refused(root_settings):
    _prepare_control(root_settings)
    seq = 5
    uid = root_settings.engine_uid_base + seq
    tenant = root_settings.tenants_dir / str(seq)
    make_tenant_db(tenant / "starling.db")
    (tenant / "notes.txt").write_text("customer file")
    secret = root_settings.data_dir / "root-only.txt"
    secret.write_text("root secret")
    os.chmod(secret, 0o600)
    os.link(secret, tenant / "planted-hardlink.txt")
    for path in [tenant, *tenant.rglob("*")]:
        if path.name != "planted-hardlink.txt":
            os.chown(path, uid, uid)
    os.chmod(root_settings.tenants_dir, 0o711)
    os.chmod(tenant, 0o700)
    os.chmod(tenant / "starling.db", 0o600)

    result = create_backup(root_settings, kdf_log2_n=FAST_KDF)

    reasons = {item["path"]: item["reason"] for item in result.skipped}
    assert reasons["tenants/5/planted-hardlink.txt"] == "not owned by the customer's engine user"
    kinds = {item["path"]: item["kind"] for item in verify_archive(result.path, PASSPHRASE).manifest["files"]}
    assert kinds["tenants/5/starling.db"] == "sqlite" and kinds["tenants/5/notes.txt"] == "file"
    assert "tenants/5/planted-hardlink.txt" not in kinds
    assert status.file_problems(root_settings.control_db_path) == []
