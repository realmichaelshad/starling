"""Hosted engine worker: one process per active customer (CONTRACTS §3.1).

The supervisor starts `python -m app.worker` with working directory
`backend/engine` and this sequence follows:

1. exactly one JSON line of secrets arrives on stdin, then EOF:
   `{"worker_token", "services_token", "tenant_id"}`;
2. the tokens are kept in memory only: never in `os.environ`, never on disk;
3. the listener named by `STARLING_LISTEN` is bound
   (`unix:/run/starling/engines/<seq>/engine.sock` or `tcp:127.0.0.1:0`);
4. once the application has started, one line goes to stdout:
   `READY {"listen": "...", "pid": <int>, "engine_version": "<semver>"}`;
5. after READY, stdout carries JSON log lines only;
6. SIGTERM (CTRL_BREAK on Windows development machines) stops accepting new
   requests, cancels in-flight work, waits at most 10 seconds, checkpoints the
   SQLite write-ahead log and exits 0.
"""
from __future__ import annotations

import asyncio
import contextvars
import io
import json
import logging
import os
import re
import socket
import stat
import sys
import threading
import time
import uuid
from typing import Any, NoReturn, TextIO

READY_TIMEOUT_S = 30.0
GRACE_S = 10.0
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
# CONTRACTS §4.3: svc1.<subject>.<expires_at>.<nonce>.<signature>. The engine never
# holds the signing key, so it cannot verify the signature; it checks the shape,
# that the token names THIS tenant, and that it has not already expired, so a
# mis-wired spawn fails loudly at start instead of on the first AI call.
_SERVICES_TOKEN_RE = re.compile(
    r"^svc1\.t-(?P<tenant>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.(?P<expires>[0-9]{1,12})\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}$")
MAX_SERVICES_TOKEN_LENGTH = 256
_REAL_STDOUT = sys.stdout


def _fail(message: str, code: int = 2) -> NoReturn:
    sys.stderr.write(json.dumps({"ts": round(time.time(), 3), "level": "error",
                                 "event": "worker_start_failed", "message": message}) + "\n")
    sys.stderr.flush()
    raise SystemExit(code)


def read_secrets(stream: TextIO) -> dict[str, str]:
    """Exactly one JSON line, then EOF. Anything else refuses to start."""
    line = stream.readline()
    rest = stream.read()
    if not line.strip() or rest.strip():
        _fail("stdin must carry exactly one JSON line followed by EOF")
    try:
        data = json.loads(line)
    except ValueError:
        _fail("the stdin line is not valid JSON")
    if not isinstance(data, dict):
        _fail("the stdin line must be a JSON object")
    worker = data.get("worker_token")
    if not isinstance(worker, str) or not _TOKEN_RE.match(worker):
        _fail("worker_token must be 43 base64url characters")
    tenant = data.get("tenant_id")
    try:
        parsed = uuid.UUID(str(tenant))
    except ValueError:
        _fail("tenant_id must be a UUID")
    if not isinstance(tenant, str) or str(parsed) != tenant:
        _fail("tenant_id must be a canonical lowercase UUID string")
    services = data.get("services_token")
    match = (_SERVICES_TOKEN_RE.match(services)
             if isinstance(services, str) and len(services) <= MAX_SERVICES_TOKEN_LENGTH else None)
    if match is None:
        _fail("services_token must be a signed services token for this tenant (CONTRACTS 4.3)")
    if match.group("tenant") != tenant:
        _fail("services_token was issued for another tenant")
    if int(match.group("expires")) <= int(time.time()):
        _fail("services_token has already expired")
    return {"worker_token": worker, "services_token": services, "tenant_id": tenant}


def parse_listen(value: str) -> tuple[str, Any]:
    value = (value or "").strip()
    if value.startswith("unix:"):
        path = value[len("unix:"):]
        if not path:
            _fail("STARLING_LISTEN unix: needs a socket path")
        return "unix", path
    if value.startswith("tcp:"):
        host, _, port_text = value[len("tcp:"):].rpartition(":")
        if host != "127.0.0.1":
            _fail("a tcp listener must bind 127.0.0.1")
        try:
            port = int(port_text)
        except ValueError:
            _fail("STARLING_LISTEN tcp: needs a numeric port")
        if not 0 <= port <= 65535:
            _fail("STARLING_LISTEN port is out of range")
        return "tcp", (host, port)
    _fail("STARLING_LISTEN must start with unix: or tcp:")


