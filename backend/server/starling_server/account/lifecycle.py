"""Account lifecycle hooks (CONTRACTS section 14): finish account deletions that were interrupted.

A deletion that fails part-way (a crash, an engine or Stripe error) leaves the account `deleting`.
While the gateway runs, a background task calls `account.deletion.resume_pending_deletions` shortly
after startup and then every hour. It needs the engine pool, so this module is registered after
`engines.lifecycle`; while no pool exists it does nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .deletion import resume_pending_deletions

log = logging.getLogger(__name__)

FIRST_RUN_DELAY_S = 30
INTERVAL_S = 3600
TASK_ATTR = "account_deletion_task"


async def _run(app: Any) -> None:
    await asyncio.sleep(FIRST_RUN_DELAY_S)
    while True:
        pool = getattr(app.state, "engine_pool", None)
        if pool is not None:
            try:
                finished = await resume_pending_deletions(app.state.db, app.state.settings, pool)
                if finished:
                    log.info("finished %d interrupted account deletion(s)", len(finished))
            except Exception as exc:  # keep retrying on the next run
                log.error("resuming interrupted account deletions failed: %s", type(exc).__name__)
        await asyncio.sleep(INTERVAL_S)


async def startup(app: Any) -> None:
    setattr(app.state, TASK_ATTR, asyncio.create_task(_run(app), name="account-deletion-resume"))


async def shutdown(app: Any) -> None:
    task = getattr(app.state, TASK_ATTR, None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
