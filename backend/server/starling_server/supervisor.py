"""Container entrypoint: the process tree and one OS user per customer engine.

CONTRACTS sections 2.1, 3.1 and 5. Linux only; runs as root under tini (PID 1). Expected
image configuration (the Dockerfile belongs to the operations workstream):

    ENTRYPOINT ["/usr/bin/tini", "--"]
    CMD ["python", "-m", "starling_server.supervisor"]
    STOPSIGNAL SIGTERM

with groups and users created in the image: `starling-gw` (uid and gid 10001), `starling-svc`
(uid and gid 10002), `starling-eng` (gid 10010), `starling-db` (gid 10020); `/data` a volume;
the application code owned by root and not writable by those users.

The supervisor

* prepares `/data/control` (2770 starling-gw:starling-db, `control.db` 0660), `/data/tenants`
  (0711 root), `/data/spool` (0700 starling-gw) and `/run/starling/engines` (0711 root);
* listens on `/run/starling/spawner.sock` (0660 root:starling-gw), answers only peers with
  uid 0 or 10001, validates every request strictly, and handles `spawn`, `stop`,
  `delete_data` and `status` (never logging request contents: they carry secrets);
* starts the gateway (`uvicorn starling_server.app:create_app --factory`, 0.0.0.0:8080) as
  uid 10001 with supplementary groups starling-eng and starling-db, and the services process
  (`python -m starling_server.services.main`) as uid 10002 with starling-db, each with only
  the secrets it may hold, restarting either with backoff when it exits;
* spawns engines as uid and gid `ENGINE_UID_BASE + tenant_seq` with supplementary group
  starling-eng, umask 007, `PR_SET_NO_NEW_PRIVS`, a clean environment (CONTRACTS 3.1 only),
  data folder `/data/tenants/<seq>` (0700, owned by the engine) and socket folder
  `/run/starling/engines/<seq>` (2750, engine uid, group starling-gw);
* samples engine memory from `/proc/<pid>/status`: at `ENGINE_MEMORY_SOFT_MB` the engine is
  reported `over_soft_limit` in `status` (the gateway pool restarts it once idle); at
  `ENGINE_MEMORY_HARD_MB` it is killed;
* runs the nightly backup thread (`ops.scheduler.start_backup_thread`) and stops it on shutdown.

The gateway runs without uvicorn's access log: query strings carry email-verification and
password-reset tokens. With `STARLING_MAINTENANCE=1` the supervisor starts nothing (no gateway,
services, engines or backups) and does not touch `/data`: it logs one line and waits for
SIGTERM, so the machine and its volume stay up for a restore.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import ctypes.util
import json
import logging
import os
import shutil
import signal
import socket
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from secrets import token_hex
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import REPO_ROOT, SERVER_ROOT, Settings, ensure_valid, load_settings
from .engines.spawner import (
    MAX_LOG_LINE_BYTES,
    MAX_PRE_READY_BYTES,
    READY_PREFIX,
    READY_TIMEOUT_S,
    SpawnError,
    assert_engine_environment_clean,
    engine_environment,
    parse_ready_line,
    read_rss_mb,
    sanitize_log_line,
    sanitize_stderr_line,
    validate_tenant_id,
    validate_tenant_seq,
    validate_worker_token,
)
from .ops.scheduler import start_backup_thread
from .services import tokens as services_tokens

log = logging.getLogger("starling_server.supervisor")

GATEWAY_UID = GATEWAY_GID = 10001
SERVICES_UID = SERVICES_GID = 10002
ENGINE_GROUP_GID = 10010
DB_GROUP_GID = 10020
GATEWAY_GROUPS = (ENGINE_GROUP_GID, DB_GROUP_GID)
SERVICES_GROUPS = (DB_GROUP_GID,)
MIN_ENGINE_UID_BASE = 100_000  # engine uids must never overlap system or service users
MAX_UID = 4_294_967_294

DEFAULT_RUN_DIR = Path("/run/starling")
ENGINE_DIR = REPO_ROOT / "backend" / "engine"
ENGINE_PATH_ENV = "/usr/local/bin:/usr/bin:/bin"
MAX_REQUEST_BYTES = 16 * 1024
MAX_GRACE_S = 60.0
MEMORY_POLL_S = 5.0
CHILD_STOP_GRACE_S = 20.0
RESTART_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
BACKUP_STOP_TIMEOUT_S = 30.0
MAINTENANCE_ENV = "STARLING_MAINTENANCE"
MAINTENANCE_MESSAGE = ("maintenance mode (STARLING_MAINTENANCE=1): not starting the gateway, "
                       "the services process, engines or backups")
DELETING_PREFIX = ".deleting-"  # non-numeric: backups and restores skip folders being deleted
PR_SET_NO_NEW_PRIVS = 38

OPS = ("spawn", "stop", "delete_data", "status")
_ALLOWED_FIELDS = {
    "spawn": frozenset({"op", "tenant_seq", "tenant_id", "secrets"}),
    "stop": frozenset({"op", "tenant_seq", "grace_s"}),
    "delete_data": frozenset({"op", "tenant_seq"}),
    "status": frozenset({"op"}),
}
_REQUIRED_FIELDS = {
    "spawn": _ALLOWED_FIELDS["spawn"],
    "stop": frozenset({"op", "tenant_seq"}),
    "delete_data": _ALLOWED_FIELDS["delete_data"],
    "status": _ALLOWED_FIELDS["status"],
}

# Secrets each long-running child must not receive (CONTRACTS 2.1).
GATEWAY_EXCLUDED_ENV = frozenset({"AI_API_KEY", "AI_API_KEY_SECONDARY", "BACKUP_PASSPHRASE"})
SERVICES_EXCLUDED_ENV = frozenset({"STARLING_SECRET_KEY", "STARLING_SECRET_KEY_PREVIOUS", "BACKUP_PASSPHRASE"})
SERVICES_EXCLUDED_PREFIXES = ("STRIPE_", "SMTP_")
# The services process is the one that talks to the AI provider and fetches pages from the internet, so
# it gets the AI keys and its own token key and nothing else that is secret. Two names are not a rule:
# `STARLING_SECRET_KEY_PREVIOUS` (the old gateway key, live for the 30 days of a rotation) matched
# neither of them and was inherited. Anything that LOOKS like a secret is now withheld by its name.
SERVICES_EXCLUDED_FRAGMENTS = ("SECRET", "PASSPHRASE", "PASSWORD")


class RequestError(ValueError):
    """A refused spawner request. The message is safe to send back and never holds a secret."""


def maintenance_requested(environ: Mapping[str, str]) -> bool:
    """`STARLING_MAINTENANCE=1`: start nothing and keep the machine (and its volume) up for a restore."""
    return environ.get(MAINTENANCE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


async def wait_for_termination(stop: asyncio.Event | None = None) -> None:
    """Block until SIGTERM or SIGINT arrives (or `stop` is set)."""
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signum, stop.set)
    await stop.wait()


# --------------------------------------------------------------------------- request validation


def parse_request(raw: bytes, *, token_key: bytes | None, now: int, require_token_key: bool = True) -> dict[str, Any]:
    """Validate one newline-JSON request line of CONTRACTS section 5 and normalise it."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestError("request too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RequestError("request is not valid JSON") from None
    if not isinstance(payload, dict):
        raise RequestError("request must be a JSON object")
    op = payload.get("op")
    if not isinstance(op, str) or op not in OPS:
        raise RequestError("unknown op")
    keys = set(payload)
    if not keys <= _ALLOWED_FIELDS[op] or not _REQUIRED_FIELDS[op] <= keys:
        raise RequestError("unexpected or missing fields")
    request: dict[str, Any] = {"op": op}
    if "tenant_seq" in payload:
        try:
            request["tenant_seq"] = validate_tenant_seq(payload["tenant_seq"])
        except ValueError as exc:
            raise RequestError(str(exc)) from None
    if op == "stop":
        grace = payload.get("grace_s", 10)
        if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not 0 <= grace <= MAX_GRACE_S:
            raise RequestError("grace_s must be a number from 0 to 60")
        request["grace_s"] = float(grace)
    if op == "spawn":
        try:
            tenant_id = validate_tenant_id(payload["tenant_id"])
        except ValueError as exc:
            raise RequestError(str(exc)) from None
        secrets_value = payload["secrets"]
        if not isinstance(secrets_value, dict) or set(secrets_value) != {"worker_token", "services_token"}:
            raise RequestError("secrets must hold exactly worker_token and services_token")
        try:
            worker_token = validate_worker_token(secrets_value["worker_token"])
        except ValueError as exc:
            raise RequestError(str(exc)) from None
        services_token = secrets_value["services_token"]
        verify_services_token(services_token, tenant_id, token_key, now, require_key=require_token_key)
        request.update(tenant_id=tenant_id, worker_token=worker_token, services_token=services_token)
    return request


