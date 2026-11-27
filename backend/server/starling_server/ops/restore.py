"""Restore an encrypted archive into a data folder.

Stop the gateway, services and every engine first (in the container: the supervisor). Steps:

1. Verify the whole archive. Nothing is written when verification fails.
2. Check the target. It must not already hold a control database or tenant data unless `force`
   is set; then the existing `control/` and `tenants/` folders are moved aside (never deleted) to
   `<target>.pre-restore-<timestamp>/`, or to `<target>/.pre-restore-<timestamp>/` when the target
   is a mount point (a volume root cannot be renamed and its sibling is another filesystem).
3. Extract into `<target>/.restore-staging-*` while checking every checksum again.
4. Move `control/` and `tenants/` into place (renames on one filesystem).
5. With OS isolation and root privileges, apply the owners and modes of CONTRACTS section 2.1.

Other entries of the target (for example `backups/` or `lost+found`) are left untouched.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..db import migrations
from .archive import read_archive, verify_archive
from .common import BackupConfigError, BackupIntegrityError, resolve_passphrase
from .retention import TIMESTAMP_FORMAT

log = logging.getLogger(__name__)

RESTORE_WARNING = (
    "WARNING: stop the gateway, the services process and every engine before restoring "
    "(in the container: stop the supervisor). Restoring while they run corrupts data. "
    "Existing data is moved aside, never deleted."
)
MANAGED = ("control", "tenants")
GATEWAY_UID = 10001  # starling-gw
DB_GID = 10020  # starling-db
STAGING_PREFIX = ".restore-staging-"


@dataclass(frozen=True)
class RestoreResult:
    target: Path
    files: int
    bytes: int
    schema_version: int
    created_at_utc: str
    moved_aside: Path | None
    ownership_applied: bool


def target_has_data(target: Path) -> bool:
    for name in MANAGED:
        path = target / name
        if path.is_symlink() or path.is_file():
            return True
        if path.is_dir() and any(path.iterdir()):
            return True
    return False


def restore_archive(
    settings: Settings,
    archive: Path,
    target: Path,
    *,
    passphrase: str | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> RestoreResult:
    secret = resolve_passphrase(settings, passphrase, for_writing=False)
    archive, target = Path(archive), Path(target)
    log.warning(RESTORE_WARNING)
    if not archive.is_file():
        raise BackupConfigError(f"archive not found: {archive}")
    if target.exists() and not target.is_dir():
        raise BackupConfigError(f"restore target {target} is not a folder")
    has_data = target_has_data(target)
    if has_data and not force:
        raise BackupConfigError(
            f"restore target {target} already holds Starling data; stop the service and re-run with "
            "--force to move the existing data aside first"
        )
    verified = verify_archive(archive, secret)  # the whole archive, before anything is written
    schema = verified.manifest["schema_version"]
    if schema > migrations.latest_version():
        raise BackupConfigError(
            f"archive schema version {schema} is newer than this release supports "
            f"({migrations.latest_version()}); restore with the Starling version that created it"
        )
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)
    target.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=target))
    moved_aside: Path | None = None
    try:
        extracted = read_archive(archive, secret, extract_to=staging)
        if extracted.manifest["files"] != verified.manifest["files"]:
            raise BackupIntegrityError("the archive changed while it was being restored")
        if has_data:
            moved_aside = _move_aside(target, stamp)
        _place(staging, target, moved_aside)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    applied = False
    if settings.isolation == "os" and os.name == "posix":
        if os.geteuid() == 0:
            apply_ownership(target, settings.engine_uid_base)
            applied = True
        else:
            log.warning("not running as root: owners and modes from CONTRACTS 2.1 were not applied")
    return RestoreResult(
        target=target,
        files=extracted.files,
        bytes=extracted.bytes,
        schema_version=schema,
        created_at_utc=str(verified.manifest.get("created_at_utc", "")),
        moved_aside=moved_aside,
        ownership_applied=applied,
    )


def aside_location(target: Path, stamp: str) -> Path:
    sibling = target.parent / f"{target.name}.pre-restore-{stamp}"
    if os.path.ismount(target) or os.stat(target).st_dev != os.stat(target.parent).st_dev:
        return target / f".pre-restore-{stamp}"
    return sibling


def _move_aside(target: Path, stamp: str) -> Path:
    aside = aside_location(target, stamp)
    aside.mkdir(mode=0o700)
    for name in MANAGED:
        if (target / name).exists() or (target / name).is_symlink():
            os.replace(target / name, aside / name)
    log.warning("existing data moved aside to %s", aside)
    return aside


def _place(staging: Path, target: Path, moved_aside: Path | None) -> None:
    placed: list[str] = []
    try:
        for name in MANAGED:
            destination = target / name
            if destination.is_dir() and not destination.is_symlink():
                destination.rmdir()  # only an empty folder can be here (checked by target_has_data)
            if (staging / name).exists():
                os.replace(staging / name, destination)
                placed.append(name)
    except BaseException:
        for name in placed:  # undo: new data back to staging (discarded), old data back in place
            os.replace(target / name, staging / name)
        if moved_aside is not None:
            for name in MANAGED:
                if (moved_aside / name).exists() and not (target / name).exists():
                    os.replace(moved_aside / name, target / name)
        raise


def ownership_plan(target: Path, engine_uid_base: int) -> list[tuple[Path, int, int, int | None]]:
    """(path, uid, gid, mode or None to keep) for the restored tree, per CONTRACTS section 2.1."""
    plan: list[tuple[Path, int, int, int | None]] = []
    control = target / "control"
    if control.is_dir():
        plan.append((control, GATEWAY_UID, DB_GID, 0o2770))
        for root, dirs, files in os.walk(control):
            plan.extend((Path(root) / d, GATEWAY_UID, DB_GID, 0o2770) for d in sorted(dirs))
            plan.extend((Path(root) / f, GATEWAY_UID, DB_GID, 0o660) for f in sorted(files))
    tenants = target / "tenants"
    if tenants.is_dir():
        plan.append((tenants, 0, 0, 0o711))
        for tenant in sorted(tenants.iterdir()):
            if not (tenant.is_dir() and tenant.name.isdigit()):
                continue
            uid = engine_uid_base + int(tenant.name)
            plan.append((tenant, uid, uid, 0o700))
            for root, dirs, files in os.walk(tenant):
                plan.extend((Path(root) / name, uid, uid, None) for name in sorted(dirs + files))
    return plan


def apply_ownership(target: Path, engine_uid_base: int) -> None:
    for path, uid, gid, mode in ownership_plan(target, engine_uid_base):
        os.lchown(path, uid, gid)
        if mode is not None and not path.is_symlink():  # chmod follows links; lchown above does not
            os.chmod(path, mode)
