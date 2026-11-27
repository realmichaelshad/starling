"""Create and verify encrypted backup archives of the control database and customer data.

An archive holds (paths relative to STARLING_DATA_DIR):

    control/control.db       consistent snapshot through the SQLite backup API
    tenants/<seq>/...        SQLite files (*.db, *.sqlite, *.sqlite3 with a SQLite header) through
                             the backup API; every other regular file copied as-is. WAL, shared-memory
                             and journal files are skipped (the snapshots contain their content).
                             Symbolic links and special files are never followed; they are listed as
                             skipped. Only folders named by a tenant number are included.
    starling-manifest.json   last member: every file with size and SHA-256, directories, schema
                             version, app version and creation time

The tar stream is gzip-compressed, then encrypted (crypto.py) straight into
`<BACKUP_DIR>/starling-backup-<UTC timestamp>.sbk.partial`, which is renamed once complete.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from .. import __version__
from ..config import Settings
from . import crypto, runas, sqlite_io
from .common import (
    BackupConfigError,
    BackupError,
    BackupIntegrityError,
    ensure_private_directory,
    exclusive_lock,
    fsync_directory,
    resolve_passphrase,
)
from .retention import PARTIAL_SUFFIX, STAGING_PREFIX, archive_name

log = logging.getLogger(__name__)

MANIFEST_NAME = "starling-manifest.json"
MANIFEST_FORMAT = "starling-backup-manifest"
MANIFEST_VERSION = 1
CONTROL_DB_MEMBER = "control/control.db"
TENANTS_MEMBER = "tenants"
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SIDECAR_SUFFIXES = tuple(f"{s}-{kind}" for s in SQLITE_SUFFIXES for kind in ("wal", "shm", "journal"))
COPY_BUFFER = 1 << 20
SPOOL_IN_MEMORY = 8 << 20
MAX_MANIFEST_BYTES = 64 << 20
GZIP_LEVEL = 6


@dataclass(frozen=True)
class BackupResult:
    path: Path
    created_at: int
    files: int
    bytes: int
    archive_bytes: int
    schema_version: int
    skipped: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class VerifyResult:
    manifest: dict
    files: int
    bytes: int


# --------------------------------------------------------------------------- creating


def create_backup(
    settings: Settings,
    *,
    passphrase: str | None = None,
    output_dir: Path | None = None,
    now: datetime | None = None,
    verify: bool = True,
    kdf_log2_n: int = crypto.DEFAULT_LOG2_N,
) -> BackupResult:
    """Write one encrypted archive and return what it contains. Safe while the service runs."""
    secret = resolve_passphrase(settings, passphrase, for_writing=True)
    if not settings.control_db_path.is_file():
        raise BackupConfigError(f"control database not found at {settings.control_db_path}; nothing to back up")
    out_dir = Path(output_dir or settings.backup_dir)
    if _is_within(out_dir, settings.tenants_dir) or _is_within(out_dir, settings.control_db_path.parent):
        raise BackupConfigError("BACKUP_DIR must not be inside the control or tenants folders")
    ensure_private_directory(out_dir)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    with exclusive_lock(out_dir):
        final_path = _unique_archive_path(out_dir, moment)
        partial = final_path.with_name(final_path.name + PARTIAL_SUFFIX)
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=out_dir))
        try:
            result = _write_archive(settings, secret, partial, staging, moment, kdf_log2_n)
            os.replace(partial, final_path)
            fsync_directory(out_dir)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    result = replace(result, path=final_path, archive_bytes=final_path.stat().st_size)
    log.info("backup archive written: %s (%d files, %d bytes)", final_path.name, result.files, result.bytes)
    if verify:
        verify_archive(final_path, secret)
        log.info("backup archive verified: %s", final_path.name)
    return result


def _write_archive(
    settings: Settings, secret: str, partial: Path, staging: Path, moment: datetime, log2_n: int
) -> BackupResult:
    files: list[dict] = []
    directories: list[str] = []
    skipped: list[dict] = []
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with open(os.open(partial, flags, 0o600), "wb", buffering=COPY_BUFFER) as raw:
        writer = crypto.EncryptingWriter(raw, secret, log2_n=log2_n)
        with gzip.GzipFile(filename="", fileobj=writer, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                privileged = runas.privileged(settings)
                control_stat = os.stat(settings.control_db_path)
                snapshot = staging / "control.db"
                kind, note = _snapshot(
                    settings.control_db_path, snapshot, control_stat, runas.control_identity() if privileged else None
                )
                if kind != "sqlite":
                    raise BackupError(f"SQLite could not read the control database: {note}")
                if note:
                    log.warning("control database: %s", note)
                schema_version = sqlite_io.schema_version(snapshot)
                files.append(_add_snapshot(tar, CONTROL_DB_MEMBER, snapshot, control_stat, kind))
                for rel, path, st, is_dir in _tenant_entries(settings.tenants_dir, skipped):
                    identity = runas.tenant_identity(settings, int(rel.split("/")[1])) if privileged else None
                    if is_dir:
                        _add_directory(tar, rel, st)
                        directories.append(rel)
                    elif path.name.lower().endswith(SIDECAR_SUFFIXES):
                        continue
                    elif identity is not None and identity.uid is not None and st.st_uid != identity.uid:
                        # Root never archives a file the customer's engine user does not own: a planted
                        # link could otherwise pull another customer's or a system file into the archive.
                        skipped.append({"path": rel, "reason": "not owned by the customer's engine user"})
                    elif path.name.lower().endswith(SQLITE_SUFFIXES) and _has_sqlite_header(path, st):
                        entry = _add_tenant_database(tar, rel, path, st, staging, identity, skipped)
                        if entry is not None:
                            files.append(entry)
                    else:
                        entry = _add_copied_file(tar, rel, path, st, staging, skipped)
                        if entry is not None:
                            files.append(entry)
                total = sum(item["size"] for item in files)
                manifest = {
                    "format": MANIFEST_FORMAT,
                    "format_version": MANIFEST_VERSION,
                    "created_at": int(moment.timestamp()),
                    "created_at_utc": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "app_version": __version__,
                    "schema_version": schema_version,
                    "environment": settings.env,
                    "files": files,
                    "directories": directories,
                    "skipped": skipped,
                    "totals": {"files": len(files), "bytes": total},
                }
                data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
                info = _tar_info(MANIFEST_NAME, int(moment.timestamp()), 0o600)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        writer.finish()
        raw.flush()
        os.fsync(raw.fileno())
    return BackupResult(partial, int(moment.timestamp()), len(files), total, 0, schema_version, skipped)


def _snapshot(
    source: Path, destination: Path, st: os.stat_result, identity: runas.Identity | None
) -> tuple[str, str]:
    """Write a consistent snapshot of `source` into the new file `destination`.

    Returns ("sqlite", note) for a snapshot, or ("file", reason) when SQLite could not read the
    database and the raw bytes were copied instead. With an `identity` the work runs in a helper
    process as that user (runas.py); without one it runs in this process.
    """
    destination.unlink(missing_ok=True)
    if identity is None:
        try:
            check = sqlite_io.snapshot_sqlite(source, destination)
        except sqlite3.DatabaseError as exc:
            destination.unlink(missing_ok=True)
            with _open_no_follow(source, st) as handle, open(destination, "xb") as out:
                shutil.copyfileobj(handle, out, COPY_BUFFER)
            return "file", str(exc)
        return "sqlite", "" if check == "ok" else f"quick_check: {check}"
    try:
        code, _, message = runas.run_helper(
            identity, ["snapshot", str(source)], stdout_path=destination, timeout=runas.SNAPSHOT_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError(f"the snapshot helper could not run for {source.name}: {exc}") from exc
    if code == 0:
        return "sqlite", message
    if code == runas.EXIT_RAW_COPY:
        return "file", message
    raise BackupError(f"the snapshot helper failed for {source.name}: {message or f'exit status {code}'}")


def _add_tenant_database(
    tar: tarfile.TarFile,
    rel: str,
    path: Path,
    st: os.stat_result,
    staging: Path,
    identity: runas.Identity | None,
    skipped: list[dict],
) -> dict | None:
    snapshot = staging / "tenant.db"
    try:
        kind, note = _snapshot(path, snapshot, st, identity)
    except (OSError, BackupError) as exc:
        snapshot.unlink(missing_ok=True)
        log.warning("could not snapshot %s: %s", rel, exc)
        skipped.append({"path": rel, "reason": f"could not be read during the backup: {exc}"[:300]})
        return None
    if kind != "sqlite":
        # One damaged customer database must not stop everyone else's backup.
        log.warning("SQLite could not read %s (%s); archived the raw file", rel, note)
        skipped.append({"path": rel, "reason": f"SQLite snapshot failed, raw file archived: {note}"[:300]})
    elif note:
        log.warning("%s: %s", rel, note)
    return _add_snapshot(tar, rel, snapshot, st, kind)


def _tenant_entries(tenants_dir: Path, skipped: list[dict]) -> Iterator[tuple[str, Path, os.stat_result, bool]]:
    try:
        with os.scandir(tenants_dir) as scan:
            entries = sorted(scan, key=lambda e: e.name)
    except FileNotFoundError:
        return
    for entry in entries:
        rel = f"{TENANTS_MEMBER}/{entry.name}"
        st = entry.stat(follow_symlinks=False)
        if not (entry.name.isascii() and entry.name.isdigit() and int(entry.name) > 0 and stat.S_ISDIR(st.st_mode)):
            skipped.append({"path": rel, "reason": "not a tenant folder"})
            continue
        yield rel, Path(entry.path), st, True
        yield from _walk(Path(entry.path), rel, skipped)


def _walk(base: Path, prefix: str, skipped: list[dict]) -> Iterator[tuple[str, Path, os.stat_result, bool]]:
    with os.scandir(base) as scan:
        entries = sorted(scan, key=lambda e: e.name)
    for entry in entries:
        rel = f"{prefix}/{entry.name}"
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            skipped.append({"path": rel.encode("utf-8", "replace").decode("utf-8"), "reason": "name is not UTF-8"})
            continue
        st = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            yield rel, Path(entry.path), st, True
            yield from _walk(Path(entry.path), rel, skipped)
        elif stat.S_ISREG(st.st_mode):
            yield rel, Path(entry.path), st, False
        else:
            skipped.append({"path": rel, "reason": "symbolic link" if stat.S_ISLNK(st.st_mode) else "special file"})


def _open_no_follow(path: Path, expected: os.stat_result) -> BinaryIO:
    """Open a regular file without following a link swapped in after the directory scan."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    handle = open(fd, "rb", buffering=COPY_BUFFER)
    now = os.fstat(fd)
    if not stat.S_ISREG(now.st_mode) or (os.name != "nt" and (now.st_dev, now.st_ino) != (expected.st_dev, expected.st_ino)):
        handle.close()
        raise FileNotFoundError(f"{path} changed during the backup")
    return handle


