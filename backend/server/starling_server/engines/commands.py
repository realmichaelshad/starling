"""Engine operator commands: `engines-status` and `engines-stop --account <id>`.

Both work through the control database, so they need no connection to the gateway process:
`engines-stop` marks the account's engine row `stop_requested`, and the gateway's engine pool
stops that engine at its next maintenance tick and marks the row `stopped`. With OS isolation
`engines-status` also asks the supervisor for live processes. Run these as the gateway user
(`starling-gw`) inside the container so database files keep their owner.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

from ..config import get_settings
from ..db.connection import connect


def _when(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(int(value), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _supervisor_status(settings) -> dict:
    from .spawner import SpawnError
    from .supervisor_client import SupervisorClient

    try:
        engines = asyncio.run(SupervisorClient(settings).status())
    except SpawnError as exc:
        return {"error": str(exc)}
    return {"engines": [item.__dict__ for item in engines]}


def _status(args: argparse.Namespace) -> int:
    settings = get_settings()
    path = settings.control_db_path
    if not path.exists():
        print(f"control database not found: {path}")
        return 1
    conn = connect(path)
    try:
        rows = [dict(row) for row in conn.execute(
            "SELECT account_id, tenant_seq, pid, state, started_at, last_used_at, rss_mb, restarts "
            "FROM engines ORDER BY tenant_seq")]
    finally:
        conn.close()
    supervisor = _supervisor_status(settings) if settings.isolation == "os" and not args.no_supervisor else None
    if args.json:
        print(json.dumps({"engines": rows, "supervisor": supervisor}, indent=2))
        return 0
    if not rows:
        print("no engines recorded")
    else:
        print(f"{'tenant':>8}  {'state':<14}  {'pid':>7}  {'restarts':>8}  {'started':<20}  {'last used':<20}  account")
        for row in rows:
            print(f"{row['tenant_seq']:>8}  {row['state']:<14}  {row['pid'] or '-':>7}  {row['restarts']:>8}  "
                  f"{_when(row['started_at']):<20}  {_when(row['last_used_at']):<20}  {row['account_id']}")
    if supervisor is not None:
        if "error" in supervisor:
            print(f"supervisor: {supervisor['error']}")
        else:
            print(f"supervisor: {len(supervisor['engines'])} engine processes")
            for item in supervisor["engines"]:
                print(f"  tenant {item['tenant_seq']}: pid {item['pid']}, rss {item['rss_mb']} MB")
    return 0


def _stop(args: argparse.Namespace) -> int:
    try:
        account_id = str(uuid.UUID(args.account))
    except ValueError:
        print("--account must be an account id (UUID)")
        return 2
    settings = get_settings()
    if not settings.control_db_path.exists():
        print(f"control database not found: {settings.control_db_path}")
        return 1
    conn = connect(settings.control_db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT tenant_seq, state FROM engines WHERE account_id = ?", (account_id,)).fetchone()
            if row is None or row["state"] in ("stopped", "crashed"):
                conn.execute("ROLLBACK")
                print("no running engine is recorded for that account")
                return 0
            conn.execute("UPDATE engines SET state = 'stop_requested' WHERE account_id = ?", (account_id,))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        seq = row["tenant_seq"]
        deadline = time.monotonic() + max(0.0, args.wait)
        while True:
            current = conn.execute("SELECT state FROM engines WHERE account_id = ?", (account_id,)).fetchone()
            if current is None or current["state"] != "stop_requested":
                print(f"engine {seq} stopped")
                return 0
            if time.monotonic() >= deadline:
                print(f"stop requested for engine {seq}; the gateway has not applied it yet (is the gateway running?)")
                return 3
            time.sleep(0.5)
    finally:
        conn.close()


def register(subparsers: argparse._SubParsersAction) -> None:
    status = subparsers.add_parser("engines-status", help="list customer engines and their state")
    status.add_argument("--json", action="store_true", help="print JSON")
    status.add_argument("--no-supervisor", action="store_true", help="do not ask the supervisor for live processes")
    status.set_defaults(func=_status)
    stop = subparsers.add_parser("engines-stop", help="stop one customer's engine (the gateway applies it)")
    stop.add_argument("--account", required=True, help="account id (UUID)")
    stop.add_argument("--wait", type=float, default=30.0, help="seconds to wait for the gateway (default 30)")
    stop.set_defaults(func=_stop)
