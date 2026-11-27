"""Restore never overwrites data silently: it refuses, or moves existing data aside with --force."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from starling_server.ops.common import BackupConfigError
from starling_server.ops.restore import aside_location, ownership_plan, restore_archive, target_has_data

from .conftest import dump, edit_manifest, rewrite_archive


def _listing(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_restore_refuses_a_target_with_data_unless_forced(small_archive, ops_settings):
    target = ops_settings.data_dir
    before = _listing(target)
    with pytest.raises(BackupConfigError, match="--force"):
        restore_archive(ops_settings, small_archive.path, target)
    assert _listing(target) == before

    (target / "tenants" / "2" / "notes.txt").write_text("changed after the backup")
    moment = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)
    result = restore_archive(ops_settings, small_archive.path, target, force=True, now=moment)

    assert result.moved_aside == target.parent / "data.pre-restore-20260914T040000Z"
    assert (result.moved_aside / "tenants" / "2" / "notes.txt").read_text() == "changed after the backup"
    assert (target / "tenants" / "2" / "notes.txt").read_text() == "second tenant\n"
    assert dump(result.moved_aside / "control" / "control.db") == dump(target / "control" / "control.db")


def test_unrelated_entries_and_empty_folders_do_not_block_a_restore(small_archive, ops_settings, tmp_path):
    target = tmp_path / "volume"
    (target / "lost+found").mkdir(parents=True)
    (target / "backups").mkdir()
    (target / "backups" / "keep.txt").write_text("untouched")
    (target / "control").mkdir()
    assert not target_has_data(target)

    result = restore_archive(ops_settings, small_archive.path, target)

    assert result.moved_aside is None
    assert (target / "control" / "control.db").is_file()
    assert (target / "backups" / "keep.txt").read_text() == "untouched"
    assert (target / "lost+found").is_dir()


def test_restore_refuses_an_archive_from_a_newer_schema(small_archive, ops_settings, tmp_path):
    rewrite_archive(small_archive.path, lambda members: edit_manifest(members, lambda m: m.update(schema_version=999)))
    with pytest.raises(BackupConfigError, match="newer"):
        restore_archive(ops_settings, small_archive.path, tmp_path / "fresh")
    assert not (tmp_path / "fresh").exists()


def test_existing_data_goes_next_to_the_target_or_inside_a_mount_point(tmp_path, monkeypatch):
    target = tmp_path / "data"
    target.mkdir()
    assert aside_location(target, "20260914T040000Z") == tmp_path / "data.pre-restore-20260914T040000Z"
    monkeypatch.setattr("starling_server.ops.restore.os.path.ismount", lambda p: Path(p) == target)
    assert aside_location(target, "20260914T040000Z") == target / ".pre-restore-20260914T040000Z"


def test_ownership_plan_follows_the_process_and_user_contract(tmp_path):
    target = tmp_path / "data"
    (target / "control").mkdir(parents=True)
    (target / "control" / "control.db").write_bytes(b"db")
    (target / "tenants" / "7" / "cache").mkdir(parents=True)
    (target / "tenants" / "7" / "cache" / "a.bin").write_bytes(b"a")

    plan = {p.relative_to(target).as_posix(): (uid, gid, mode) for p, uid, gid, mode in ownership_plan(target, 200000)}

    assert plan == {
        "control": (10001, 10020, 0o2770),
        "control/control.db": (10001, 10020, 0o660),
        "tenants": (0, 0, 0o711),
        "tenants/7": (200007, 200007, 0o700),
        "tenants/7/cache": (200007, 200007, None),
        "tenants/7/cache/a.bin": (200007, 200007, None),
    }
