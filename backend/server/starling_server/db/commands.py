"""Database commands: `migrate` and `db-status`."""
from __future__ import annotations

import argparse

from ..config import get_settings
from . import migrations
from .connection import connect


def _migrate(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.control_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.control_db_path)
    try:
        before = migrations.current_version(conn)
        after = migrations.apply(conn)
    finally:
        conn.close()
    print(f"control database {settings.control_db_path}: schema {before} -> {after}")
    return 0


def _status(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.control_db_path.exists():
        print(f"control database not found: {settings.control_db_path}")
        return 1
    conn = connect(settings.control_db_path)
    try:
        version = migrations.current_version(conn)
    finally:
        conn.close()
    latest = migrations.latest_version()
    print(f"schema version {version} (latest {latest})")
    return 0 if version == latest else 2


def register(subparsers: argparse._SubParsersAction) -> None:
    migrate = subparsers.add_parser("migrate", help="apply pending control database migrations")
    migrate.set_defaults(func=_migrate)
    status = subparsers.add_parser("db-status", help="show the control database schema version")
    status.set_defaults(func=_status)
