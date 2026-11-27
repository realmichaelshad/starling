"""Background email sender for SMTP mode (started and stopped by the app lifespan)."""
from __future__ import annotations

import asyncio
import logging

import anyio
from fastapi import FastAPI

from . import sender

log = logging.getLogger(__name__)

POLL_S = 15.0


async def startup(app: FastAPI) -> None:
    settings = app.state.settings
    if settings.email_mode != "smtp":
        return
    if not settings.smtp_host:
        log.warning("EMAIL_MODE is smtp but SMTP_HOST is empty; queued email will not be delivered")
        return
    app.state.email_sender_task = asyncio.create_task(_run(app), name="starling-email-sender")


async def shutdown(app: FastAPI) -> None:
    task = getattr(app.state, "email_sender_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    app.state.email_sender_task = None


async def _run(app: FastAPI) -> None:
    settings = app.state.settings
    db = app.state.db
    while True:
        try:
            await anyio.to_thread.run_sync(sender.deliver_pending, db, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the loop alive; delivery errors are recorded per message
            log.error("email sender iteration failed: %s", type(exc).__name__)
        await sender.wait_for_work(POLL_S)