def _has_sqlite_header(path: Path, st: os.stat_result) -> bool:
    try:
        with _open_no_follow(path, st) as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _tar_info(name: str, mtime: int, mode: int, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mtime = mtime
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _add_directory(tar: tarfile.TarFile, rel: str, st: os.stat_result) -> None:
    tar.addfile(_tar_info(rel, int(st.st_mtime), stat.S_IMODE(st.st_mode) or 0o700, tarfile.DIRTYPE))


def _add_stream(tar: tarfile.TarFile, rel: str, handle: BinaryIO, size: int, st: os.stat_result, kind: str) -> dict:
    info = _tar_info(rel, int(st.st_mtime), stat.S_IMODE(st.st_mode) or 0o600)
    info.size = size
    reader = _HashingReader(handle)
    tar.addfile(info, reader)
    return {"path": rel, "kind": kind, "size": size, "sha256": reader.digest.hexdigest()}


def _add_snapshot(tar: tarfile.TarFile, rel: str, snapshot: Path, st: os.stat_result, kind: str) -> dict:
    try:
        with open(snapshot, "rb", buffering=COPY_BUFFER) as handle:
            return _add_stream(tar, rel, handle, os.fstat(handle.fileno()).st_size, st, kind)
    finally:
        snapshot.unlink(missing_ok=True)


def _add_copied_file(
    tar: tarfile.TarFile, rel: str, path: Path, st: os.stat_result, staging: Path, skipped: list[dict]
) -> dict | None:
    """Copy first (so a file growing or shrinking mid-read cannot break the tar stream), then add."""
    try:
        source = _open_no_follow(path, st)
    except OSError:
        skipped.append({"path": rel, "reason": "vanished or changed during the backup"})
        return None
    with source, tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY, dir=staging) as spool:
        shutil.copyfileobj(source, spool, COPY_BUFFER)
        size = spool.tell()
        spool.seek(0)
        return _add_stream(tar, rel, spool, size, st, "file")


