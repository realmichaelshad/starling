"""The hosted worker contract (CONTRACTS §3.1, §3.2, §13.5), end to end.

Starts `python -m app.worker` as a real child process the way the supervisor
does, then checks the READY line, `/livez` with and without the worker token,
and that neither secret from stdin appears in the worker's environment or in
any file under its data folder.
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

ENGINE_DIR = Path(__file__).resolve().parents[1]
READY_RE = re.compile(r"^READY (\{.*\})$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


# --------------------------------------------------------------------------- #
# reading another process's environment
# --------------------------------------------------------------------------- #

def _windows_environment(pid: int) -> str:
    """The environment block of `pid`, read from its process parameters (x64)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class BasicInformation(ctypes.Structure):
        _fields_ = [("ExitStatus", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("AffinityMask", ctypes.c_void_p), ("BasePriority", ctypes.c_void_p),
                    ("UniqueProcessId", ctypes.c_void_p), ("InheritedFrom", ctypes.c_void_p)]

    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
                                                ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)     # QUERY_INFORMATION | VM_READ
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        def read(address: int, size: int) -> bytes:
            buffer = ctypes.create_string_buffer(size)
            done = ctypes.c_size_t()
            if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size,
                                              ctypes.byref(done)):
                raise OSError(ctypes.get_last_error(), "ReadProcessMemory failed")
            return buffer.raw[:done.value]

        info = BasicInformation()
        status = ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info),
                                                 ctypes.sizeof(info), None)
        if status != 0:
            raise OSError(status, "NtQueryInformationProcess failed")
        peb = read(int(info.PebBaseAddress), 0x40)
        parameters = int.from_bytes(peb[0x20:0x28], "little")
        header = read(parameters, 0x400)
        environment = int.from_bytes(header[0x80:0x88], "little")
        size = int.from_bytes(header[0x3F0:0x3F8], "little")
        if not 0 < size < 16 * 1_048_576:
            size = 64 * 1024
        return read(environment, size).decode("utf-16-le", errors="replace")
    finally:
        kernel32.CloseHandle(handle)


def child_environment(pid: int) -> str:
    if sys.platform == "win32":
        return _windows_environment(pid)
    return Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# the worker
# --------------------------------------------------------------------------- #

class Worker:
    def __init__(self, proc: subprocess.Popen, ready_line: str, secrets_in: dict[str, str],
                 app_dir: Path, lines: "queue.Queue[str]") -> None:
        self.proc = proc
        self.ready_line = ready_line
        self.ready = json.loads(READY_RE.match(ready_line).group(1)) if READY_RE.match(ready_line) else {}
        self.secrets = secrets_in
        self.app_dir = app_dir
        self.lines = lines

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:" + str(self.ready["listen"]).rsplit(":", 1)[1]


def _environment(app_dir: Path, scratch: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("STARLING_")}
    env.update({
        "STARLING_HOSTED": "1",
        "STARLING_APP_DIR": str(app_dir),
        "STARLING_LISTEN": "tcp:127.0.0.1:0",
        "STARLING_SERVICES_URL": "http://127.0.0.1:9",
        "STARLING_MODEL_QUALITY": "starling-quality",
        "STARLING_MODEL_FAST": "starling-fast",
        "STARLING_MODEL_VISION": "starling-vision",
        "STARLING_RESEARCH": "proxy",
        "STARLING_DISABLE_WARM": "1",
        "TZ": "UTC",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "PYTHONUNBUFFERED": "1",
        "LOCALAPPDATA": str(scratch),
    })
    return env


def services_token_for(tenant_id: str, expires_at: int | None = None) -> str:
    """A token in the CONTRACTS §4.3 shape. The engine never verifies signatures
    (it holds no key), so random signature bytes are a faithful stand-in."""
    expires = int(time.time()) + 24 * 3600 if expires_at is None else expires_at
    return (f"svc1.t-{tenant_id}.{expires}.{secrets.token_urlsafe(16)}."
            f"{secrets.token_urlsafe(32)}")


def fresh_secrets() -> dict[str, str]:
    tenant = str(uuid.uuid4())
    return {"worker_token": secrets.token_urlsafe(32),
            "services_token": services_token_for(tenant),
            "tenant_id": tenant}


@pytest.fixture()
def worker(tmp_path):
    app_dir = tmp_path / "tenant"
    app_dir.mkdir()
    secrets_in = fresh_secrets()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ENGINE_DIR,
                            env=_environment(app_dir, tmp_path / "localappdata"),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=flags)
    proc.stdin.write((json.dumps(secrets_in) + "\n").encode("utf-8"))
    proc.stdin.close()
    lines: "queue.Queue[str]" = queue.Queue()

    def pump() -> None:
        for raw in proc.stdout:
            lines.put(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    threading.Thread(target=pump, daemon=True).start()
    try:
        ready_line = lines.get(timeout=60)
    except queue.Empty:
        proc.kill()
        pytest.fail("the worker printed nothing within 60 seconds")
    running = Worker(proc, ready_line, secrets_in, app_dir, lines)
    try:
        yield running
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)


