"""Usage commands: `usage-verify`."""
from __future__ import annotations

import argparse
import json

from ..config import get_settings
from ..db import Database
from . import service


def _verify(args: argparse.Namespace) -> int:
    """Print every disagreement between the usage counters, the ledger and the reservations.

    Exit status 0 when they agree, 1 when they do not, 2 when there is no database to check. The
    running gateway makes the same check daily and raises an owner alert; this is the full list.
    """
    settings = get_settings()
    if not settings.control_db_path.exists():
        print(f"control database not found: {settings.control_db_path}")
        return 2
    db = Database(settings.control_db_path)
    try:
        problems = service.verify_counters(db)
    finally:
        db.close()
    for problem in problems:
        print(json.dumps(problem, sort_keys=True))
    print(f"usage counters: {len(problems)} problem(s)")
    return 1 if problems else 0


def register(subparsers: argparse._SubParsersAction) -> None:
    verify = subparsers.add_parser(
        "usage-verify", help="check the usage counters against the credit ledger and the reservations"
    )
    verify.set_defaults(func=_verify)
