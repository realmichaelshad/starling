"""Repository layout and the per-run folder, resolved without importing the server package."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = REPO_ROOT / "e2e"
RUN_ROOT = E2E_DIR / ".run"
SERVER_DIR = REPO_ROOT / "backend" / "server"
ENGINE_DIR = REPO_ROOT / "backend" / "engine"
WEB_DIR = REPO_ROOT / "web"
PLANS_FILE = SERVER_DIR / "config" / "plans.toml"
AI_PRICING_FILE = SERVER_DIR / "config" / "ai_pricing.json"

DEFAULT_STATE_FILE = RUN_ROOT / "state.json"


def venv_python() -> str:
    """The interpreter every Starling process must run under.

    The harness itself is normally started with it; when it is not (a bare `python -m
    e2e.harness`), the repository venv is used so the engine spawner, which re-runs
    `sys.executable`, still finds the server dependencies.
    """
    if sys.platform == "win32":
        candidate = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
    running = Path(sys.executable).resolve()
    if candidate.is_file() and running != candidate.resolve():
        return str(candidate)
    return sys.executable


def add_server_to_path() -> None:
    """Make `starling_server` and the workstream test packages importable in this process."""
    for entry in (str(SERVER_DIR),):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def run_dir(run_id: str) -> Path:
    return RUN_ROOT / run_id


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
