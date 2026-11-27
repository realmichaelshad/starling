"""Development spawner: engines as child processes of the gateway (`STARLING_ISOLATION=none`).

Only for `STARLING_ENV` development and e2e. Each engine still gets the contract of
CONTRACTS section 3.1: a clean environment (no gateway secrets), secrets as one JSON line
on stdin, and a READY line within 30 seconds naming `tcp:127.0.0.1:<port>`.

Processes are driven with `subprocess.Popen` and small reader threads instead of asyncio
subprocesses, so the spawner works on every event loop (a Windows selector loop cannot
start asyncio subprocesses). Stop sends SIGTERM to the engine's session on POSIX and
CTRL_BREAK to its process group on Windows, then kills after the grace period.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from secrets import token_hex
from pathlib import Path
from typing import Sequence

import httpx

from ..config import REPO_ROOT, Settings
from .spawner import (
    MAX_LOG_LINE_BYTES,
    MAX_PRE_READY_BYTES,
    POSIX_BASE_ENV,
    READY_PREFIX,
    READY_TIMEOUT_S,
    WINDOWS_BASE_ENV,
    EngineSecrets,
    EngineStatus,
    ExitCallback,
    SpawnedEngine,
    SpawnError,
    assert_engine_environment_clean,
    base_url_and_transport,
    engine_environment,
    parse_ready_line,
    read_rss_mb,
    sanitize_log_line,
    sanitize_stderr_line,
    tcp_listen_port,
    validate_tenant_id,
    validate_tenant_seq,
    validate_worker_token,
)

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
DEFAULT_ENGINE_DIR = REPO_ROOT / "backend" / "engine"
DEV_LISTEN = "tcp:127.0.0.1:0"


@dataclass
class _Child:
    tenant_seq: int
    tenant_id: str
    proc: subprocess.Popen
    started_at: int
    loop: asyncio.AbstractEventLoop
    ready: asyncio.Future
    listen: str | None = None
    stopping: bool = False
    exited: threading.Event = field(default_factory=threading.Event)
    returncode: int | None = None


class DevSpawner:
    kind = "dev"
    reports_exits = True

    def __init__(
        self,
        settings: Settings,
        command: Sequence[str] | None = None,
        cwd: Path | str | None = None,
        ready_timeout_s: float = READY_TIMEOUT_S,
    ) -> None:
        if settings.isolation != "none" or settings.is_production:
            raise SpawnError("the development spawner requires STARLING_ISOLATION=none outside production")
        self._settings = settings
        self._command = list(command) if command else [sys.executable, "-m", "app.worker"]
        self._cwd = Path(cwd) if cwd else DEFAULT_ENGINE_DIR
        self._ready_timeout_s = ready_timeout_s
        self._children: dict[int, _Child] = {}
        self._callback: ExitCallback | None = None
        self._closed = False

    # ------------------------------------------------------------------ interface

    def set_exit_callback(self, callback: ExitCallback | None) -> None:
        self._callback = callback

    def tenant_dir(self, tenant_seq: int) -> Path:
        return self._settings.tenants_dir / str(validate_tenant_seq(tenant_seq))

    def transport_for(self, listen: str) -> tuple[str, httpx.AsyncBaseTransport | None]:
        tcp_listen_port(listen)  # development engines listen on loopback TCP only
        return base_url_and_transport(listen)

    async def spawn(self, tenant_seq: int, tenant_id: str, secrets: EngineSecrets) -> SpawnedEngine:
        if self._closed:
            raise SpawnError("spawner is closed")
        seq = validate_tenant_seq(tenant_seq)
        try:
            validate_tenant_id(tenant_id)
            validate_worker_token(secrets.worker_token)
        except ValueError as exc:
            raise SpawnError(str(exc)) from exc
        existing = self._children.get(seq)
        if existing is not None and existing.proc.poll() is None:
            raise SpawnError(f"engine {seq} is already running")

        loop = asyncio.get_running_loop()
        app_dir = self.tenant_dir(seq)
        await asyncio.to_thread(_prepare_tenant_dir, app_dir)
        env = self._environment(app_dir)
        assert_engine_environment_clean(env, os.environ)
        try:
            proc = subprocess.Popen(
                self._command,
                cwd=str(self._cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                **_platform_kwargs(),
            )
        except OSError as exc:
            raise SpawnError(f"could not start engine {seq}: {exc.__class__.__name__}") from exc

        child = _Child(seq, tenant_id, proc, int(time.time()), loop, loop.create_future())
        # The READY future can fail after spawn() stopped waiting (timeout); mark that retrieved.
        child.ready.add_done_callback(lambda fut: fut.cancelled() or fut.exception())
        self._children[seq] = child
        for target, name in ((self._read_stdout, "out"), (self._read_stderr, "err"), (self._wait_exit, "wait")):
            threading.Thread(target=target, args=(child,), name=f"engine-{seq}-{name}", daemon=True).start()

        try:
            _write_secrets(proc, secrets.stdin_line(tenant_id))
            listen, ready_pid, _version = await asyncio.wait_for(asyncio.shield(child.ready), self._ready_timeout_s)
            tcp_listen_port(listen)
        except asyncio.TimeoutError:
            await self._discard(child)
            raise SpawnError(f"engine {seq} did not report READY within {self._ready_timeout_s:g} seconds") from None
        except asyncio.CancelledError:
            self._kill_now(child)
            raise
        except BaseException:
            await self._discard(child)
            raise
        child.listen = listen
        if ready_pid != proc.pid:
            # A Windows virtual-environment launcher runs the interpreter as its own child.
            log.debug("engine %s reported pid %s; managing launcher pid %s", seq, ready_pid, proc.pid)
        log.info("engine %s ready (pid %s)", seq, proc.pid)
        return SpawnedEngine(seq, tenant_id, proc.pid, listen, child.started_at, _version)

    async def stop(self, tenant_seq: int, grace_s: float = 10.0) -> None:
        child = self._children.get(tenant_seq)
        if child is None:
            return
        child.stopping = True
        if child.proc.poll() is None:
            _signal_graceful(child.proc)
            exited = await asyncio.to_thread(child.exited.wait, max(0.0, grace_s))
            if not exited:
                log.warning("engine %s did not stop within %.1f seconds; killing it", tenant_seq, grace_s)
                self._kill_now(child)
                await asyncio.to_thread(child.exited.wait, 10.0)
        if self._children.get(tenant_seq) is child:
            del self._children[tenant_seq]
        log.info("engine %s stopped", tenant_seq)

    async def delete_data(self, tenant_seq: int) -> None:
        seq = validate_tenant_seq(tenant_seq)
        await self.stop(seq, grace_s=5.0)
        await asyncio.to_thread(_remove_tenant_dir, self._settings.tenants_dir, self.tenant_dir(seq))

    async def status(self) -> list[EngineStatus]:
        return [
            EngineStatus(seq, child.proc.pid, read_rss_mb(child.proc.pid), child.started_at)
            for seq, child in list(self._children.items())
            if child.listen and child.proc.poll() is None
        ]

    async def is_running(self, tenant_seq: int) -> bool:
        child = self._children.get(tenant_seq)
        return child is not None and child.proc.poll() is None

    async def close(self) -> None:
        self._closed = True
        await asyncio.gather(*(self.stop(seq, 5.0) for seq in list(self._children)), return_exceptions=True)

    # ------------------------------------------------------------------ internals

    def _environment(self, app_dir: Path) -> dict[str, str]:
        names = WINDOWS_BASE_ENV if IS_WINDOWS else POSIX_BASE_ENV
        env = {name: os.environ[name] for name in names if name in os.environ}
        tmp = str(app_dir / "tmp")
        env.update({"HOME": str(app_dir), "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp})
        if IS_WINDOWS:
            # Windows resolves `~` from USERPROFILE (or HOMEDRIVE + HOMEPATH); HOME alone means
            # nothing there, and the engine reads `Path.home()` while importing `app.paths`, so
            # without this the worker exits before READY with "Could not determine home
            # directory". The home stays inside the customer's own folder, as on Linux.
            env["USERPROFILE"] = str(app_dir)
            env["HOMEDRIVE"] = app_dir.drive
            env["HOMEPATH"] = str(app_dir)[len(app_dir.drive):] or "\\"
        env.update(engine_environment(app_dir=app_dir, listen=DEV_LISTEN, services_url=self._settings.services_url))
        return env

    def _resolve_ready(self, child: _Child, result: object = None, error: BaseException | None = None) -> None:
        def apply() -> None:
            if child.ready.done():
                return
            if error is not None:
                child.ready.set_exception(error)
            else:
                child.ready.set_result(result)

        try:
            child.loop.call_soon_threadsafe(apply)
        except RuntimeError:  # event loop already closed
            pass

    def _read_stdout(self, child: _Child) -> None:
        stream = child.proc.stdout
        seen, ready, skipping = 0, False, False
        try:
            while True:
                raw = stream.readline(MAX_LOG_LINE_BYTES + 1)
                if not raw:
                    break
                if skipping:  # the rest of an oversized line
                    skipping = not raw.endswith(b"\n")
                    continue
                if len(raw) > MAX_LOG_LINE_BYTES and not raw.endswith(b"\n"):
                    skipping = True
                    log.warning("engine %s: oversized stdout line skipped", child.tenant_seq)
                    continue
                if not ready:
                    seen += len(raw)
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith(READY_PREFIX):
                        ready = True
                        try:
                            self._resolve_ready(child, result=parse_ready_line(line))
                        except SpawnError as exc:
                            self._resolve_ready(child, error=exc)
                    elif seen > MAX_PRE_READY_BYTES:
                        ready = True
                        self._resolve_ready(child, error=SpawnError("engine wrote too much output before READY"))
                    continue
                level, text = sanitize_log_line(raw.rstrip(b"\r\n"))
                log.log(level, "engine %s: %s", child.tenant_seq, text)
        except (OSError, ValueError):
            pass

    def _read_stderr(self, child: _Child) -> None:
        stream = child.proc.stderr
        ignored = 0
        try:
            while True:
                raw = stream.readline(MAX_LOG_LINE_BYTES + 1)
                if not raw:
                    break
                found = sanitize_stderr_line(raw)
                if found is None:
                    ignored += len(raw)
                    continue
                log.log(found[0], "engine %s: %s", child.tenant_seq, found[1])
        except (OSError, ValueError):
            pass
        if ignored:
            log.debug("engine %s wrote %s bytes of other stderr output", child.tenant_seq, ignored)

    def _wait_exit(self, child: _Child) -> None:
        returncode = child.proc.wait()
        child.returncode = returncode
        child.exited.set()
        self._resolve_ready(child, error=SpawnError(f"engine exited with code {returncode} before READY"))

        def notify() -> None:
            if self._children.get(child.tenant_seq) is child and child.proc.poll() is not None:
                del self._children[child.tenant_seq]
            if child.stopping or child.listen is None:
                return
            log.warning("engine %s exited unexpectedly with code %s", child.tenant_seq, returncode)
            if self._callback is not None:
                self._callback(child.tenant_seq, returncode, "exited")

        try:
            child.loop.call_soon_threadsafe(notify)
        except RuntimeError:
            pass

    async def _discard(self, child: _Child) -> None:
        """Kill an engine that failed to start, wait for it to exit and forget it."""
        self._kill_now(child)
        await asyncio.to_thread(child.exited.wait, 10.0)
        if self._children.get(child.tenant_seq) is child:
            del self._children[child.tenant_seq]

    def _kill_now(self, child: _Child) -> None:
        child.stopping = True
        if child.proc.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                child.proc.kill()
            else:
                os.killpg(child.proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                child.proc.kill()
            except OSError:
                pass


# --------------------------------------------------------------------------- helpers


def _platform_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _write_secrets(proc: subprocess.Popen, line: bytes) -> None:
    try:
        proc.stdin.write(line)
        proc.stdin.flush()
        proc.stdin.close()
    except (OSError, ValueError) as exc:
        raise SpawnError("engine exited before reading its secrets") from exc


def _signal_graceful(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass


def _prepare_tenant_dir(app_dir: Path) -> None:
    if app_dir.is_symlink():
        raise SpawnError("tenant data directory must not be a symbolic link")
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "tmp").mkdir(exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(app_dir, 0o700)


def _remove_tenant_dir(tenants_dir: Path, app_dir: Path) -> None:
    if app_dir.parent.resolve() != tenants_dir.resolve():
        raise SpawnError("refusing to delete a directory outside the tenants folder")
    if app_dir.is_symlink():
        raise SpawnError("tenant data directory must not be a symbolic link")

    def make_writable(func, path, _exc_info):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)

    # Renamed first, to a non-numeric name that backups skip, so nothing sees a half-deleted folder.
    doomed = app_dir.with_name(f".deleting-{app_dir.name}-{token_hex(4)}")
    for attempt in range(25):
        if not app_dir.exists() and not doomed.exists():
            return
        try:
            if app_dir.exists():
                os.rename(app_dir, doomed)
            shutil.rmtree(doomed, onerror=make_writable)
            return
        except FileNotFoundError:
            return
        except OSError:
            # Windows keeps files locked for a moment after the engine process exits.
            time.sleep(0.2)
    raise SpawnError("could not delete the tenant data directory")
