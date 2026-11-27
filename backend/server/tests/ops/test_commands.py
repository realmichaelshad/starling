"""The operator commands end to end through the CLI dispatcher."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from starling_server import cli
from starling_server.config import get_settings
from starling_server.ops import status

from .conftest import populate_control, write_tenant_files


@pytest.fixture
def cli_env(ops_env, monkeypatch):
    for key, value in ops_env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield ops_env
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "command", ["backup", "backup-verify", "restore", "backup-prune", "backup-status", "proxy-peers"]
)
def test_ops_commands_are_registered(command):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--help"])
    assert exit_info.value.code == 0


def test_backup_verify_restore_and_prune_through_the_cli(cli_env, tmp_path, capsys):
    settings = get_settings()
    populate_control(settings)
    write_tenant_files(settings)

    assert cli.main(["backup"]) == 0
    out = capsys.readouterr().out
    archive = next(settings.backup_dir.glob("starling-backup-*.sbk"))
    assert str(archive) in out and "verified: yes" in out

    assert cli.main(["backup-verify", "--archive", str(archive)]) == 0
    assert "is intact" in capsys.readouterr().out

    assert cli.main(["restore", "--archive", str(archive), "--target", str(settings.data_dir)]) == 2
    err = capsys.readouterr().err
    assert "WARNING" in err and "--force" in err

    target = tmp_path / "restored"
    assert cli.main(["restore", "--archive", str(archive), "--target", str(target)]) == 0
    assert (target / "control" / "control.db").is_file()
    assert (target / "tenants" / "2" / "notes.txt").read_text() == "second tenant\n"

    assert cli.main(["backup-prune", "--dry-run"]) == 0
    assert "kept 1 archive(s)" in capsys.readouterr().out


def test_wrong_passphrase_file_exits_with_status_1(cli_env, tmp_path, capsys):
    settings = get_settings()
    populate_control(settings)
    assert cli.main(["backup", "--no-verify"]) == 0
    archive = next(settings.backup_dir.glob("starling-backup-*.sbk"))
    wrong = tmp_path / "wrong-passphrase.txt"
    wrong.write_text("definitely not it\n")
    capsys.readouterr()

    assert cli.main(["backup-verify", "--archive", str(archive), "--passphrase-file", str(wrong)]) == 1
    assert "wrong backup passphrase" in capsys.readouterr().err


def test_backup_status_shows_the_last_scheduled_run(cli_env, capsys):
    settings = get_settings()
    assert cli.main(["backup-status"]) == 0
    assert "last scheduled run: none recorded" in capsys.readouterr().out

    populate_control(settings)
    at = 1_757_822_400
    status.record_status(settings, status.build_status(at=at, ok=False, error="RuntimeError: disk full"))
    assert cli.main(["backup-status"]) == 1
    when = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    assert f"last scheduled run: FAILED at {when}: RuntimeError: disk full" in capsys.readouterr().out

    status.record_status(settings, status.build_status(at=at, ok=True, archive="x.sbk", size_bytes=1234, verified=True))
    assert cli.main(["backup-status"]) == 0
    assert f"ok at {when}, x.sbk (1,234 bytes), verified: yes" in capsys.readouterr().out