class _HashingReader:
    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        self.digest.update(data)
        return data


def _unique_archive_path(directory: Path, moment: datetime) -> Path:
    for sequence in range(1, 1000):
        candidate = directory / archive_name(moment, sequence)
        if not candidate.exists() and not candidate.with_name(candidate.name + PARTIAL_SUFFIX).exists():
            return candidate
    raise BackupError("too many archives with the same timestamp")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- reading and verification


def verify_archive(path: Path, passphrase: str) -> VerifyResult:
    """Decrypt the whole archive and check every file against the manifest. Writes nothing."""
    return read_archive(path, passphrase)


def read_archive(path: Path, passphrase: str, extract_to: Path | None = None) -> VerifyResult:
    """Decrypt and check an archive; with `extract_to`, also write its files under that folder.

    Raises BackupIntegrityError (WrongPassphraseError for a wrong passphrase) unless every file
    matches the manifest. Files may already be written when it raises, so callers extract into
    a staging folder and discard it on failure.
    """
    path = Path(path)
    if not path.is_file():
        raise BackupConfigError(f"archive not found: {path}")
    seen: dict[str, tuple[int, str]] = {}
    directories: set[str] = set()
    manifest: dict | None = None
    try:
        with open(path, "rb", buffering=COPY_BUFFER) as raw:
            reader = io.BufferedReader(crypto.DecryptingReader(raw, passphrase), COPY_BUFFER)
            with gzip.GzipFile(fileobj=reader, mode="rb") as gz, tarfile.open(fileobj=gz, mode="r|") as tar:
                for member in tar:
                    if manifest is not None:
                        raise BackupIntegrityError("archive has data after its manifest")
                    if member.name == MANIFEST_NAME and member.isfile():
                        if member.size > MAX_MANIFEST_BYTES:
                            raise BackupIntegrityError("archive manifest is too large")
                        manifest = _parse_manifest(tar.extractfile(member).read())
                        continue
                    rel = _safe_member_name(member.name)
                    if member.isdir():
                        directories.add(rel)
                        if extract_to is not None:
                            _make_directory(extract_to, rel)
                        continue
                    if not member.isfile():
                        raise BackupIntegrityError(f"archive member {rel!r} is not a regular file")
                    if rel in seen:
                        raise BackupIntegrityError(f"archive member {rel!r} appears twice")
                    seen[rel] = _consume_member(tar.extractfile(member), extract_to, rel, member)
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        raise BackupIntegrityError(f"archive could not be read: {exc}") from exc

    if manifest is None:
        raise BackupIntegrityError("archive has no manifest (it is incomplete)")
    expected = {item["path"]: (item["size"], item["sha256"]) for item in manifest["files"]}
    missing = set(expected) - set(seen)
    unlisted = set(seen) - set(expected)
    if missing or unlisted:
        raise BackupIntegrityError(
            f"archive does not match its manifest: {len(missing)} missing and {len(unlisted)} unlisted file(s)"
        )
    mismatched = sorted(rel for rel, value in seen.items() if expected[rel] != value)
    if mismatched:
        raise BackupIntegrityError(f"checksum mismatch in {len(mismatched)} file(s), first: {mismatched[0]}")
    if set(manifest["directories"]) != directories:
        raise BackupIntegrityError("archive folders do not match its manifest")
    if CONTROL_DB_MEMBER not in seen:
        raise BackupIntegrityError("archive has no control database")
    return VerifyResult(manifest, len(seen), sum(size for size, _ in seen.values()))


