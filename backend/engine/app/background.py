"""This account's background AI work: the voice card and the topic research. Never on a press.

API-NATIVE PASS (2026-09-22). Both jobs are slow-changing and cost money, so neither may
ever run while somebody waits:

  * after a press finishes, `schedule_after_press` starts one run on a daemon thread, in an
    EMPTY context (no request id, so every call it makes is metered as the account's
    background work: `X-Starling-Bucket: background`), a few seconds later and at most once
    every `AFTER_PRESS_DEBOUNCE_S`;
  * the hosted worker also calls `run_due_jobs` on a slow loop (`worker._background_loop`),
    which does nothing unless something is due and the account is active.

Each job decides for itself whether it is due (`voice_card.due`, `topic_research.due`); the
services process enforces the money side (the background share of the plan's ceiling, the
account's ceiling, the daily search cap). Enabled on a hosted worker; elsewhere only with
`STARLING_BACKGROUND_JOBS=1` (so a test run never spends a background call by accident).
"""
from __future__ import annotations

import contextvars
import os
import threading
import time
from typing import Any

AFTER_PRESS_DELAY_S = 3.0
AFTER_PRESS_DEBOUNCE_S = 120.0
LOOP_INTERVAL_S = 15 * 60.0

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"running": False, "scheduled_at": 0.0, "last": None}


def enabled() -> bool:
    value = os.environ.get("STARLING_BACKGROUND_JOBS")
    if value is not None and value.strip():
        return value.strip().lower() in ("1", "on", "true", "yes")
    return os.environ.get("STARLING_HOSTED") == "1"


def run_due_jobs(*, force: bool = False, llm: Any = None) -> dict:
    """Run whatever is due, one job after the other. Never raises; returns what ran."""
    with _LOCK:
        if _STATE["running"]:
            return {"skipped": "already running"}
        _STATE["running"] = True
    out: dict[str, Any] = {}
    try:
        if llm is None:
            try:
                from .llm import ModelClient
                llm = ModelClient()
            except Exception:                                # noqa: BLE001
                llm = None
        try:
            from .voice_card import refresh_if_due
            card = refresh_if_due(llm, force=force) if llm is not None else None
            out["voice_card"] = {"made": bool(card), "version": (card or {}).get("version")}
        except Exception as error:                           # noqa: BLE001
            out["voice_card"] = {"error": type(error).__name__}
        try:
            from .topic_research import mark_active, run_if_due
            if force:
                mark_active()
            out["topic_research"] = run_if_due()
        except Exception as error:                           # noqa: BLE001
            out["topic_research"] = {"error": type(error).__name__}
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["last"] = {"at": time.time(), **out}
    return out


def warm_caches() -> dict:
    """Build the account's no-AI caches once, before the first press needs them.

    The first ✦ of a fresh engine paid ~0.25 s (simulated) for work every later press
    reads from a cache: the account profile rebuild, the niche read and the shared news
    pool. None of it calls a model or the web search, so it costs nothing to do at start.
    Never raises; returns what it warmed.
    """
    out: dict[str, Any] = {}
    for name, warm in (("profile", _warm_profile), ("niche", _warm_niche), ("news", _warm_news)):
        started = time.monotonic()
        try:
            warm()
            out[name] = round(time.monotonic() - started, 3)
        except Exception as error:                           # noqa: BLE001
            out[name] = type(error).__name__
    return out


def _warm_profile() -> None:
    from .profile import ensure_profile
    ensure_profile()


def _warm_niche() -> None:
    from .niche import current
    current()


def _warm_news() -> None:
    from .news import headlines
    headlines()


def schedule_after_press() -> bool:
    """A press just finished: start the due background jobs soon, off its thread and context."""
    try:
        from .topic_research import mark_active
        mark_active()
    except Exception:                                        # noqa: BLE001
        pass
    if not enabled():
        return False
    now = time.monotonic()
    with _LOCK:
        if _STATE["running"] or now - float(_STATE["scheduled_at"]) < AFTER_PRESS_DEBOUNCE_S:
            return False
        _STATE["scheduled_at"] = now

    def _later() -> None:
        time.sleep(AFTER_PRESS_DELAY_S)
        run_due_jobs()

    # An empty context: nothing of the press (its request id, its deadline) travels along.
    thread = threading.Thread(target=contextvars.Context().run, args=(_later,),
                              name="starling-background", daemon=True)
    thread.start()
    return True


def last_run() -> dict | None:
    with _LOCK:
        return dict(_STATE["last"]) if _STATE["last"] else None


__all__ = ["enabled", "last_run", "run_due_jobs", "schedule_after_press", "warm_caches"]