def bind_listener(kind: str, target: Any) -> tuple[socket.socket, str]:
    if kind == "unix":
        if not hasattr(socket, "AF_UNIX"):
            _fail("unix listeners are not available on this platform")
        path = str(target)
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)                   # a stale socket from a previous run
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        # The socket directory is owned by this engine with the gateway's group
        # (CONTRACTS §2.1); owner and group may connect, nobody else.
        os.chmod(path, 0o660)
        sock.listen(128)
        return sock, f"unix:{path}"
    host, port = target
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0x7FFFFFFB), 1)
    sock.bind((host, port))
    sock.listen(128)
    return sock, f"tcp:{host}:{sock.getsockname()[1]}"


class JsonLines(io.TextIOBase):
    """Stdout after READY: every line becomes (or already is) one JSON record."""

    def __init__(self, raw: TextIO) -> None:
        super().__init__()
        self._raw = raw
        self._buffer = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        text = str(text)
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._emit(line)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._buffer:
                self._emit(self._buffer)
                self._buffer = ""
            self._raw.flush()

    def _emit(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line.startswith("{") and line.endswith("}"):
            try:
                json.loads(line)
                self._raw.write(line + "\n")
                self._raw.flush()
                return
            except ValueError:
                pass
        record = {"ts": round(time.time(), 3), "level": "info", "logger": "engine",
                  "message": line[:300]}
        self._raw.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._raw.flush()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {"ts": round(record.created, 3),
                                   "level": record.levelname.lower(),
                                   "logger": record.name,
                                   "message": record.getMessage()[:300]}
        if record.exc_info:
            payload["error"] = record.exc_info[0].__name__ if record.exc_info[0] else "error"
        return json.dumps(payload, ensure_ascii=False)


class _StreamHandler(logging.Handler):
    """Writes to whatever `sys.stdout` / `sys.stderr` is at emit time."""

    def __init__(self, use_stdout: bool) -> None:
        super().__init__()
        self.use_stdout = use_stdout

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stdout if self.use_stdout else sys.stderr
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:                                   # noqa: BLE001
            pass


def log(event: str, **fields: Any) -> None:
    """One structured line. Never pass prompts, drafts or tokens."""
    record = {"ts": round(time.time(), 3), "level": "info", "logger": "engine.worker",
              "event": event, **fields}
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def engine_version() -> str:
    from app.identity import ENGINE_VERSION
    return ENGINE_VERSION


def _configure_logging(handler: _StreamHandler) -> None:
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True


def _make_server(app: Any):
    import contextlib
    import signal

    import uvicorn
    from uvicorn.server import HANDLED_SIGNALS

    from app import request_context

    class EngineServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            # uvicorn re-raises a captured signal once shutdown completes so the
            # default handler runs, and that handler ends the process with the
            # signal's own status (3 for CTRL_BREAK on Windows, a signal death
            # on POSIX). A graceful stop must exit 0 (CONTRACTS §3.1), so the
            # handlers are restored without re-raising.
            if threading.current_thread() is not threading.main_thread():
                yield
                return
            original = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
            try:
                yield
            finally:
                for sig, handler in original.items():
                    signal.signal(sig, handler)

        def handle_exit(self, sig: int, frame: Any) -> None:
            # Stop the pipelines first so they stop starting AI calls, then let
            # uvicorn stop accepting and drain within the graceful timeout.
            request_context.cancel_all()
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, lifespan="on", loop="asyncio", ws="none",
                            log_config=None, access_log=False, server_header=False,
                            date_header=False, timeout_graceful_shutdown=int(GRACE_S))
    return EngineServer(config)


def _warm_services_client() -> None:
    try:
        from app import cloud

        cloud._client()
    except Exception:  # a warm-up must never take the engine down; the first press builds it instead
        pass


