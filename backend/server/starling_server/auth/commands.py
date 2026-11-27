"""Operator commands: `admin-grant --email X` and `admin-revoke --email X`."""
from __future__ import annotations

import argparse
import sys
from typing import Callable

from ..config import get_settings
from ..db import Database
from .admin import AdminError, grant_admin, revoke_admin


def _run(action: Callable, args: argparse.Namespace) -> int:
    settings = get_settings()
    db = Database(settings.control_db_path)
    try:
        account = action(db, settings, args.email)
    except AdminError as exc:
        print(f"{args.command}: refused: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
    print(f"{args.command}: done for account {account.id}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    grant = subparsers.add_parser(
        "admin-grant",
        help="give owner admin access to an active, verified account listed in STARLING_OWNER_EMAILS",
    )
    grant.add_argument("--email", required=True, help="the account's sign-in email")
    grant.set_defaults(func=lambda args: _run(grant_admin, args))

    revoke = subparsers.add_parser(
        "admin-revoke", help="remove owner admin access, the authenticator enrollment and any elevation"
    )
    revoke.add_argument("--email", required=True, help="the account's sign-in email")
    revoke.set_defaults(func=lambda args: _run(revoke_admin, args))
