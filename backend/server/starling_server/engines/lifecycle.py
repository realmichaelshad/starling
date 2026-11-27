"""Engine pool lifecycle hooks (CONTRACTS section 14).

`startup(app)` builds the pool (the development spawner when `STARLING_ISOLATION=none`, the
supervisor client otherwise), starts its maintenance task (idle reaping, exit and memory
polling, operator stop requests) and sets `app.state.engine_pool`. A pool already placed on
`app.state.engine_pool` before startup (tests, e2e harnesses) is started instead of replaced.
`shutdown(app)` stops every engine and closes the pool.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from ..db import Database
from .pool import EnginePool

log = logging.getLogger(__name__)


def build_pool(settings: Settings, db: Database) -> EnginePool:
    if settings.isolation == "none":
        from .dev_spawner import DevSpawner

        spawner = DevSpawner(settings)
    else:
        from .supervisor_client import SupervisorClient

        spawner = SupervisorClient(settings)
    return EnginePool(settings, db, spawner)


async def startup(app: Any) -> None:
    pool = getattr(app.state, "engine_pool", None)
    if pool is None:
        pool = build_pool(app.state.settings, app.state.db)
        app.state.engine_pool = pool
    await pool.start()
    log.info("engine pool ready (%s spawner, at most %s engines)", pool.spawner.kind, pool.options.max_live)


async def shutdown(app: Any) -> None:
    pool = getattr(app.state, "engine_pool", None)
    if pool is not None:
        await pool.close()
