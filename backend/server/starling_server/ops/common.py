"""Shared pieces of the backup tooling: errors, passphrase rules, locking, durable writes."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import Settings

# A production archive passphrase shorter than this is refused (it protects every customer's data).
MIN_PRODUCTION_PASSPHRASE_CHARS = 16


class BackupError(Exception):
    """A backup, verification, restore or prune could not be completed."""

    exit_code = 2


class BackupConfigError(BackupError):
    """Missing passphrase, unusable paths, or a target that must not be overwritten."""


class BackupBusyError(BackupError):
    """Another backup or prune holds the lock for this backup directory."""


class BackupIntegrityError(BackupError):
    """The archive is not a valid, untampered Starling backup."""

    exit_code = 1


class WrongPassphraseError(BackupIntegrityError):
    """The passphrase does not open this archive."""


def resolve_passphrase(settings: Settings, passphrase: str | None = None, *, for_writing: bool) -> str:
    """Return the passphrase to use, or raise with an operator-facing explanation."""
    value = settings.backup_passphrase if passphrase is None else passphrase
    if not value:
        where = "in production" if settings.is_production else "(backups are always encrypted)"
        raise BackupConfigError(
            f"BACKUP_PASSPHRASE is not set; refusing to run {where}. Set a long random passphrase "
            "and keep a copy outside the server (for example in a password manager)."
        )
    if for_writing and settings.is_production and len(value) < MIN_PRODUCTION_PASSPHRASE_CHARS:
        raise BackupConfigError(
            f"BACKUP_PASSPHRASE is shorter than {MIN_PRODUCTION_PASSPHRASE_CHARS} characters; "
            "use a long random passphrase in production."
        )
    return value


def read_passphrase_file(path: Path) -> str:
    """First line of a file, without the line ending (so `echo ... > file` works)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BackupConfigError(f"cannot read passphrase file {path}: {exc.strerror or exc}") from exc
    first = text.splitlines()[0] if text else ""
    if not first:
        raise BackupConfigError(f"passphrase file {path} is empty")
    return first


@contextmanager
def exclusive_lock(directory: Path, name: str = ".lock") -> Iterator[None]:
    """Non-blocking exclusive lock so a scheduled and a manual run never overlap."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise BackupBusyError(f"another backup or prune is already running (lock file {path})") from exc
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def fsync_directory(path: Path) -> None:
    """Make a rename durable (no-op where directories cannot be opened, such as Windows)."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_private_directory(path: Path) -> None:
    """Create `path` (and parents); restrict it to the owner on POSIX where the filesystem allows."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except PermissionError:
            pass  # for example a mounted folder owned by another user; archives are encrypted anyway
