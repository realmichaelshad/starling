"""Operator commands: `backup`, `backup-verify`, `restore`, `backup-prune`, `backup-status` and
`proxy-peers`.

The passphrase comes from `--passphrase-file`, else `BACKUP_PASSPHRASE`, else (verify and restore
only, on an interactive terminal) a prompt. It is never accepted as a command-line argument,
which would leave it in shell history and process listings.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import Settings, get_settings
from . import netpeers
from .archive import create_backup, verify_archive
from .common import BackupError, exclusive_lock, read_passphrase_file, resolve_passphrase
from .restore import RESTORE_WARNING, restore_archive
from .retention import DEFAULT_KEEP_DAILY, DEFAULT_KEEP_MONTHLY, DEFAULT_KEEP_WEEKLY, list_archives, prune_archives
from .status import read_status


def _passphrase(args: argparse.Namespace, settings: Settings, *, for_writing: bool) -> str:
    if args.passphrase_file is not None:
        value: str | None = read_passphrase_file(args.passphrase_file)
    elif settings.backup_passphrase:
        value = settings.backup_passphrase
    elif not for_writing and sys.stdin.isatty():
        value = getpass.getpass("Backup passphrase: ")
    else:
        value = None
    return resolve_passphrase(settings, value, for_writing=for_writing)


def _handled(fn: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except BackupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code

    return run


def _utc(seconds: object) -> str:
    if not isinstance(seconds, (int, float)):
        return "an unknown time"
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _backup(args: argparse.Namespace) -> int:
    settings = get_settings()
    result = create_backup(
        settings,
        passphrase=_passphrase(args, settings, for_writing=True),
        output_dir=args.output_dir,
        verify=not args.no_verify,
    )
    print(f"archive:  {result.path}")
    print(f"contents: {result.files} files, {result.bytes:,} bytes; archive size {result.archive_bytes:,} bytes")
    print(f"schema:   {result.schema_version}")
    print(f"verified: {'no (--no-verify)' if args.no_verify else 'yes'}")
    for item in result.skipped[:20]:
        print(f"skipped:  {item['path']} ({item['reason']})")
    if len(result.skipped) > 20:
        print(f"skipped:  ... and {len(result.skipped) - 20} more (listed in the archive manifest)")
    print("Copy the archive off this machine: a backup on the same volume does not survive losing it.")
    return 0


def _verify(args: argparse.Namespace) -> int:
    settings = get_settings()
    result = verify_archive(args.archive, _passphrase(args, settings, for_writing=False))
    manifest = result.manifest
    print(
        f"OK: {args.archive} is intact ({result.files} files, {result.bytes:,} bytes; created "
        f"{manifest.get('created_at_utc', '?')}, app {manifest.get('app_version', '?')}, "
        f"schema {manifest['schema_version']})"
    )
    return 0


def _restore(args: argparse.Namespace) -> int:
    settings = get_settings()
    print(RESTORE_WARNING, file=sys.stderr)
    result = restore_archive(
        settings,
        args.archive,
        args.target,
        passphrase=_passphrase(args, settings, for_writing=False),
        force=args.force,
    )
    print(f"restored: {result.files} files, {result.bytes:,} bytes into {result.target}")
    print(f"archive created {result.created_at_utc}, schema {result.schema_version}")
    if result.moved_aside is not None:
        print(f"previous data moved aside to {result.moved_aside} (delete it once the restore is confirmed)")
    if settings.isolation == "os":
        print(f"owners and modes applied: {'yes' if result.ownership_applied else 'NO (run as root)'}")
    print("Start the service again. Stripe changes made after the archive was created are picked up by the reconciler.")
    return 0


def _prune(args: argparse.Namespace) -> int:
    settings = get_settings()
    directory = args.dir or settings.backup_dir
    with exclusive_lock(directory):
        plan = prune_archives(directory, args.keep_daily, args.keep_weekly, args.keep_monthly, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    for archive in plan.delete:
        print(f"{verb}: {archive.path.name}")
    print(f"kept {len(plan.keep)} archive(s), {verb} {len(plan.delete)}")
    return 0


def _status(args: argparse.Namespace) -> int:
    """Exit status 1 when the last scheduled run failed, so monitoring can alert on it."""
    settings = get_settings()
    last = read_status(settings)
    if last is None:
        print("last scheduled run: none recorded")
    elif last.get("ok"):
        size = last.get("size_bytes")
        size_text = f"{size:,} bytes" if isinstance(size, int) else "size unknown"
        print(
            f"last scheduled run: ok at {_utc(last.get('at'))}, {last.get('archive')} ({size_text}), "
            f"verified: {'yes' if last.get('verified') else 'no'}"
        )
    else:
        print(f"last scheduled run: FAILED at {_utc(last.get('at'))}: {last.get('error') or 'unknown error'}")
    archives = list_archives(settings.backup_dir)
    print(f"archives in {settings.backup_dir}: {len(archives)}")
    for archive in archives[: args.limit]:
        print(f"  {archive.path.name}")
    return 1 if last is not None and not last.get("ok") else 0


def _proxy_peers(args: argparse.Namespace) -> int:
    if not netpeers.PROC_FILES[0].exists():
        print("error: proxy-peers reads /proc/net/tcp and works only on Linux (run it in the container)", file=sys.stderr)
        return 2
    peers = netpeers.established_peers(args.port)
    if not peers:
        print(f"no established connections to port {args.port}; send a few requests to the site, then run it again")
        return 0
    print(f"established connections to port {args.port}, by peer address:")
    for address, count in peers.most_common():
        print(f"  {address}  {count}")
    print("See deploy/README.md (client IP addresses) before changing STARLING_TRUSTED_PROXIES.")
    return 0


def _count(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def register(subparsers: argparse._SubParsersAction) -> None:
    backup = subparsers.add_parser("backup", help="write an encrypted, verified archive of all data")
    backup.add_argument("--output-dir", type=Path, help="folder for the archive (default: BACKUP_DIR)")
    backup.add_argument("--passphrase-file", type=Path, help="read the passphrase from this file")
    backup.add_argument("--no-verify", action="store_true", help="skip reading the archive back")
    backup.set_defaults(func=_handled(_backup))

    verify = subparsers.add_parser("backup-verify", help="decrypt an archive and check every checksum")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--passphrase-file", type=Path, help="read the passphrase from this file")
    verify.set_defaults(func=_handled(_verify))

    restore = subparsers.add_parser("restore", help="restore an archive (stop the service first)")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True, help="data folder, normally STARLING_DATA_DIR")
    restore.add_argument("--force", action="store_true", help="move existing data aside instead of refusing")
    restore.add_argument("--passphrase-file", type=Path, help="read the passphrase from this file")
    restore.set_defaults(func=_handled(_restore))

    prune = subparsers.add_parser("backup-prune", help="delete archives outside the retention rules")
    prune.add_argument("--dir", type=Path, help="archive folder (default: BACKUP_DIR)")
    prune.add_argument("--keep-daily", type=_count, default=DEFAULT_KEEP_DAILY)
    prune.add_argument("--keep-weekly", type=_count, default=DEFAULT_KEEP_WEEKLY)
    prune.add_argument("--keep-monthly", type=_count, default=DEFAULT_KEEP_MONTHLY)
    prune.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    prune.set_defaults(func=_handled(_prune))

    status = subparsers.add_parser("backup-status", help="show the last scheduled backup run and the newest archives")
    status.add_argument("--limit", type=_count, default=5, help="how many archives to list (default 5)")
    status.set_defaults(func=_handled(_status))

    peers = subparsers.add_parser("proxy-peers", help="list the addresses connected to the gateway port (Linux)")
    peers.add_argument("--port", type=int, default=8080)
    peers.set_defaults(func=_handled(_proxy_peers))
