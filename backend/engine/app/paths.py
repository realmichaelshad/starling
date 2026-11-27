"""Where this engine keeps its data.

A hosted engine runs on the server, one per customer, and the supervisor always sets
`STARLING_APP_DIR` to that customer's folder (`/data/tenants/<number>/`, mode 0700,
CONTRACTS §3.1). The per-OS desktop locations the engine used to fall back to (Windows
%LOCALAPPDATA%, macOS Application Support, XDG) belonged to the desktop companion and are
gone (API-native pass, 2026-09-22). Without the variable, a development or test run uses
`backend/engine/.data`, never a folder in the user's home.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Starling"
DEV_DATA_DIR = Path(__file__).resolve().parents[1] / ".data"


def app_dir() -> Path:
    """The directory holding this customer's data. Never creates it.

    `STARLING_APP_DIR` must be absolute: it decides where the database and (in development)
    the bearer token live, so a relative value would depend on the launcher's working
    directory. A relative value is ignored in favour of the development folder.
    """
    override = os.environ.get("STARLING_APP_DIR")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
    return DEV_DATA_DIR
