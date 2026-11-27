"""The harness, owned by a process that dies with the test run.

`run.mjs` starts this with a pipe on its standard input and holds the pipe open for as long as
the journeys are running. When the run ends - normally, by a crash, or because someone killed
Node - the pipe closes, `wait_for_stdin_close` returns and the stack is stopped. Node cannot
send CTRL_BREAK to a child on Windows and `taskkill /F` gives a process no chance to stop its
own children, so a closing pipe is the only shutdown signal that survives every ending.

While it runs it also answers the few operator actions a browser journey needs and cannot
perform through a browser: restarting the gateway with a different minimum extension version,
restarting the services process, an admin credit debit, the operator CLI (`admin-grant`,
`ai-pause`, `ai-resume`) and read-only queries against the control database.

    backend\\.venv\\Scripts\\python.exe e2e\\browser\\tools\\stack_driver.py --gateway-port 8765 ...

prints one `READY <json>` line with the control address, its token and the harness state.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from e2e.harness import env as environment  # noqa: E402
from e2e.harness.paths import SERVER_DIR, venv_python  # noqa: E402
from e2e.harness.stack import Stack  # noqa: E402

TOKEN_HEADER = "X-Starling-E2E"


class Driver:
    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()

    # ---- actions ---------------------------------------------------------

    def state(self) -> dict:
        assert self.stack.state is not None
        return self.stack.state.as_dict()

    def restart_gateway(self, overrides: dict) -> dict:
        with self.lock:
            return {"gateway_url": self.stack.restart_gateway({k: str(v) for k, v in overrides.items()})}

    def restart_services(self, overrides: dict) -> dict:
        with self.lock:
            return {"services_url": self.stack.restart_services({k: str(v) for k, v in overrides.items()})}

    def credits(self, payload: dict) -> dict:
        """The admin credit grant/debit, run in this process against the same database.

        The free plan allows 3 requests a minute, so a journey cannot reach the monthly
        allowance by drafting: it debits the allowance down to the last draft and takes that
        one in the browser.
        """
        from e2e.tests.client import Control

        control = Control(self.state())
        try:
            operation = payload.get("op", "debit")
            amount = int(payload["amount"])
            account_id = str(payload["account_id"])
            reason = str(payload.get("reason", "e2e browser journey"))
            if operation == "grant":
                return {"result": control.grant_credits(account_id, amount, reason)}
            return {"result": control.debit_credits(account_id, amount, reason)}
        finally:
            control.close()

    def cli(self, payload: dict) -> dict:
        """`python -m starling_server.cli ...` against this run's database and settings."""
        args = [str(part) for part in payload.get("args", [])]
        completed = subprocess.run(
            [venv_python(), "-m", "starling_server.cli", *args],
            cwd=SERVER_DIR,
            env=environment.process_environment(self.stack.env),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {"code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}

    def sql(self, payload: dict) -> dict:
        """A read-only query, for the few facts a browser cannot see (an account's id)."""
        conn = sqlite3.connect(f"file:{Path(self.stack.control_db).as_posix()}?mode=ro", uri=True, timeout=20)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(str(payload["sql"]), tuple(payload.get("params", []))).fetchall()
            return {"rows": [dict(row) for row in rows]}
        finally:
            conn.close()


class Handler(BaseHTTPRequestHandler):
    driver: Driver

    def log_message(self, *_args) -> None:  # noqa: ANN002 - quiet: the run's output is the tests'
        return

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorised(self) -> bool:
        if self.headers.get(TOKEN_HEADER) == self.driver.token:
            return True
        self._send(403, {"error": "wrong or missing control token"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorised():
            return
        if self.path == "/state":
            return self._send(200, self.driver.state())
        if self.path == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"error": f"no action {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorised():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "the body was not JSON"})
        actions = {
            "/restart-gateway": lambda: self.driver.restart_gateway(payload.get("overrides", {})),
            "/restart-services": lambda: self.driver.restart_services(payload.get("overrides", {})),
            "/credits": lambda: self.driver.credits(payload),
            "/cli": lambda: self.driver.cli(payload),
            "/sql": lambda: self.driver.sql(payload),
        }
        action = actions.get(self.path)
        if action is None:
            return self._send(404, {"error": f"no action {self.path}"})
        try:
            self._send(200, action())
        except Exception as error:  # noqa: BLE001 - the journey must see why, not a dead socket
            self._send(500, {"error": f"{type(error).__name__}: {error}"})


def wait_for_stdin_close() -> None:
    """Return when the parent closes the pipe (or ends), whatever killed it."""
    while True:
        chunk = sys.stdin.readline()
        if not chunk:
            return
        if chunk.strip() == "down":
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stack_driver", description=__doc__)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--extension-id", required=True)
    parser.add_argument("--state", required=True, help="where to write the harness state file")
    args = parser.parse_args(argv)

    stack = Stack(
        state_file=Path(args.state),
        gateway_port=args.gateway_port,
        extension_id=args.extension_id,
    )
    stack.up()
    driver = Driver(stack)
    Handler.driver = driver
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(
        "READY " + json.dumps({
            "control_url": f"http://127.0.0.1:{server.server_address[1]}",
            "control_token": driver.token,
            "state": driver.state(),
        }),
        flush=True,
    )
    try:
        wait_for_stdin_close()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        report = stack.down()
        print("STOPPED " + json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
