"""The server clock (unix seconds).

Every expiry check in the server core reads time through `clock.now()` so tests can move
time forward with `monkeypatch.setattr(clock, "now", ...)` instead of sleeping.
"""
from __future__ import annotations

import time


def now() -> int:
    return int(time.time())
