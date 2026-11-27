"""Gateway lifecycle for billing: startup guard, serial event queue and recovery jobs.

* Production: `stripe-verify` runs before anything else and a mismatch refuses startup.
* The queue re-queues `received`, `failed` (and orphaned `processing`) events and starts
  its worker thread; the webhook route hands new events to `app.state.billing_queue`.
* The reconciler lists missed events hourly and resyncs every customer daily.
Without a Stripe key (development) nothing is started and webhooks answer 503.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Callable

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from . import processor, reconcile, setup
from .stripe_client import StripeClient, StripeError

log = logging.getLogger(__name__)

HOURLY_S = 3600
DAILY_S = 86_400
FIRST_EVENTS_RUN_S = 300
FIRST_RESYNC_RUN_S = 900


async def startup(app: FastAPI) -> None:
    settings, db = app.state.settings, app.state.db
    app.state.billing_queue = None
    app.state.billing_tasks = []
    client = getattr(app.state, "stripe_client", None)
    owns_client = False
    if client is None:
        if not settings.stripe_secret_key:
            log.warning("Stripe is not configured; billing webhooks and background jobs are disabled")
            return
        client = StripeClient(settings)
        owns_client = True
        app.state.stripe_client = client
    app.state.billing_owns_client = owns_client
    if settings.is_production:
        try:
            report = await run_in_threadpool(setup.verify, db, settings, client)
        except StripeError as exc:
            _close(app)
            raise RuntimeError(f"Stripe startup check failed: {exc}") from exc
        for warning in report.warnings:
            log.warning("stripe-verify: %s", warning)
        if report.problems:
            _close(app)
            raise RuntimeError("Stripe does not match plans.toml or settings:\n- " + "\n- ".join(report.problems))
    queue = processor.BillingQueue(db, settings, client)
    recovered = await run_in_threadpool(queue.recover)
    queue.start()
    app.state.billing_queue = queue
    if recovered:
        log.info("billing: re-queued %d stored events", recovered)
    if getattr(app.state, "billing_background_jobs", True):
        app.state.billing_tasks = [
            asyncio.create_task(_every(HOURLY_S, FIRST_EVENTS_RUN_S, "event reconciliation",
                                       lambda: reconcile.reconcile_events(db, settings, client, queue=queue))),
            asyncio.create_task(_every(DAILY_S, FIRST_RESYNC_RUN_S, "customer resync",
                                       lambda: reconcile.full_resync(db, settings, client))),
        ]


async def shutdown(app: FastAPI) -> None:
    tasks = list(getattr(app.state, "billing_tasks", []) or [])
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    app.state.billing_tasks = []
    queue = getattr(app.state, "billing_queue", None)
    if queue is not None:
        await run_in_threadpool(queue.stop)
        app.state.billing_queue = None
    _close(app)


async def _every(interval_s: float, first_run_s: float, name: str, job: Callable[[], Any]) -> None:
    await asyncio.sleep(first_run_s)
    while True:
        try:
            await run_in_threadpool(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("billing %s failed", name)
        await asyncio.sleep(interval_s)


def _close(app: FastAPI) -> None:
    if getattr(app.state, "billing_owns_client", False):
        client = getattr(app.state, "stripe_client", None)
        if client is not None:
            client.close()
        app.state.stripe_client = None
        app.state.billing_owns_client = False
