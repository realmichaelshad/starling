"""Engine spawner interface: what the pool needs from whatever starts engine processes.

Two implementations share it (CONTRACTS sections 3.1 and 5):

* `dev_spawner.DevSpawner` starts engines as child processes of the gateway
  (`STARLING_ISOLATION=none`, development and e2e only).
* `supervisor_client.SupervisorClient` asks the root supervisor over
  `/run/starling/spawner.sock` (production, one OS user per customer).

This module also holds what both sides must agree on exactly: the READY line, the
engine environment, identifier and token validation, and the log sanitiser that
keeps engine output (which could carry customer text) out of gateway logs.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

import httpx

READY_TIMEOUT_S = 30.0
READY_PREFIX = "READY "
MAX_PRE_READY_BYTES = 64 * 1024
MAX_LOG_LINE_BYTES = 64 * 1024
TENANT_SEQ_MIN = 1
TENANT_SEQ_MAX = 10_000_000

WORKER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TCP_LISTEN_RE = re.compile(r"^tcp:127\.0\.0\.1:([0-9]{1,5})$")
_UNIX_LISTEN_RE = re.compile(r"^unix:(/[A-Za-z0-9._/-]{1,200})$")
_ENGINE_VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,64}$")
_LOG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_EXCEPTION_LINE_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_.]{0,120}(?:Error|Exception|Exit|Interrupt|Warning))(?::|\s*$)")

# Names that carry secrets somewhere in the stack. None may reach an engine
# (CONTRACTS section 2.1 and security invariant 5).
SECRET_ENV_NAMES = frozenset({
    "STARLING_SECRET_KEY", "STARLING_SECRET_KEY_PREVIOUS", "STARLING_SERVICES_TOKEN_KEY", "AI_API_KEY", "AI_API_KEY_SECONDARY",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET_PREVIOUS",
    "SMTP_USERNAME", "SMTP_PASSWORD", "BACKUP_PASSPHRASE", "GOOGLE_CLIENT_SECRET",
})
SECRET_ENV_PREFIXES = ("AI_API_KEY", "STRIPE_", "SMTP_")
# Values of these are searched for inside every engine variable as a second check.
_SECRET_VALUE_NAMES = frozenset(SECRET_ENV_NAMES - {"SMTP_USERNAME"})
_MIN_SECRET_VALUE_LEN = 12

# Operating-system variables a child process needs to start at all. Nothing else from
# the gateway environment is inherited.
POSIX_BASE_ENV = ("PATH", "LANG", "LC_ALL")
WINDOWS_BASE_ENV = (
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)


class SpawnError(RuntimeError):
    """An engine could not be started, stopped or reached through its spawner."""


@dataclass(frozen=True, repr=False)
class EngineSecrets:
    worker_token: str
    services_token: str

    def __repr__(self) -> str:  # never print secrets by accident
        return "EngineSecrets(<redacted>)"

    def stdin_line(self, tenant_id: str) -> bytes:
        payload = {"worker_token": self.worker_token, "services_token": self.services_token, "tenant_id": tenant_id}
        return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SpawnedEngine:
    tenant_seq: int
    tenant_id: str
    pid: int
    listen: str
    started_at: int
    engine_version: str | None = None


@dataclass(frozen=True)
class EngineStatus:
    tenant_seq: int
    pid: int
    rss_mb: int | None
    started_at: int
    over_soft_limit: bool = False


# (tenant_seq, return code or None, reason) - called on the event loop thread.
ExitCallback = Callable[[int, "int | None", str], None]


class Spawner(Protocol):
    kind: str
    # True when the spawner reports engine exits through the exit callback; otherwise the
    # pool detects exits by polling `status()`.
    reports_exits: bool

    def set_exit_callback(self, callback: ExitCallback | None) -> None: ...

    async def spawn(self, tenant_seq: int, tenant_id: str, secrets: EngineSecrets) -> SpawnedEngine: ...

    async def stop(self, tenant_seq: int, grace_s: float = 10.0) -> None: ...

    async def delete_data(self, tenant_seq: int) -> None: ...

    async def status(self) -> list[EngineStatus]: ...

    async def is_running(self, tenant_seq: int) -> bool: ...

    def transport_for(self, listen: str) -> tuple[str, httpx.AsyncBaseTransport | None]: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- validation


def new_worker_token() -> str:
    """A fresh per-spawn worker token: 32 random bytes, base64url, 43 characters."""
    token = secrets.token_urlsafe(32)
    if not WORKER_TOKEN_RE.match(token):  # token_urlsafe(32) always matches; refuse anything else
        raise SpawnError("could not create a worker token")
    return token


def validate_tenant_seq(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not TENANT_SEQ_MIN <= value <= TENANT_SEQ_MAX:
        raise ValueError("tenant_seq must be an integer from 1 to 10,000,000")
    return value


def validate_tenant_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("tenant_id must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("tenant_id must be a UUID") from exc
    if str(parsed) != value:
        raise ValueError("tenant_id must be a canonical lowercase UUID")
    return value


def validate_worker_token(value: object) -> str:
    if not isinstance(value, str) or not WORKER_TOKEN_RE.match(value):
        raise ValueError("worker_token must be 43 base64url characters")
    return value


def parse_ready_line(line: str) -> tuple[str, int, str | None]:
    """Parse `READY {"listen", "pid", "engine_version"}`; raise SpawnError when malformed."""
    if not line.startswith(READY_PREFIX):
        raise SpawnError("engine did not print a READY line")
    try:
        payload = json.loads(line[len(READY_PREFIX):])
    except ValueError as exc:
        raise SpawnError("engine READY line is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SpawnError("engine READY line is not a JSON object")
    listen, pid, version = payload.get("listen"), payload.get("pid"), payload.get("engine_version")
    if not isinstance(listen, str):
        raise SpawnError("engine READY line has no listen address")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise SpawnError("engine READY line has no valid pid")
    if not isinstance(version, str) or not _ENGINE_VERSION_RE.match(version):
        version = None
    return listen, pid, version


def tcp_listen_port(listen: str) -> int:
    """Port of a `tcp:127.0.0.1:PORT` address. Any other host is refused: the gateway sends
    worker tokens to this address, so an engine must not be able to point it elsewhere."""
    match = _TCP_LISTEN_RE.match(listen)
    port = int(match.group(1)) if match else 0
    if not 1 <= port <= 65535:
        raise SpawnError("engine listen address must be tcp:127.0.0.1:<port>")
    return port


def unix_listen_path(listen: str) -> str:
    match = _UNIX_LISTEN_RE.match(listen)
    if not match or "/../" in match.group(1) or match.group(1).endswith("/.."):
        raise SpawnError("engine listen address must be unix:<absolute socket path>")
    return match.group(1)


def engine_socket_path(run_dir: Path | str, tenant_seq: int) -> str:
    return f"{str(run_dir).rstrip('/')}/engines/{validate_tenant_seq(tenant_seq)}/engine.sock"


def base_url_and_transport(listen: str) -> tuple[str, httpx.AsyncBaseTransport | None]:
    if listen.startswith("tcp:"):
        return f"http://127.0.0.1:{tcp_listen_port(listen)}", None
    if listen.startswith("unix:"):
        # verify=False: a Unix socket never carries TLS, and loading the certificate store for a
        # context nobody uses costs about a quarter of a second on the gateway's event loop.
        return "http://engine", httpx.AsyncHTTPTransport(uds=unix_listen_path(listen), verify=False)
    raise SpawnError("unsupported engine listen address")


# --------------------------------------------------------------------------- environment


def engine_environment(*, app_dir: Path | str, listen: str, services_url: str) -> dict[str, str]:
    """The non-secret engine environment of CONTRACTS section 3.1."""
    return {
        "STARLING_HOSTED": "1",
        "STARLING_APP_DIR": str(app_dir),
        "STARLING_LISTEN": listen,
        "STARLING_SERVICES_URL": services_url,
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
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def is_secret_env_name(name: str) -> bool:
    upper = name.upper()
    return upper in SECRET_ENV_NAMES or upper.startswith(SECRET_ENV_PREFIXES)


def assert_engine_environment_clean(env: Mapping[str, str], parent_environ: Mapping[str, str]) -> None:
    """Refuse to start an engine whose environment names or contains a secret."""
    for name in env:
        if is_secret_env_name(name):
            raise SpawnError(f"refusing to pass {name} to an engine")
    values = [
        value for name, value in parent_environ.items()
        if name.upper() in _SECRET_VALUE_NAMES and len(value.strip()) >= _MIN_SECRET_VALUE_LEN
    ]
    for name, value in env.items():
        if any(secret in value for secret in values):
            raise SpawnError(f"refusing to pass a secret value to an engine in {name}")


def read_rss_mb(pid: int) -> int | None:
    """Resident memory of a process from `/proc/<pid>/status` (Linux only; None elsewhere)."""
    try:
        with open(f"/proc/{int(pid)}/status", "rb") as fh:
            for raw in fh:
                if raw.startswith(b"VmRSS:"):
                    parts = raw.split()
                    return int(parts[1]) // 1024 if len(parts) >= 2 and parts[1].isdigit() else None
    except (OSError, ValueError):
        return None
    return None


# --------------------------------------------------------------------------- logs


def sanitize_log_line(raw: bytes) -> tuple[int, str]:
    """(logging level, text) for one engine stdout line, keeping only short identifier-like
    fields. Free text never passes, so a misbehaving engine cannot put prompts or drafts
    into gateway logs."""
    size = len(raw)
    if size > MAX_LOG_LINE_BYTES:
        return logging.WARNING, f"oversized stdout line ({size} bytes)"
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return logging.INFO, f"non-JSON stdout line ({size} bytes)"
    if not isinstance(payload, dict):
        return logging.INFO, f"non-object JSON stdout line ({size} bytes)"
    fields = []
    for key in ("level", "event", "logger", "code", "status", "route", "duration_ms"):
        value = payload.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            fields.append(f"{key}={value}")
        elif isinstance(value, str) and _LOG_VALUE_RE.match(value):
            fields.append(f"{key}={value}")
    level_name = payload.get("level")
    level = logging.WARNING if isinstance(level_name, str) and level_name.lower() in ("warning", "error", "critical") else logging.INFO
    return level, " ".join(fields) or f"log line without loggable fields ({size} bytes)"


def sanitize_stderr_line(raw: bytes) -> tuple[int, str] | None:
    """Exception class names only; everything else on stderr is counted, not logged."""
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("Traceback (most recent call last)"):
        return logging.WARNING, "traceback"
    match = _EXCEPTION_LINE_RE.match(text)
    if match:
        return logging.WARNING, f"exception {match.group(1)}"
    return None