def verify_services_token(token: object, tenant_id: str, key: bytes | None, now: int, *, require_key: bool = True) -> None:
    """The services token must be validly signed for `t-<tenant_id>` (CONTRACTS 4.3 and 5)."""
    if not isinstance(token, str) or not token:
        raise RequestError("services_token is missing")
    if key:
        try:
            services_tokens.verify_engine_token(key, token, tenant_id, now)
        except services_tokens.TokenError as exc:
            raise RequestError(f"services_token rejected: {exc}") from None
        return
    if require_key:
        raise RequestError("the services token key is not configured")
    parts = token.split(".")  # development without a key: shape and subject only
    if len(parts) != 5 or parts[0] != "svc1" or parts[1] != f"t-{tenant_id}" or not parts[2].isdigit() or int(parts[2]) <= now:
        raise RequestError("services_token rejected: malformed token")


def engine_ids(tenant_seq: int, uid_base: int) -> tuple[int, int]:
    """(uid, gid) of a customer's engine: `ENGINE_UID_BASE + tenant_seq` for both."""
    seq = validate_tenant_seq(tenant_seq)
    if isinstance(uid_base, bool) or not isinstance(uid_base, int) or uid_base < MIN_ENGINE_UID_BASE:
        raise ValueError(f"ENGINE_UID_BASE must be at least {MIN_ENGINE_UID_BASE}")
    uid = uid_base + seq
    if uid > MAX_UID:
        raise ValueError("engine uid out of range")
    return uid, uid