def _parse_manifest(data: bytes) -> dict:
    try:
        manifest = json.loads(data.decode("utf-8"))
        valid = (
            isinstance(manifest, dict)
            and manifest.get("format") == MANIFEST_FORMAT
            and isinstance(manifest.get("format_version"), int)
            and isinstance(manifest.get("schema_version"), int)
            and isinstance(manifest.get("files"), list)
            and isinstance(manifest.get("directories"), list)
            and all(_valid_file_entry(item) for item in manifest["files"])
            and len({item["path"] for item in manifest["files"]}) == len(manifest["files"])
            and all(isinstance(item, str) for item in manifest["directories"])
        )
    except (UnicodeDecodeError, ValueError):
        valid = False
    if not valid:
        raise BackupIntegrityError("archive manifest is malformed")
    if manifest["format_version"] > MANIFEST_VERSION:
        raise BackupIntegrityError("archive manifest is newer than this version of Starling understands")
    return manifest


def _valid_file_entry(item: object) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("size"), int)
        and item["size"] >= 0
        and isinstance(item.get("sha256"), str)
        and len(item["sha256"]) == 64
        and all(c in "0123456789abcdef" for c in item["sha256"])
    )


def _safe_member_name(name: str) -> str:
    """Only `control/control.db` and paths under `tenants/<number>/`; no absolute or parent paths."""
    parts = name.split("/")
    valid = (
        bool(name)
        and "\\" not in name
        and "\x00" not in name
        and all(part not in ("", ".", "..") for part in parts)
        and (
            name == CONTROL_DB_MEMBER
            or (parts[0] == TENANTS_MEMBER and len(parts) >= 2 and parts[1].isascii() and parts[1].isdigit())
        )
    )
    if not valid:
        raise BackupIntegrityError(f"archive member has an unsafe or unexpected name: {name[:120]!r}")
    return name