def test_the_ready_line_follows_the_contract(worker):
    assert READY_RE.match(worker.ready_line), worker.ready_line
    assert set(worker.ready) == {"listen", "pid", "engine_version"}
    assert re.match(r"^tcp:127\.0\.0\.1:\d+$", worker.ready["listen"])
    assert int(worker.ready["listen"].rsplit(":", 1)[1]) > 0, "the real port must be reported, not 0"
    assert isinstance(worker.ready["pid"], int) and worker.ready["pid"] > 0
    if sys.platform != "win32":
        # A Windows virtual environment starts the interpreter through a launcher
        # process, so only POSIX can compare the pid with the one we spawned.
        assert worker.ready["pid"] == worker.proc.pid
    assert SEMVER_RE.match(worker.ready["engine_version"]), worker.ready["engine_version"]


def test_livez_needs_the_worker_token(worker):
    token = worker.secrets["worker_token"]
    with httpx.Client(base_url=worker.base_url, trust_env=False, timeout=10) as client:
        ok = client.get("/livez", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert ok.json() == {"ok": True}

        for headers in ({}, {"Authorization": "Bearer wrong"},
                        {"Authorization": f"Bearer {worker.secrets['services_token']}"}):
            denied = client.get("/livez", headers=headers)
            assert denied.status_code == 404
            assert denied.content == b""

        # Removed routes are indistinguishable from routes that never existed.
        assert client.get("/health", headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_after_ready_stdout_carries_json_lines_only(worker):
    token = worker.secrets["worker_token"]
    with httpx.Client(base_url=worker.base_url, trust_env=False, timeout=10) as client:
        assert client.get("/livez", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    time.sleep(0.5)
    seen = []
    while not worker.lines.empty():
        seen.append(worker.lines.get_nowait())
    for line in seen:
        assert isinstance(json.loads(line), dict), line
        assert token not in line and worker.secrets["services_token"] not in line


def test_no_secret_reaches_the_environment_or_the_disk(worker):
    token = worker.secrets["worker_token"]
    with httpx.Client(base_url=worker.base_url, trust_env=False, timeout=10) as client:
        assert client.get("/livez", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    environment = child_environment(int(worker.ready["pid"]))
    # The reader must actually be reading the worker's block, or this proves nothing.
    assert "STARLING_LISTEN=tcp:127.0.0.1:0" in environment
    assert "STARLING_HOSTED=1" in environment
    for secret in (worker.secrets["worker_token"], worker.secrets["services_token"]):
        assert secret not in environment

    assert not (worker.app_dir / "starling.token").exists()
    for path in worker.app_dir.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        for secret in (worker.secrets["worker_token"], worker.secrets["services_token"]):
            assert secret.encode("utf-8") not in data, f"a secret was written to {path.name}"


def _refused(tmp_path, stdin: dict | bytes) -> subprocess.CompletedProcess:
    app_dir = tmp_path / "tenant"
    app_dir.mkdir(exist_ok=True)
    payload = stdin if isinstance(stdin, bytes) else (json.dumps(stdin) + "\n").encode("utf-8")
    return subprocess.run([sys.executable, "-m", "app.worker"], cwd=ENGINE_DIR,
                          env=_environment(app_dir, tmp_path / "localappdata"),
                          input=payload, capture_output=True, timeout=120)


def test_a_malformed_stdin_line_refuses_to_start(tmp_path):
    result = _refused(tmp_path, b'{"worker_token": "short", "services_token": "short", "tenant_id": "x"}\n')
    assert result.returncode != 0
    assert b"READY" not in result.stdout


def test_a_services_token_for_another_tenant_refuses_to_start(tmp_path):
    stdin = fresh_secrets()
    stdin["services_token"] = services_token_for(str(uuid.uuid4()))
    result = _refused(tmp_path, stdin)
    assert result.returncode != 0
    assert b"READY" not in result.stdout
    assert b"another tenant" in result.stderr
    assert stdin["services_token"].encode() not in result.stderr, "a rejected token must not be echoed"


def test_an_expired_services_token_refuses_to_start(tmp_path):
    stdin = fresh_secrets()
    stdin["services_token"] = services_token_for(stdin["tenant_id"], expires_at=int(time.time()) - 5)
    result = _refused(tmp_path, stdin)
    assert result.returncode != 0
    assert b"READY" not in result.stdout
    assert b"expired" in result.stderr


def test_a_stop_signal_exits_cleanly(worker):
    token = worker.secrets["worker_token"]
    with httpx.Client(base_url=worker.base_url, trust_env=False, timeout=10) as client:
        assert client.get("/livez", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    began = time.monotonic()
    worker.proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
    code = worker.proc.wait(timeout=30)
    assert code == 0
    assert time.monotonic() - began < 15.0
