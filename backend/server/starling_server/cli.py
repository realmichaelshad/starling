"""Operator command line: `python -m starling_server.cli <command> ...`.

Commands are contributed by modules named `starling_server.<package>.commands`; each
exposes `register(subparsers)` and sets `func` on its parsers. This keeps every
workstream's commands in its own package (CONTRACTS section 14).
"""
from __future__ import annotations

import argparse
import importlib
import sys

COMMAND_MODULES: tuple[str, ...] = (
    "db.commands",
    "auth.commands",
    "billing.commands",
    "usage.commands",
    "services.commands",
    "engines.commands",
    "ops.commands",
    "admin.commands",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="starling", description="Starling operator commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in COMMAND_MODULES:
        target = f"starling_server.{name}"
        try:
            module = importlib.import_module(target)
        except ModuleNotFoundError as exc:
            # Skip only when the command module itself (or its package) does not exist yet;
            # a missing dependency inside an existing module is a real error.
            if exc.name and (target == exc.name or target.startswith(exc.name + ".")):
                continue
            raise
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
