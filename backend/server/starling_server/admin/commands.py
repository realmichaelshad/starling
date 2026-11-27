"""Operator commands: `ai-pause`, `ai-resume` and `alerts` (`python -m starling_server.cli ...`).

`ai-pause` and `ai-resume` write and clear `service_state` `ai_paused` (CONTRACTS 11) exactly as the owner
dashboard does, each with one `admin_audit` row whose admin is "cli". `alerts` prints the owner alerts the
dashboard shows. `ai-check` and `services-status` live in `services.commands`.
"""
from __future__ import annotations

import argparse
import json
import sys

from ..config import Settings, get_settings
from ..db import Database
from ..security import clock
from . import actions, alerts
from .common import format_time


def _database(settings: Settings) -> Database:
    settings.control_db_path.parent.mkdir(parents=True, exist_ok=True)
    return Database(settings.control_db_path)


def _ai_pause(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = _database(settings)
    try:
        result = actions.pause_ai(db, actions.CLI_ACTOR, args.reason, args.message, clock.now(), args.retry_after_s)
    finally:
        db.close()
    if not result.done:
        print(
            "ai-pause: refused: give a --reason of 1 to 200 characters, a --message of at most 200 and a "
            f"--retry-after-s from 1 to {actions.MAX_PAUSE_RETRY_AFTER_S}",
            file=sys.stderr,
        )
        return 1
    print("ai-pause: drafting is paused for all customers; run ai-resume to undo it")
    return 0


def _ai_resume(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = _database(settings)
    try:
        result = actions.resume_ai(db, actions.CLI_ACTOR, args.reason, clock.now())
    finally:
        db.close()
    if result.outcome == "invalid":
        print("ai-resume: refused: keep --reason to at most 200 characters", file=sys.stderr)
        return 1
    print("ai-resume: drafting is running again" if result.done else "ai-resume: drafting was not paused")
    return 0


def _alerts(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = _database(settings)
    try:
        with db.read() as conn:
            found = alerts.collect(conn, settings, clock.now(), recent_s=max(1, args.days) * 86_400)
    finally:
        db.close()
    if args.json:
        print(json.dumps(found, indent=2, sort_keys=True))
    elif not found:
        print("No owner alerts.")
    else:
        for item in found:
            print(f"{format_time(item['created_at'])}  {item['level'].upper():<7}  {item['text']}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    pause = subparsers.add_parser("ai-pause", help="pause drafting for all customers (service_state ai_paused)")
    pause.add_argument("--reason", required=True, help="why, for the audit log")
    pause.add_argument("--message", default="", help="what customers see (default: a standard short notice)")
    pause.add_argument("--retry-after-s", type=int, default=None, dest="retry_after_s",
                       help=f"Retry-After on refusals, 1 to {actions.MAX_PAUSE_RETRY_AFTER_S} seconds "
                            f"(default: {actions.PAUSE_RETRY_AFTER_S})")
    pause.set_defaults(func=_ai_pause)
    resume = subparsers.add_parser("ai-resume", help="resume drafting after ai-pause")
    resume.add_argument("--reason", default="", help="optional note for the audit log")
    resume.set_defaults(func=_ai_resume)
    listing = subparsers.add_parser("alerts", help="list owner alerts: billing, engines, AI keys and backups")
    listing.add_argument("--days", type=int, default=7, help="include stored alerts that fired within this many days")
    listing.add_argument("--json", action="store_true", help="print the alerts as JSON")
    listing.set_defaults(func=_alerts)