# --------------------------------------------------------------------------- environments


def gateway_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in environ.items() if name.upper() not in GATEWAY_EXCLUDED_ENV}


def services_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value for name, value in environ.items()
        if name.upper() not in SERVICES_EXCLUDED_ENV and not name.upper().startswith(SERVICES_EXCLUDED_PREFIXES)
        and not any(fragment in name.upper() for fragment in SERVICES_EXCLUDED_FRAGMENTS)
    }


@dataclass(frozen=True)
class Layout:
    data_dir: Path
    run_dir: Path = DEFAULT_RUN_DIR

    @property
    def control_dir(self) -> Path:
        return self.data_dir / "control"

    @property
    def tenants_dir(self) -> Path:
        return self.data_dir / "tenants"

    @property
    def spool_dir(self) -> Path:
        return self.data_dir / "spool"

    @property
    def engines_run_dir(self) -> Path:
        return self.run_dir / "engines"

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "spawner.sock"

    def tenant_dir(self, tenant_seq: int) -> Path:
        return self.tenants_dir / str(validate_tenant_seq(tenant_seq))

    def engine_socket(self, tenant_seq: int) -> Path:
        return self.engines_run_dir / str(validate_tenant_seq(tenant_seq)) / "engine.sock"


def engine_env(settings: Settings, layout: Layout, tenant_seq: int) -> dict[str, str]:
    """The complete engine environment: CONTRACTS 3.1 plus a minimal PATH, HOME and locale."""
    app_dir = layout.tenant_dir(tenant_seq)
    env = {
        "PATH": ENGINE_PATH_ENV,
        "HOME": str(app_dir),
        "TMPDIR": str(app_dir / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    env.update(engine_environment(
        app_dir=app_dir,
        listen="unix:" + str(layout.engine_socket(tenant_seq)),
        services_url=settings.services_url,
    ))
    return env


# --------------------------------------------------------------------------- directories (Linux)


def ensure_dir(path: Path, mode: int, uid: int, gid: int) -> None:
    """Create or fix a directory without following symbolic links (fd-based chown/chmod)."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)  # after chown, which clears set-group-id bits
    finally:
        os.close(fd)


def ensure_file(path: Path, mode: int, uid: int, gid: int) -> None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def prepare_base_dirs(layout: Layout) -> None:
    ensure_dir(layout.data_dir, 0o755, 0, 0)
    ensure_dir(layout.control_dir, 0o2770, GATEWAY_UID, DB_GROUP_GID)
    # SQLite creates the database with mode 0644 and copies that mode to -wal and -shm, so the
    # files are created here group-writable for the services process (group starling-db).
    for suffix in ("", "-wal", "-shm"):
        candidate = layout.control_dir / f"control.db{suffix}"
        if suffix == "" or candidate.exists():
            ensure_file(candidate, 0o660, GATEWAY_UID, DB_GROUP_GID)
    ensure_dir(layout.tenants_dir, 0o711, 0, 0)
    ensure_dir(layout.spool_dir, 0o700, GATEWAY_UID, GATEWAY_GID)
    ensure_dir(layout.run_dir, 0o755, 0, 0)
    ensure_dir(layout.engines_run_dir, 0o711, 0, 0)


def prepare_tenant_dirs(layout: Layout, tenant_seq: int, uid: int, gid: int) -> Path:
    """Data folder 0700 owned by the engine; socket folder 2750 engine uid, group starling-gw."""
    app_dir = layout.tenant_dir(tenant_seq)
    ensure_dir(app_dir, 0o700, uid, gid)
    ensure_dir(app_dir / "tmp", 0o700, uid, gid)
    socket_dir = layout.engine_socket(tenant_seq).parent
    ensure_dir(socket_dir, 0o2750, uid, GATEWAY_GID)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(layout.engine_socket(tenant_seq))  # a stale socket from an earlier run
    return app_dir


def remove_tenant_data(layout: Layout, tenant_seq: int) -> None:
    """Delete a customer's folders. Each is first renamed (atomically) to a non-numeric
    `.deleting-*` name, so a backup, a restore or a new engine never sees a half-deleted folder."""
    for path in (layout.tenant_dir(tenant_seq), layout.engine_socket(tenant_seq).parent):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RequestError("tenant folder is not a directory")
        doomed = path.with_name(f"{DELETING_PREFIX}{path.name}-{token_hex(4)}")
        os.rename(path, doomed)
        shutil.rmtree(doomed)  # descriptor-based on Linux: never follows links inside the tree


def sweep_deleting_dirs(layout: Layout) -> int:
    """Finish deletions interrupted by a crash or restart (folders named `.deleting-*`)."""
    removed = 0
    for parent in (layout.tenants_dir, layout.engines_run_dir):
        try:
            entries = list(os.scandir(parent))
        except FileNotFoundError:
            continue
        for entry in entries:
            if entry.name.startswith(DELETING_PREFIX) and entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
    return removed


def _libc_prctl():
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    return prctl


def no_new_privs_preexec():
    """A preexec function that sets PR_SET_NO_NEW_PRIVS (resolved before fork, tiny after)."""
    prctl = _libc_prctl()

    def apply() -> None:
        if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            os._exit(126)

    return apply


def peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", data)
    return uid


# --------------------------------------------------------------------------- supervisor


@dataclass
class EngineProcess:
    tenant_seq: int
    tenant_id: str
    uid: int
    proc: asyncio.subprocess.Process
    started_at: int
    listen: str | None = None
    rss_mb: int | None = None
    over_soft_limit: bool = False
    stopping: bool = False
    tasks: list[asyncio.Task] = field(default_factory=list)


@dataclass
class ManagedChild:
    name: str
    argv: list[str]
    uid: int
    gid: int
    groups: tuple[int, ...]
    env: dict[str, str]
    cwd: Path
    proc: asyncio.subprocess.Process | None = None


def gateway_command(settings: Settings) -> list[str]:
    argv = [
        sys.executable, "-m", "uvicorn", "starling_server.app:create_app", "--factory",
        "--host", "0.0.0.0", "--port", "8080", "--workers", "1",
        "--timeout-graceful-shutdown", "20", "--no-server-header", "--no-access-log",
    ]
    if settings.trusted_proxies:
        argv += ["--proxy-headers", "--forwarded-allow-ips", ",".join(settings.trusted_proxies)]
    else:
        argv += ["--no-proxy-headers"]
    return argv


def services_command() -> list[str]:
    return [sys.executable, "-m", "starling_server.services.main"]


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        *,
        layout: Layout | None = None,
        engine_command: Sequence[str] | None = None,
        engine_cwd: Path | None = None,
        allowed_peer_uids: Sequence[int] = (0, GATEWAY_UID),
        socket_gid: int = GATEWAY_GID,
        run_gateway: bool = True,
        run_services: bool = True,
        require_token_key: bool = True,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.layout = layout or Layout(settings.data_dir, DEFAULT_RUN_DIR)
        self.engine_command = list(engine_command or [sys.executable, "-m", "app.worker"])
        self.engine_cwd = Path(engine_cwd or ENGINE_DIR)
        self.allowed_peer_uids = frozenset(allowed_peer_uids)
        self.socket_gid = socket_gid
        self.run_gateway = run_gateway
        self.run_services = run_services
        self.require_token_key = require_token_key
        self.environ = dict(os.environ if environ is None else environ)
        self.memory_poll_s = MEMORY_POLL_S
        self.maintenance = maintenance_requested(self.environ)
        self._backup: tuple[threading.Thread, threading.Event] | None = None
        self.engines: dict[int, EngineProcess] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._stopping: asyncio.Event | None = None
        self._server: asyncio.AbstractServer | None = None
        self._children: list[ManagedChild] = []
        self._tasks: list[asyncio.Task] = []

    def _lock(self, tenant_seq: int) -> asyncio.Lock:
        lock = self._locks.get(tenant_seq)
        if lock is None:
            lock = self._locks[tenant_seq] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------------ run

    async def start(self) -> None:
        if self.maintenance:
            raise RuntimeError("the supervisor is in maintenance mode; run() waits without starting anything")
        if self._stopping is None:
            self._stopping = asyncio.Event()
        prepare_base_dirs(self.layout)
        await self._open_socket()
        self._tasks.append(asyncio.create_task(self._monitor_memory(), name="engine-memory"))
        self._tasks.append(asyncio.create_task(asyncio.to_thread(sweep_deleting_dirs, self.layout),
                                               name="finish-deletions"))
        self._backup = start_backup_thread(self.settings)
        if self.run_services:
            self._children.append(ManagedChild(
                "services", services_command(), SERVICES_UID, SERVICES_GID, SERVICES_GROUPS,
                services_environment(self.environ), SERVER_ROOT))
        if self.run_gateway:
            self._children.append(ManagedChild(
                "gateway", gateway_command(self.settings), GATEWAY_UID, GATEWAY_GID, GATEWAY_GROUPS,
                gateway_environment(self.environ), SERVER_ROOT))
        for child in self._children:
            self._tasks.append(asyncio.create_task(self._keep_running(child), name=f"child-{child.name}"))

    async def run(self) -> int:
        self._stopping = asyncio.Event()
        if self.maintenance:
            log.warning(MAINTENANCE_MESSAGE)
            await wait_for_termination(self._stopping)
            return 0
        await self.start()
        await wait_for_termination(self._stopping)
        log.info("stopping")
        await self.stop()
        return 0

    def request_stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._backup is not None:
            self._backup[1].set()  # no new backup starts; a run in progress finishes
        # The gateway first: its shutdown still stops engines through the socket.
        for child in sorted(self._children, key=lambda c: c.name != "gateway"):
            await self._stop_child(child)
        await asyncio.gather(*(self.stop_engine(seq, 10.0) for seq in list(self.engines)), return_exceptions=True)
        if self._server is not None:
            self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.layout.socket_path)
        if self._backup is not None:
            thread, stop_event = self._backup
            stop_event.set()
            await asyncio.to_thread(thread.join, BACKUP_STOP_TIMEOUT_S)
            if thread.is_alive():
                log.warning("a backup was still running at shutdown")
            self._backup = None

    # ------------------------------------------------------------------ socket

    async def _open_socket(self) -> None:
        path = self.layout.socket_path
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        previous = os.umask(0o177)  # the socket exists with mode 0600 until its group is set
        try:
            self._server = await asyncio.start_unix_server(self._handle_client, path=str(path),
                                                           limit=MAX_REQUEST_BYTES + 1)
        finally:
            os.umask(previous)
        if os.geteuid() == 0:
            os.chown(path, 0, self.socket_gid)
        os.chmod(path, 0o660)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            uid = peer_uid(writer)
            if uid is None or uid not in self.allowed_peer_uids:
                log.warning("spawner connection from uid %s refused", uid)
                await self._reply(writer, {"ok": False, "error": "peer not allowed"})
                return
            while True:
                try:
                    raw = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    return
                except asyncio.LimitOverrunError:
                    await self._reply(writer, {"ok": False, "error": "request too large"})
                    return
                await self._reply(writer, await self.handle_line(raw.rstrip(b"\r\n")))
        except (ConnectionError, OSError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _reply(self, writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
        writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        await writer.drain()

    async def handle_line(self, raw: bytes) -> dict[str, Any]:
        """Validate and execute one request line; never raises, never logs secrets."""
        try:
            request = parse_request(raw, token_key=self.settings.services_token_key or None,
                                    now=int(time.time()), require_token_key=self.require_token_key)
            log.info("spawner %s tenant=%s", request["op"], request.get("tenant_seq", "-"))
            return await self.dispatch(request)
        except RequestError as exc:
            log.warning("spawner request refused: %s", exc)
            return {"ok": False, "error": str(exc)}
        except Exception:
            log.exception("spawner request failed")
            return {"ok": False, "error": "internal error"}

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        if op == "status":
            return self.status()
        if op == "spawn":
            return await self.spawn_engine(request)
        if op == "stop":
            await self.stop_engine(request["tenant_seq"], request["grace_s"])
            return {"ok": True}
        await self.delete_data(request["tenant_seq"])
        return {"ok": True}

    # ------------------------------------------------------------------ engines

    async def spawn_engine(self, request: dict[str, Any]) -> dict[str, Any]:
        seq, tenant_id = request["tenant_seq"], request["tenant_id"]
        async with self._lock(seq):
            current = self.engines.get(seq)
            if current is not None and current.proc.returncode is None:
                raise RequestError("engine is already running")
            try:
                uid, gid = engine_ids(seq, self.settings.engine_uid_base)
            except ValueError as exc:
                raise RequestError(str(exc)) from None
            prepare_tenant_dirs(self.layout, seq, uid, gid)
            env = engine_env(self.settings, self.layout, seq)
            try:
                assert_engine_environment_clean(env, self.environ)
            except SpawnError as exc:
                raise RequestError(str(exc)) from None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.engine_command,
                    cwd=str(self.engine_cwd),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    user=uid,
                    group=gid,
                    extra_groups=[ENGINE_GROUP_GID],
                    umask=0o007,
                    start_new_session=True,
                    preexec_fn=no_new_privs_preexec(),
                    close_fds=True,
                    limit=MAX_LOG_LINE_BYTES + 1,
                )
            except OSError as exc:
                raise RequestError(f"could not start the engine: {exc.__class__.__name__}") from None
            engine = EngineProcess(seq, tenant_id, uid, proc, int(time.time()))
            self.engines[seq] = engine
            engine.tasks.append(asyncio.create_task(self._forward_stderr(engine)))
            expected = "unix:" + str(self.layout.engine_socket(seq))
            try:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps({
                    "worker_token": request["worker_token"],
                    "services_token": request["services_token"],
                    "tenant_id": tenant_id,
                }, separators=(",", ":")).encode("utf-8") + b"\n")
                await proc.stdin.drain()
                proc.stdin.close()
                listen, pid = await asyncio.wait_for(self._read_ready(proc), READY_TIMEOUT_S)
                if listen != expected or pid != proc.pid:
                    raise RequestError("engine READY line does not match its socket or pid")
                info = os.lstat(self.layout.engine_socket(seq))
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid:
                    raise RequestError("engine socket is missing or has the wrong owner")
            except BaseException as exc:
                await self._terminate(engine, 0.0)
                self.engines.pop(seq, None)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, TimeoutError):
                    raise RequestError("engine did not report READY within 30 seconds") from None
                if isinstance(exc, (RequestError, SpawnError)):
                    raise RequestError(str(exc)) from None
                raise RequestError("engine failed to start") from None
            engine.listen = expected
            engine.tasks.append(asyncio.create_task(self._forward_stdout(engine)))
            engine.tasks.append(asyncio.create_task(self._watch_exit(engine)))
            log.info("engine %s started (pid %s, uid %s)", seq, proc.pid, uid)
            return {"ok": True, "pid": proc.pid, "listen": expected}

    async def _read_ready(self, proc: asyncio.subprocess.Process) -> tuple[str, int]:
        assert proc.stdout is not None
        seen = 0
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:
                raise SpawnError("engine wrote an oversized line before READY") from None
            if not raw:
                raise SpawnError("engine exited before READY")
            seen += len(raw)
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith(READY_PREFIX):
                listen, pid, _version = parse_ready_line(line)
                return listen, pid
            if seen > MAX_PRE_READY_BYTES:
                raise SpawnError("engine wrote too much output before READY")

    async def _forward_stdout(self, engine: EngineProcess) -> None:
        stream = engine.proc.stdout
        while stream is not None:
            try:
                raw = await stream.readline()
            except ValueError:
                log.warning("engine %s: oversized stdout line skipped", engine.tenant_seq)
                continue
            if not raw:
                return
            level, text = sanitize_log_line(raw.rstrip(b"\r\n"))
            log.log(level, "engine %s: %s", engine.tenant_seq, text)

    async def _forward_stderr(self, engine: EngineProcess) -> None:
        stream = engine.proc.stderr
        while stream is not None:
            try:
                raw = await stream.readline()
            except ValueError:
                continue
            if not raw:
                return
            found = sanitize_stderr_line(raw)
            if found is not None:
                log.log(found[0], "engine %s: %s", engine.tenant_seq, found[1])

    async def _watch_exit(self, engine: EngineProcess) -> None:
        code = await engine.proc.wait()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(engine.proc.pid, signal.SIGKILL)  # anything left in the engine's session
        if not engine.stopping:
            log.warning("engine %s exited with code %s", engine.tenant_seq, code)
            if self.engines.get(engine.tenant_seq) is engine:
                self.engines.pop(engine.tenant_seq, None)

    async def _terminate(self, engine: EngineProcess, grace_s: float) -> None:
        engine.stopping = True
        if engine.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(engine.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(engine.proc.wait(), timeout=max(grace_s, 0.01))
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(engine.proc.pid, signal.SIGKILL)
                await engine.proc.wait()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(engine.proc.pid, signal.SIGKILL)
        current = asyncio.current_task()
        for task in engine.tasks:
            if task is not current:
                task.cancel()

    async def stop_engine(self, tenant_seq: int, grace_s: float) -> None:
        async with self._lock(tenant_seq):
            engine = self.engines.get(tenant_seq)
            if engine is None:
                return
            await self._terminate(engine, grace_s)
            self.engines.pop(tenant_seq, None)
            log.info("engine %s stopped", tenant_seq)

    async def delete_data(self, tenant_seq: int) -> None:
        await self.stop_engine(tenant_seq, 5.0)
        async with self._lock(tenant_seq):
            await asyncio.to_thread(remove_tenant_data, self.layout, tenant_seq)
        log.info("engine %s data deleted", tenant_seq)

    def status(self) -> dict[str, Any]:
        return {"ok": True, "engines": [
            {"tenant_seq": seq, "pid": engine.proc.pid, "rss_mb": engine.rss_mb,
             "started_at": engine.started_at, "over_soft_limit": engine.over_soft_limit}
            for seq, engine in sorted(self.engines.items())
            if engine.listen is not None and engine.proc.returncode is None
        ]}

    async def check_memory(self) -> None:
        soft, hard = self.settings.engine_memory_soft_mb, self.settings.engine_memory_hard_mb
        for seq, engine in list(self.engines.items()):
            if engine.proc.returncode is not None or engine.listen is None:
                continue
            rss = read_rss_mb(engine.proc.pid)
            engine.rss_mb = rss
            if rss is None:
                continue
            if rss >= hard:
                log.error("engine %s uses %s MB, above the hard limit of %s MB; killing it", seq, rss, hard)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(engine.proc.pid, signal.SIGKILL)
            elif rss >= soft and not engine.over_soft_limit:
                engine.over_soft_limit = True
                log.warning("engine %s uses %s MB, above the soft limit of %s MB", seq, rss, soft)

    async def _monitor_memory(self) -> None:
        while True:
            try:
                await self.check_memory()
            except Exception:
                log.exception("engine memory check failed")
            await asyncio.sleep(self.memory_poll_s)

    # ------------------------------------------------------------------ gateway and services

    async def _keep_running(self, child: ManagedChild) -> None:
        assert self._stopping is not None
        failures = 0
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                child.proc = await asyncio.create_subprocess_exec(
                    *child.argv, cwd=str(child.cwd), env=child.env, stdin=asyncio.subprocess.DEVNULL,
                    user=child.uid, group=child.gid, extra_groups=list(child.groups), umask=0o007,
                    start_new_session=True, close_fds=True)
            except OSError as exc:
                log.error("could not start %s: %s", child.name, exc.__class__.__name__)
            else:
                log.info("%s started (pid %s)", child.name, child.proc.pid)
                code = await child.proc.wait()
                if self._stopping.is_set():
                    return
                log.error("%s exited with code %s; restarting", child.name, code)
            failures = 0 if time.monotonic() - started >= 60 else failures + 1
            delay = RESTART_BACKOFF_S[min(max(failures - 1, 0), len(RESTART_BACKOFF_S) - 1)]
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)

    async def _stop_child(self, child: ManagedChild) -> None:
        proc = child.proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), CHILD_STOP_GRACE_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="starling-supervisor", description="Starling container supervisor (Linux, root)")
    parser.add_argument("--no-gateway", action="store_true", help="do not start the gateway (diagnostics)")
    parser.add_argument("--no-services", action="store_true", help="do not start the services process (diagnostics)")
    args = parser.parse_args(argv)
    if maintenance_requested(os.environ):
        # Checked before loading settings: a restore must stay possible with a broken configuration.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        log.warning(MAINTENANCE_MESSAGE)
        asyncio.run(wait_for_termination())
        return 0
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        ensure_valid(settings, "supervisor")
    except Exception as exc:
        log.error("%s", exc)
        return 2
    problems = []
    if sys.platform != "linux":
        problems.append("the supervisor runs only on Linux")
    elif os.geteuid() != 0:
        problems.append("the supervisor must run as root")
    if settings.isolation != "os":
        problems.append("the supervisor requires STARLING_ISOLATION=os")
    if settings.engine_uid_base < MIN_ENGINE_UID_BASE:
        problems.append(f"ENGINE_UID_BASE must be at least {MIN_ENGINE_UID_BASE}")
    if not settings.services_token_key:
        problems.append("STARLING_SERVICES_TOKEN_KEY is required")
    for problem in problems:
        log.error("%s", problem)
    if problems:
        return 2
    supervisor = Supervisor(settings, run_gateway=not args.no_gateway, run_services=not args.no_services)
    return asyncio.run(supervisor.run())


if __name__ == "__main__":
    sys.exit(main())