def _warm_sklearn() -> None:
    """Import the scikit-learn pieces a draft may need, after READY instead of before it.

    `app.intelligence` and `app.performance` import them where they fit or score, not at
    module level, so READY no longer waits the ~1.2 s they take to import; this warms them
    in the background so the first draft does not pay for them either.
    """
    try:
        import sklearn.ensemble  # noqa: F401
        import sklearn.feature_extraction.text  # noqa: F401
        import sklearn.linear_model  # noqa: F401
        import sklearn.metrics  # noqa: F401
        import sklearn.metrics.pairwise  # noqa: F401
        import sklearn.preprocessing  # noqa: F401
    except Exception:  # a warm-up must never take the engine down; the first use imports them instead
        pass


def _warm_account_caches() -> None:
    """`app.background.warm_caches`: no model call, no web search. Never raises."""
    try:
        from app import background
        background.warm_caches()
    except Exception:  # a warm-up must never take the engine down; the first press builds them instead
        pass


async def _background_loop() -> None:
    """This account's background AI work (voice card, topic research) on a slow loop.

    API-native pass. Each job decides for itself whether it is due and the account is
    active (`app.background`); a press also starts them right after it finishes. Runs in an
    executor thread, in a context of its own, so none of it is attributed to a request.
    """
    from app import background
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(background.LOOP_INTERVAL_S)
        try:
            await loop.run_in_executor(None, contextvars.Context().run, background.run_due_jobs)
        except asyncio.CancelledError:
            raise
        except Exception:  # background work must never take the engine down
            pass


async def _serve(sock: socket.socket, listen: str, started: float,
                 handler: _StreamHandler) -> int:
    from app import main as engine_main

    server = _make_server(engine_main.app)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    give_up = time.monotonic() + READY_TIMEOUT_S
    while not server.started:
        if serving.done():
            return 3                               # startup failed; uvicorn logged why
        if time.monotonic() > give_up:
            server.should_exit = True
            await serving
            return 4
        await asyncio.sleep(0.01)

    ready = {"listen": listen, "pid": os.getpid(), "engine_version": engine_version()}
    _REAL_STDOUT.write("READY " + json.dumps(ready) + "\n")
    _REAL_STDOUT.flush()
    sys.stdout = JsonLines(_REAL_STDOUT)
    handler.use_stdout = True
    log("engine_ready", cold_start_ms=round((time.monotonic() - started) * 1000.0, 1))
    # The learning reconciliation runs after READY, in a context of its own, so
    # none of it is attributed to a request.
    asyncio.get_running_loop().create_task(engine_main.run_learning_pass(),
                                           context=contextvars.Context())
    # Build the services client now rather than on the first press, which otherwise pays for it.
    asyncio.get_running_loop().run_in_executor(None, _warm_services_client)
    # And the scikit-learn imports that used to hold READY back (cold start).
    asyncio.get_running_loop().run_in_executor(None, _warm_sklearn)
    # And the account's no-AI caches (profile, niche read, news pool), off the first press.
    asyncio.get_running_loop().run_in_executor(None, contextvars.Context().run, _warm_account_caches)
    # The account's background AI work, on a slow loop (a press also starts it).
    asyncio.get_running_loop().create_task(_background_loop(), context=contextvars.Context())
    await serving
    return 0


def main() -> int:
    started = time.monotonic()
    if os.name == "posix":
        os.umask(0o007)                            # CONTRACTS §2.1
    os.environ["STARLING_HOSTED"] = "1"
    handler = _StreamHandler(use_stdout=False)
    _configure_logging(handler)
    secrets = read_secrets(sys.stdin)
    kind, target = parse_listen(os.environ.get("STARLING_LISTEN", "tcp:127.0.0.1:0"))

    # Imported only now: module import reads STARLING_APP_DIR and the hosted flag.
    from app import cloud, security, storage
    security.set_worker_token(secrets.pop("worker_token"))
    cloud.set_services_token(secrets.pop("services_token"))
    secrets.clear()

    sock, listen = bind_listener(kind, target)
    code = asyncio.run(_serve(sock, listen, started, handler))
    storage.checkpoint()
    cloud.invalidate()
    if code == 0:
        log("engine_stopped", uptime_s=round(time.monotonic() - started, 1))
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        # Worker threads still inside a slow network read must not hold the
        # process past the graceful window; their requests were cancelled.
        os._exit(code)


if __name__ == "__main__":
    main()
