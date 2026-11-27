"""Helper process for SQLite work that must run as the file owner (see runas.py).

    python -m starling_server.ops.helper snapshot DATABASE          snapshot bytes on stdout
    python -m starling_server.ops.helper state-write DATABASE KEY   JSON object on stdin
    python -m starling_server.ops.helper state-read DATABASE KEY    JSON value (or null) on stdout

Exit status: 0 success; 3 (`snapshot` only) SQLite could not read the database, so the raw file
bytes were sent instead; 1 failure; 2 usage. Problems are reported as one line on stderr.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from .runas import EXIT_RAW_COPY
from .sqlite_io import read_state, snapshot_sqlite, write_state

CHUNK = 1 << 20
USAGE = "usage: python -m starling_server.ops.helper snapshot DB | state-write DB KEY | state-read DB KEY"


def _stream(path: Path, out) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with open(fd, "rb") as handle:
        shutil.copyfileobj(handle, out, CHUNK)


def _snapshot(database: Path) -> int:
    out = sys.stdout.buffer
    with tempfile.TemporaryDirectory(prefix="starling-snapshot-") as work:
        target = Path(work) / "snapshot.db"
        try:
            check = snapshot_sqlite(database, target)
        except sqlite3.DatabaseError as exc:
            print(f"SQLite could not read the database, raw file sent: {exc}", file=sys.stderr)
            _stream(database, out)
            out.flush()
            return EXIT_RAW_COPY
        if check != "ok":
            print(f"quick_check: {check}", file=sys.stderr)
        _stream(target, out)
    out.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) == 2 and args[0] == "snapshot":
            return _snapshot(Path(args[1]))
        if len(args) == 3 and args[0] == "state-write":
            value = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("the state value must be a JSON object")
            write_state(Path(args[1]), args[2], value, int(value.get("at", 0)))
            return 0
        if len(args) == 3 and args[0] == "state-read":
            sys.stdout.write(json.dumps(read_state(Path(args[1]), args[2])))
            return 0
    except Exception as exc:  # reported to the parent as one line
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
