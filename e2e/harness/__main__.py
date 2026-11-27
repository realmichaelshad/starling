"""`python -m e2e.harness up|down|status` (run from the repository root).

`up` starts everything and stays in the foreground until Ctrl-C or `down` from another shell,
so the state file always describes processes that are really running. `--detach` returns
instead, leaving the stack up for a browser session or a separate pytest run.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from .paths import DEFAULT_STATE_FILE
from .stack import Stack, attach, stop_from_state
from .state import read_state


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _up(args: argparse.Namespace) -> int:
    state_file = Path(args.state)
    running = attach(state_file)
    if running is not None:
        print(f"a harness is already running for {running.gateway_url} (state: {state_file})", file=sys.stderr)
        return 1
    stack = Stack(
        state_file=state_file,
        gateway_port=getattr(args, "gateway_port", None),
        extension_id=getattr(args, "extension_id", None),
    )
    state = stack.up()
    print(json.dumps(state.as_dict(), indent=2, sort_keys=True))
    if args.detach:
        return 0
    stopping = threading.Event()

    def handle(signum, frame) -> None:  # noqa: ANN001
        stopping.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                pass
    print("harness is up; press Ctrl-C to stop", file=sys.stderr)
    try:
        while not stopping.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    print(json.dumps(stack.down(), indent=2, sort_keys=True), file=sys.stderr)
    return 0


def _down(args: argparse.Namespace) -> int:
    state_file = Path(args.state)
    report = stop_from_state(state_file)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _status(args: argparse.Namespace) -> int:
    state_file = Path(args.state)
    state = read_state(state_file)
    if state is None:
        print(json.dumps({"running": False, "reason": "no state file"}, indent=2))
        return 1
    live = attach(state_file)
    print(json.dumps({"running": live is not None, "state": state.as_dict()}, indent=2, sort_keys=True))
    return 0 if live is not None else 1


def build_parser() -> argparse.ArgumentParser:
    # `--state` and `-v` work before or after the command, because both spellings read naturally
    # (`up --state e2e/.run/state.json` is the one in the README).
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--state", default=argparse.SUPPRESS, help="where to write the run's state file")
    shared.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(prog="e2e.harness", description=__doc__, parents=[shared])
    parser.set_defaults(state=str(DEFAULT_STATE_FILE), verbose=False)
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", parents=[shared], help="start the fakes, the services process and the gateway")
    up.add_argument("--detach", action="store_true", help="return once everything is ready instead of waiting")
    up.add_argument(
        "--gateway-port", type=int, default=None,
        help="pin the gateway's port (a browser run needs the origin the extension was built against)",
    )
    up.add_argument(
        "--extension-id", default=None,
        help="the extension id the gateway trusts (a browser run passes the unpacked extension's own id)",
    )
    up.set_defaults(func=_up)
    down = commands.add_parser("down", parents=[shared], help="stop everything named by the state file, engines included")
    down.set_defaults(func=_down)
    status = commands.add_parser("status", parents=[shared], help="print the state file and whether the gateway answers")
    status.set_defaults(func=_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
