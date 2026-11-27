"""Child processes with captured logs, readiness waits and a stop that leaves nothing behind.

Every process the harness starts gets its own log file in the run folder
(`logs/<name>.out.txt` and `logs/<name>.err.txt`) and keeps its last lines in memory so a
failed start can say why. Stop is graceful first (CTRL_BREAK on Windows, SIGTERM on POSIX,
both delivered to the child's own process group), then a kill after the grace period.

The gateway spawns engine workers of its own. Stopping it gracefully is what stops them, so
`stop()` never kills the gateway before its grace period is spent, and `sweep_engines()`
exists for the case where it dies anyway.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

IS_WINDOWS = sys.platform == "win32"
READY_PREFIX = "READY "
TAIL_LINES = 60


class ProcessError(RuntimeError):
    """A child process could not be started, did not become ready, or died too early."""


def _platform_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


@dataclass
class _Stream:
    path: Path
    tail: deque
    thread: threading.Thread | None = None


class Proc:
    """One child process of the harness."""

    def __init__(
        self,
        name: str,
        command: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        log_dir: Path,
        stdin_line: bytes | None = None,
    ) -> None:
        self.name = name
        self.command = list(command)
        self.cwd = Path(cwd)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.ready_line: str | None = None
        self._ready = threading.Event()
        self._out = _Stream(self.log_dir / f"{name}.out.txt", deque(maxlen=TAIL_LINES))
        self._err = _Stream(self.log_dir / f"{name}.err.txt", deque(maxlen=TAIL_LINES))
        self.started_at = time.time()
        try:
            self.proc = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE if stdin_line is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                **_platform_kwargs(),
            )
        except OSError as exc:
            raise ProcessError(f"{name}: could not start ({exc.__class__.__name__}: {exc})") from exc
        if stdin_line is not None and self.proc.stdin is not None:
            self.proc.stdin.write(stdin_line)
            self.proc.stdin.flush()
            self.proc.stdin.close()
        self._out.thread = self._reader(self.proc.stdout, self._out, watch_ready=True)
        self._err.thread = self._reader(self.proc.stderr, self._err, watch_ready=False)

    # ---- output

    def _reader(self, stream, target: _Stream, *, watch_ready: bool) -> threading.Thread:
        def pump() -> None:
            with target.path.open("ab", buffering=0) as sink:
                for raw in iter(stream.readline, b""):
                    sink.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    text = raw.decode("utf-8", "replace").rstrip("\r\n")
                    target.tail.append(text)
                    if watch_ready and not self._ready.is_set() and text.startswith(READY_PREFIX):
                        self.ready_line = text
                        self._ready.set()

        thread = threading.Thread(target=pump, name=f"{self.name}-{target.path.stem}", daemon=True)
        thread.start()
        return thread

    def tail(self, lines: int = 20) -> str:
        parts = [f"--- {self.name} stdout ---", *list(self._out.tail)[-lines:]]
        parts += [f"--- {self.name} stderr ---", *list(self._err.tail)[-lines:]]
        return "\n".join(parts)

    # ---- state

    @property
    def pid(self) -> int:
        return int(self.proc.pid)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait_ready_line(self, timeout_s: float) -> dict:
        """Wait for the `READY {...}` line the services process and engines print."""
        give_up = time.monotonic() + timeout_s
        while time.monotonic() < give_up:
            if self._ready.wait(0.1):
                payload = (self.ready_line or "")[len(READY_PREFIX):]
                try:
                    data = json.loads(payload)
                except ValueError as exc:
                    raise ProcessError(f"{self.name}: unreadable READY line: {payload!r}") from exc
                if not isinstance(data, dict):
                    raise ProcessError(f"{self.name}: READY line is not an object: {payload!r}")
                return data
            if not self.alive():
                raise ProcessError(f"{self.name}: exited with code {self.proc.returncode} before READY\n{self.tail()}")
        raise ProcessError(f"{self.name}: no READY line within {timeout_s:g}s\n{self.tail()}")

    def wait_until(self, check: Callable[[], bool], timeout_s: float, what: str) -> None:
        give_up = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < give_up:
            if not self.alive():
                raise ProcessError(f"{self.name}: exited with code {self.proc.returncode} while waiting for {what}\n{self.tail()}")
            try:
                if check():
                    return
            except Exception as exc:  # a connection refused while it is still binding
                last_error = exc
            time.sleep(0.1)
        suffix = f" (last error: {last_error.__class__.__name__}: {last_error})" if last_error else ""
        raise ProcessError(f"{self.name}: not ready for {what} within {timeout_s:g}s{suffix}\n{self.tail()}")

    # ---- stopping

    def signal_graceful(self) -> None:
        if not self.alive():
            return
        try:
            if IS_WINDOWS:
                os.kill(self.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                self.proc.terminate()
            except OSError:
                pass

    def stop(self, grace_s: float = 20.0) -> int | None:
        if self.proc.poll() is None:
            self.signal_graceful()
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.kill()
        self._close_pipes()
        return self.proc.returncode

    def kill(self) -> None:
        try:
            if IS_WINDOWS:
                self.proc.kill()
            else:
                os.killpg(self.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                self.proc.kill()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def _close_pipes(self) -> None:
        for stream in (self.proc.stdout, self.proc.stderr, self.proc.stdin):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- engine sweep


def process_command_line(pid: int) -> str | None:
    """The command line of a live process, or None when it is gone or unreadable."""
    if IS_WINDOWS:
        script = (
            "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" -ErrorAction SilentlyContinue; "
            "if ($p) { $p.CommandLine }" % int(pid)
        )
        try:
            done = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = done.stdout.decode("utf-8", "replace").strip()
        return text or None
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
    except OSError:
        try:
            done = subprocess.run(["ps", "-o", "args=", "-p", str(int(pid))], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.decode("utf-8", "replace").strip() or None


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return process_command_line(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_pid(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=60)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


ENGINE_MARKER = "app.worker"


def sweep_engines(pids: Iterable[int]) -> list[int]:
    """Kill any engine worker of this run that outlived the gateway. Returns the pids killed.

    A pid is only killed when its command line still names the engine worker module, so a
    recycled pid belonging to something else is left alone.
    """
    killed: list[int] = []
    for pid in {int(p) for p in pids if int(p) > 0}:
        command = process_command_line(pid)
        if command and ENGINE_MARKER in command:
            kill_pid(pid)
            killed.append(pid)
    return killed