def _local_path(root: Path, rel: str) -> Path:
    if os.name == "nt" and ":" in rel:
        raise BackupError(f"cannot restore {rel!r} on Windows (the name contains ':')")
    return root.joinpath(*rel.split("/"))


def _make_directory(root: Path, rel: str) -> None:
    target = _local_path(root, rel)
    try:
        target.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(target, 0o700)
    except OSError as exc:
        raise BackupError(f"cannot create folder {target}: {exc}") from exc


def _consume_member(
    source: BinaryIO | None, extract_to: Path | None, rel: str, member: tarfile.TarInfo
) -> tuple[int, str]:
    if source is None:
        raise BackupIntegrityError(f"archive member {rel!r} has no data")
    digest = hashlib.sha256()
    size = 0
    target = _local_path(extract_to, rel) if extract_to is not None else None
    out = None
    if target is not None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            out = open(os.open(target, flags, 0o600), "wb", buffering=COPY_BUFFER)
        except OSError as exc:
            raise BackupError(f"cannot create {target}: {exc}") from exc
    try:
        while chunk := source.read(COPY_BUFFER):
            digest.update(chunk)
            size += len(chunk)
            if out is not None:
                try:
                    out.write(chunk)
                except OSError as exc:
                    raise BackupError(f"cannot write {target}: {exc}") from exc
        if out is not None:
            try:
                out.flush()
                os.fsync(out.fileno())
            except OSError as exc:
                raise BackupError(f"cannot write {target}: {exc}") from exc
    finally:
        if out is not None:
            out.close()
    if target is not None:
        try:
            os.utime(target, (member.mtime, member.mtime))
            if os.name != "nt":
                os.chmod(target, (stat.S_IMODE(member.mode) & 0o770) | 0o600)
        except OSError as exc:
            raise BackupError(f"cannot set times or mode on {target}: {exc}") from exc
    return size, digest.hexdigest()
