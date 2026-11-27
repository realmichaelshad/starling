"""Retention keeps 7 daily, 4 weekly and 3 monthly archives (configurable) and nothing else."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starling_server.ops.retention import archive_name, list_archives, parse_archive_name, plan_retention, prune_archives

UTC = timezone.utc

# Daily archives at 03:00 from 2026-05-01 to 2026-09-14 (a Monday), plus two runs at 15:00 on
# 2026-09-14 (sequence 1 and 2, same second) and one at 15:00 on 2026-09-13.
EXPECTED_KEPT = [
    "starling-backup-20260914T150000Z-2.sbk",  # daily (newest of 09-14), week 38, September
    "starling-backup-20260913T150000Z.sbk",  # daily, week 37
    "starling-backup-20260912T030000Z.sbk",  # daily
    "starling-backup-20260911T030000Z.sbk",  # daily
    "starling-backup-20260910T030000Z.sbk",  # daily
    "starling-backup-20260909T030000Z.sbk",  # daily
    "starling-backup-20260908T030000Z.sbk",  # daily (7th day)
    "starling-backup-20260906T030000Z.sbk",  # weekly: week 36 (Aug 31 to Sep 6)
    "starling-backup-20260831T030000Z.sbk",  # monthly: August
    "starling-backup-20260830T030000Z.sbk",  # weekly: week 35 (Aug 24 to Aug 30)
    "starling-backup-20260731T030000Z.sbk",  # monthly: July
]


def _history(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
    names = [archive_name(start + timedelta(days=day)) for day in range(137)]
    late = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
    names += [archive_name(late), archive_name(late, 2), archive_name(datetime(2026, 9, 13, 15, 0, tzinfo=UTC))]
    for name in names + ["notes.txt", "starling-backup-latest.sbk"]:
        (directory / name).write_bytes(b"archive")


def test_prune_keeps_seven_daily_four_weekly_three_monthly(tmp_path):
    directory = tmp_path / "archives"
    _history(directory)
    assert len(list_archives(directory)) == 140

    plan = prune_archives(directory)

    assert [a.path.name for a in plan.keep] == EXPECTED_KEPT
    assert len(plan.delete) == 140 - len(EXPECTED_KEPT)
    remaining = sorted(p.name for p in directory.iterdir())
    assert remaining == sorted(EXPECTED_KEPT + ["notes.txt", "starling-backup-latest.sbk"])


def test_counts_are_configurable_and_the_newest_archive_always_survives(tmp_path):
    directory = tmp_path / "archives"
    _history(directory)
    two_days = plan_retention(list_archives(directory), keep_daily=2, keep_weekly=0, keep_monthly=0)
    assert [a.path.name for a in two_days.keep] == EXPECTED_KEPT[:2]
    nothing = plan_retention(list_archives(directory), 0, 0, 0)
    assert [a.path.name for a in nothing.keep] == EXPECTED_KEPT[:1]


def test_dry_run_deletes_nothing_and_old_leftovers_are_cleaned(tmp_path):
    directory = tmp_path / "archives"
    _history(directory)
    stale = directory / "starling-backup-20260101T000000Z.sbk.partial"
    fresh = directory / "starling-backup-20260914T030001Z.sbk.partial"
    staging = directory / ".staging-interrupted"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    staging.mkdir()
    (staging / "tenant.db").write_bytes(b"x")
    old = time.time() - 3 * 24 * 3600
    os.utime(stale, (old, old))
    os.utime(staging, (old, old))
    before = sorted(p.name for p in directory.iterdir())

    prune_archives(directory, dry_run=True)
    assert sorted(p.name for p in directory.iterdir()) == before

    prune_archives(directory)
    assert not stale.exists() and not staging.exists() and fresh.exists()


def test_archive_names_round_trip():
    moment = datetime(2026, 9, 14, 3, 0, 5, tzinfo=UTC)
    assert archive_name(moment) == "starling-backup-20260914T030005Z.sbk"
    assert parse_archive_name(archive_name(moment, 3)) == (moment, 3)
    assert parse_archive_name("starling-backup-20261314T030005Z.sbk") is None
    assert parse_archive_name("starling-backup-20260914T030005Z.sbk.partial") is None
