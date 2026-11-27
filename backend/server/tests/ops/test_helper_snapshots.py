"""The helper-process path production uses (as other users) works end to end as the current user."""
from __future__ import annotations

import pytest

from starling_server.ops import runas
from starling_server.ops.archive import create_backup
from starling_server.ops.restore import restore_archive

from .conftest import FAST_KDF, dump, make_tenant_db, populate_control, write_tenant_files


@pytest.fixture
def helper_calls(monkeypatch):
    calls: list[str] = []
    real_run_helper = runas.run_helper

    def spy(identity, args, **kwargs):
        calls.append(args[0])
        return real_run_helper(identity, args, **kwargs)

    monkeypatch.setattr(runas, "privileged", lambda settings: True)
    monkeypatch.setattr(runas, "control_identity", lambda: runas.CURRENT_USER)
    monkeypatch.setattr(runas, "tenant_identity", lambda settings, seq: runas.CURRENT_USER)
    monkeypatch.setattr(runas, "run_helper", spy)
    return calls


def test_helper_snapshots_restore_exactly(ops_settings, helper_calls, tmp_path):
    populate_control(ops_settings)
    files = write_tenant_files(ops_settings)
    tenant_db = ops_settings.tenants_dir / "1" / "starling.db"
    make_tenant_db(tenant_db)
    damaged = ops_settings.tenants_dir / "3" / "broken.db"
    damaged.parent.mkdir(parents=True)
    damaged.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)

    result = create_backup(ops_settings, kdf_log2_n=FAST_KDF)

    assert helper_calls == ["snapshot", "snapshot", "snapshot"]  # control, tenant 1, tenant 3
    assert any(i["path"] == "tenants/3/broken.db" and "raw file archived" in i["reason"] for i in result.skipped)
    target = tmp_path / "restored"
    restore_archive(ops_settings, result.path, target)
    assert dump(target / "control" / "control.db") == dump(ops_settings.control_db_path)
    assert dump(target / "tenants" / "1" / "starling.db") == dump(tenant_db)
    assert (target / "tenants" / "3" / "broken.db").read_bytes() == damaged.read_bytes()
    for rel, data in files.items():
        assert target.joinpath(*rel.split("/")).read_bytes() == data


def test_a_failing_helper_skips_only_that_customer_file(ops_settings, helper_calls, monkeypatch):
    populate_control(ops_settings)
    make_tenant_db(ops_settings.tenants_dir / "4" / "starling.db")
    real_run_helper = runas.run_helper

    def refuse_tenants(identity, args, **kwargs):
        if "tenants" in args[1]:
            return 1, b"", "PermissionError: [Errno 13] Permission denied"
        return real_run_helper(identity, args, **kwargs)

    monkeypatch.setattr(runas, "run_helper", refuse_tenants)
    result = create_backup(ops_settings, kdf_log2_n=FAST_KDF)

    reasons = {item["path"]: item["reason"] for item in result.skipped}
    assert reasons["tenants/4/starling.db"].startswith("could not be read during the backup")
    assert result.files == 1  # the control database is still archived


def test_files_not_owned_by_the_engine_user_are_never_archived(ops_settings, helper_calls, monkeypatch):
    populate_control(ops_settings)
    write_tenant_files(ops_settings)
    make_tenant_db(ops_settings.tenants_dir / "1" / "starling.db")
    stranger = runas.Identity(4_000_000_000, 4_000_000_000)
    monkeypatch.setattr(runas, "tenant_identity", lambda settings, seq: stranger)

    result = create_backup(ops_settings, kdf_log2_n=FAST_KDF)

    reasons = {item["path"]: item["reason"] for item in result.skipped}
    assert reasons["tenants/2/notes.txt"] == "not owned by the customer's engine user"
    assert reasons["tenants/1/starling.db"] == "not owned by the customer's engine user"
    assert result.files == 1 and helper_calls == ["snapshot"]  # only the control database
