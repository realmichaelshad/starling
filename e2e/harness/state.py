"""The JSON state file: everything a test (or part 2's browser journeys) needs to drive the stack.

The file is written once the whole stack is ready and deleted by `down`. It is plain JSON so
Node can read it:

```json
{
  "run_id": "20260918-101112-ab12",
  "gateway_url": "http://127.0.0.1:53101",
  "services_url": "http://127.0.0.1:53102",
  "fake_openai_url": "http://127.0.0.1:53103/v1",
  "fake_stripe_url": "http://127.0.0.1:53104",
  "extension_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "owner_email": "owner@starling.example",
  "control_db": "...",
  "run_dir": "...",
  "log_dir": "...",
  "pids": {"gateway": 1, "services": 2, "fake_openai": 3, "fake_stripe": 4},
  "settings": {"consent_version": "...", "extension_min_version": "...", "plans": {...}}
}
```
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HarnessState:
    run_id: str
    gateway_url: str
    services_url: str
    fake_openai_url: str
    fake_openai_control: str
    fake_stripe_url: str
    fake_stripe_control: str
    extension_id: str
    owner_email: str
    run_dir: str
    log_dir: str
    data_dir: str
    control_db: str
    state_file: str
    fake_google_url: str = ""
    pids: dict[str, int] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    stripe: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_state(path: Path, state: HarnessState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(state.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_state(path: Path) -> HarnessState | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    known = {name for name in HarnessState.__dataclass_fields__}
    return HarnessState(**{key: value for key, value in data.items() if key in known})


def clear_state(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
