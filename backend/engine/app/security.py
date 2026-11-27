from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from fastapi import Header, HTTPException

from .paths import app_dir

APP_DIR = app_dir()
# Development and test runs keep a bearer token in its own file. A hosted engine
# never does: its token arrives on stdin and lives in memory (`set_worker_token`).
TOKEN_PATH = APP_DIR / "starling.token"
_CONFIG_LOCK = threading.RLock()

# Read once and kept in memory.
_token_cache: str | None = None
# The gateway-issued worker token of a hosted engine. Never written anywhere.
_worker_token: str | None = None


def hosted() -> bool:
    """True when this process is a hosted per-customer engine worker."""
    return os.environ.get("STARLING_HOSTED", "").strip() == "1"


def ensure_app_dir() -> None:
    # Owner-only where mode bits mean something. A hosted engine's data folder is
    # created by the supervisor (mode 0700, owned by the engine's own user).
    APP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


def _write_secret(path: Path, text: str) -> None:
    """Write a credential atomically, readable only by this user.

    The mode is set on the temporary file before the replace, so the secret is
    never on disk with wider permissions, even briefly.
    """
    ensure_app_dir()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=APP_DIR)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def set_worker_token(token: str | None) -> None:
    """Install the gateway's worker token (hosted engines). Memory only."""
    global _worker_token, _token_cache
    with _CONFIG_LOCK:
        _worker_token = (token or "").strip() or None
        _token_cache = None


def ensure_token() -> str:
    """The bearer token every request must carry.

    Hosted: the worker token from stdin, or "" (every request is then denied)
    when none was supplied. Nothing is ever written to disk in hosted mode.

    Development and tests: read the token file, or mint and persist one.
    """
    global _token_cache
    with _CONFIG_LOCK:
        if _worker_token:
            return _worker_token
        if hosted():
            return ""
        if _token_cache:
            return _token_cache
        try:
            existing = TOKEN_PATH.read_text(encoding="utf-8-sig").strip()
        except OSError:
            existing = ""
        if not existing:
            existing = secrets.token_urlsafe(32)
        _write_secret(TOKEN_PATH, existing)
        _token_cache = existing
        return existing


def require_token(authorization: str | None = Header(default=None)) -> None:
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not token_is_valid(supplied):
        raise HTTPException(status_code=404)


def token_is_valid(supplied: str | None, expected: str | None = None) -> bool:
    """Constant-time comparison against the engine's token.

    Compared as bytes: `secrets.compare_digest` refuses non-ASCII str arguments,
    and one such byte in a header must be a rejection, never a crash.
    """
    expected = expected if expected is not None else ensure_token()
    if not expected or not supplied:
        return False
    return secrets.compare_digest(str(expected).encode("utf-8"),
                                  str(supplied).encode("utf-8"))
