"""Archive names and retention: keep the newest archive of each of the last N days, weeks and months."""
from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

ARCHIVE_PREFIX = "starling-backup-"
ARCHIVE_SUFFIX = ".sbk"
PARTIAL_SUFFIX = ".partial"
STAGING_PREFIX = ".staging-"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4
DEFAULT_KEEP_MONTHLY = 3
LEFTOVER_MAX_AGE_S = 24 * 3600

_NAME = re.compile(r"^starling-backup-(\d{8}T\d{6}Z)(?:-(\d+))?\.sbk$")


@dataclass(frozen=True)
class ArchiveFile:
    path: Path
    created_at: datetime
    sequence: int


@dataclass(frozen=True)
class PrunePlan:
    keep: list[ArchiveFile]
    delete: list[ArchiveFile]


def archive_name(created_at: datetime, sequence: int = 1) -> str:
    stamp = created_at.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)
    return f"{ARCHIVE_PREFIX}{stamp}{'' if sequence <= 1 else f'-{sequence}'}{ARCHIVE_SUFFIX}"


def parse_archive_name(name: str) -> tuple[datetime, int] | None:
    match = _NAME.match(name)
    if not match:
        return None
    try:
        created = datetime.strptime(match.group(1), TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return created, int(match.group(2) or 1)


def list_archives(directory: Path) -> list[ArchiveFile]:
    """Archives in `directory`, newest first. Other files are ignored."""
    if not directory.is_dir():
        return []
    found = []
    for entry in directory.iterdir():
        parsed = parse_archive_name(entry.name)
        if parsed and entry.is_file() and not entry.is_symlink():
            found.append(ArchiveFile(entry, parsed[0], parsed[1]))
    return sorted(found, key=lambda a: (a.created_at, a.sequence), reverse=True)


def _week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year:04d}-W{week:02d}"


def plan_retention(
    archives: Iterable[ArchiveFile],
    keep_daily: int = DEFAULT_KEEP_DAILY,
    keep_weekly: int = DEFAULT_KEEP_WEEKLY,
    keep_monthly: int = DEFAULT_KEEP_MONTHLY,
) -> PrunePlan:
    """For each rule keep the newest archive of each of the most recent N periods that have one.

    The newest archive overall is always kept, whatever the rules say.
    """
    if min(keep_daily, keep_weekly, keep_monthly) < 0:
        raise ValueError("retention counts must not be negative")
    ordered = sorted(archives, key=lambda a: (a.created_at, a.sequence), reverse=True)
    rules: tuple[tuple[int, Callable[[datetime], str]], ...] = (
        (keep_daily, lambda d: d.strftime("%Y-%m-%d")),
        (keep_weekly, _week),
        (keep_monthly, lambda d: d.strftime("%Y-%m")),
    )
    kept: set[Path] = set()
    for count, period_of in rules:
        periods: set[str] = set()
        for archive in ordered:
            if len(periods) >= count:
                break
            period = period_of(archive.created_at)
            if period not in periods:
                periods.add(period)
                kept.add(archive.path)
    if ordered:
        kept.add(ordered[0].path)
    return PrunePlan(
        keep=[a for a in ordered if a.path in kept],
        delete=[a for a in ordered if a.path not in kept],
    )


def prune_archives(
    directory: Path,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    keep_weekly: int = DEFAULT_KEEP_WEEKLY,
    keep_monthly: int = DEFAULT_KEEP_MONTHLY,
    *,
    dry_run: bool = False,
    now: float | None = None,
) -> PrunePlan:
    """Delete archives outside the retention rules and leftovers of interrupted runs.

    Callers hold `common.exclusive_lock(directory)` so an archive being written is never touched.
    """
    plan = plan_retention(list_archives(directory), keep_daily, keep_weekly, keep_monthly)
    if dry_run:
        return plan
    for archive in plan.delete:
        archive.path.unlink(missing_ok=True)
    cutoff = (time.time() if now is None else now) - LEFTOVER_MAX_AGE_S
    for entry in directory.iterdir() if directory.is_dir() else ():
        leftover = (entry.name.startswith(ARCHIVE_PREFIX) and entry.name.endswith(PARTIAL_SUFFIX)) or (
            entry.name.startswith(STAGING_PREFIX)
        )
        if not leftover or entry.is_symlink() or entry.stat().st_mtime > cutoff:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    return plan
