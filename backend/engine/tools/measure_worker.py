"""Measure one hosted engine worker: cold start to READY, /livez latency, memory.

    python tools/measure_worker.py [--runs N]

Starts `python -m app.worker` exactly as the supervisor would (non-secret
environment, fake secrets on stdin, a services URL nothing listens on), waits for
the READY line, calls `/livez`, reads the worker's resident memory, then stops it
gracefully (SIGTERM, or CTRL_BREAK on Windows) and reports how long that took.
No model or network call is made. Windows numbers are indicative only: the
production engine runs on Linux.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx

ENGINE_DIR = Path(__file__).resolve().parents[1]


def rss_mb(pid: int) -> float | None:
    """Resident set size of `pid` in MB (working set on Windows)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)   # QUERY_LIMITED_INFORMATION | VM_READ
        if not handle:
            return None
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            if not kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return round(counters.WorkingSetSize / 1_048_576, 1)
        finally:
            kernel32.CloseHandle(handle)
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def worker_env(app_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("STARLING_")}
    env.update({
        "STARLING_HOSTED": "1",
        "STARLING_APP_DIR": str(app_dir),
        # Nothing may fall back to the real per-user data folder.
        "LOCALAPPDATA": str(app_dir.parent),
        "XDG_DATA_HOME": str(app_dir.parent),
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
    })
    return env


def measure_once(python: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="starling-measure-") as scratch:
        app_dir = Path(scratch) / "tenant"
        app_dir.mkdir()
        tenant = str(uuid.uuid4())
        # A services token in the CONTRACTS §4.3 shape; the engine checks the shape
        # and the tenant, never the signature, so random signature bytes suffice.
        services_token = (f"svc1.t-{tenant}.{int(time.time()) + 24 * 3600}."
                          f"{secrets.token_urlsafe(16)}.{secrets.token_urlsafe(32)}")
        stdin_line = json.dumps({"worker_token": secrets.token_urlsafe(32),
                                 "services_token": services_token,
                                 "tenant_id": tenant}) + "\n"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        token = json.loads(stdin_line)["worker_token"]
        spawned = time.perf_counter()
        proc = subprocess.Popen([python, "-m", "app.worker"], cwd=ENGINE_DIR, env=worker_env(app_dir),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags)
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(stdin_line.encode("utf-8"))
        proc.stdin.close()
        lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=lambda: [lines.put(raw.decode("utf-8", "replace").rstrip())
                                         for raw in proc.stdout], daemon=True).start()
        try:
            ready_line = lines.get(timeout=60)
        except queue.Empty:
            proc.kill()
            raise SystemExit("no READY line within 60 s")
        ready_s = time.perf_counter() - spawned
        if not ready_line.startswith("READY "):
            proc.kill()
            raise SystemExit(f"unexpected first line: {ready_line[:200]}")
        ready = json.loads(ready_line[len("READY "):])
        port = int(str(ready["listen"]).rsplit(":", 1)[1])
        headers = {"Authorization": f"Bearer {token}"}
        latencies = []
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
            for _ in range(5):
                began = time.perf_counter()
                response = client.get("/livez", headers=headers)
                latencies.append((time.perf_counter() - began) * 1000.0)
                assert response.status_code == 200 and response.json() == {"ok": True}
        first_livez_s = ready_s + latencies[0] / 1000.0
        memory = rss_mb(int(ready["pid"]))
        stop_began = time.perf_counter()
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        try:
            exit_code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = "killed"
        return {"ready_s": round(ready_s, 2), "first_livez_s": round(first_livez_s, 2),
                "livez_ms_median": round(sorted(latencies)[len(latencies) // 2], 1),
                "rss_mb": memory, "stop_s": round(time.perf_counter() - stop_began, 2),
                "exit_code": exit_code, "engine_version": ready.get("engine_version")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    results = [measure_once(args.python) for _ in range(max(1, args.runs))]
    for index, result in enumerate(results, 1):
        print(f"run {index}: " + json.dumps(result))
    ready = sorted(float(r["ready_s"]) for r in results)
    memory = [float(r["rss_mb"]) for r in results if r["rss_mb"] is not None]
    print(f"platform {sys.platform}  cold start to READY: median {ready[len(ready) // 2]:.2f} s "
          f"(min {ready[0]:.2f}, max {ready[-1]:.2f})  RSS after /livez: "
          + (f"median {sorted(memory)[len(memory) // 2]:.1f} MB" if memory else "unavailable"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
