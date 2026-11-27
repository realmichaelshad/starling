"""FastAPI application for the services process: AI proxy, world service and admin diagnostics.

This process holds `AI_API_KEY` / `AI_API_KEY_SECONDARY` and opens the control database with its own
`Database` (its own writer thread). It is never exposed publicly: engines and the gateway reach it on a
unix socket (or loopback TCP in development). Every dependency that touches the network is injectable,
so tests run without any real provider or feed.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

import httpcore
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..config import Settings, ensure_valid, get_settings
from ..db import Database
from ..security.middleware import RequestIdMiddleware
from . import admin, proxy
from .capabilities import CapabilityTable
from .errors import ServiceError, error_response, service_error_response
from .metering import Metering, clear_stale_trips
from .upstream import UpstreamClient
from .world import fixtures as world_fixtures
from .world import routes as world_routes
from .world.news import HttpGetter, NewsPool
from .world.safe_fetch import Resolver, SafeFetcher
from .world.search import WebSearchService, ensure_search_valid
from .world.summary import SummaryService
from .world.x_post import XPostReader

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    world_http: HttpGetter | None = None,
    resolver: Resolver | None = None,
    network_backend: httpcore.AsyncNetworkBackend | None = None,
    ssl_context: ssl.SSLContext | None = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    start_background: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    # httpx logs every request URL at INFO and httpcore traces connections at DEBUG. World fetch URLs
    # come from customers' research, so these loggers report warnings only in the services process.
    # The anthropic SDK (AI_PROVIDER=anthropic) logs request options, prompts included, at DEBUG
    # when ANTHROPIC_LOG asks it to, through its own logger and httpx2's.
    for name in ("httpx", "httpcore", "anthropic", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    problems = ensure_valid(settings, "services") + ensure_search_valid(settings)
    for problem in problems:
        log.warning("configuration: %s", problem)
    if settings.services_token_key_ephemeral:
        log.warning(
            "STARLING_SERVICES_TOKEN_KEY is not set and no gateway secret is available; "
            "tokens minted by the gateway will be refused"
        )

    database = Database(settings.control_db_path)
    capabilities = CapabilityTable()
    metering = Metering(database, settings, clock=clock)
    upstream = UpstreamClient(
        settings, capabilities, transport=upstream_transport, sleep=sleep, monotonic=monotonic, clock=clock
    )
    chat_proxy = proxy.ChatProxy(settings, metering, upstream, capabilities, monotonic=monotonic)
    # STARLING_WORLD_OFFLINE (development and e2e only; refused in production by `validate`):
    # no news refresh task, and fetch, image and summary fail fast without any outbound
    # connection (CONTRACTS 4.2 and 13.11).
    offline = bool(settings.world_offline)
    # STARLING_WORLD_FIXTURES (e2e only, and only while offline; refused in production by
    # `validate`): listed pages are fetched from a file and the news pool is seeded from it.
    fixtures = world_fixtures.load_if_offline(settings)
    fetcher = SafeFetcher(
        resolver=resolver, network_backend=network_backend, ssl_context=ssl_context, offline=offline,
        fixtures=fixtures,
    )
    pool = NewsPool(world_http or fetcher, clock=clock, monotonic=monotonic, sleep=sleep)
    if fixtures is not None and fixtures.news:
        pool.seed(fixtures.news)
    summaries = SummaryService(pool, chat_proxy, settings, clock=clock, offline=offline)
    # W4: the provider's web search tool, through the same upstream client and metering as a draft.
    search = WebSearchService(settings, metering, upstream, capabilities, database, clock=clock, monotonic=monotonic)
    world = world_routes.World(
        fetcher, pool, summaries, world_routes.FetchRateLimiter(monotonic=monotonic), search=search,
        # The writing page's links to posts on X: X's oEmbed endpoint, through the same safe fetcher.
        x_posts=XPostReader(fetcher, monotonic=monotonic),
        x_post_limiter=world_routes.FetchRateLimiter(per_minute=world_routes.X_POST_LOOKUPS_PER_MINUTE,
                                                     monotonic=monotonic),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cleared = await database.write_async(lambda conn: clear_stale_trips(conn, settings, int(clock())))
        if cleared:
            log.info("cleared %d budget stop(s) that are below the configured limits", cleared)
        if offline:
            log.warning("STARLING_WORLD_OFFLINE is set: the news pool never refreshes and no page is fetched")
        elif start_background:
            app.state.news_task = asyncio.create_task(pool.run(), name="news-pool-refresh")
        try:
            yield
        finally:
            await close_services(app)

    app = FastAPI(title="Starling services", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    state = app.state
    state.settings = settings
    state.db = database
    state.capabilities = capabilities
    state.metering = metering
    state.upstream = upstream
    state.proxy = chat_proxy
    state.world = world
    state.clock = clock
    state.monotonic = monotonic
    state.config_problems = problems
    state.started_at = int(clock())
    state.news_task = None
    state.world_offline = offline
    state.closed = False

    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return service_error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code in (404, 405):
            return error_response(404, "not_found", "not found")
        if 400 <= exc.status_code < 500:
            return error_response(exc.status_code, "bad_request", "the request could not be handled")
        return error_response(exc.status_code, "server_error", "the services process failed")

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(400, "bad_request", "the request is not valid")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled %s on %s", type(exc).__name__, request.url.path)
        return error_response(500, "server_error", "the services process failed")

    @app.get("/livez")
    async def livez() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(proxy.router)
    app.include_router(world_routes.router)
    app.include_router(admin.router)
    return app


async def close_services(app: FastAPI) -> None:
    """Stop background work and release the upstream client and database (idempotent)."""
    state = app.state
    if getattr(state, "closed", True):
        return
    state.closed = True
    task = getattr(state, "news_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    with contextlib.suppress(Exception):
        await state.upstream.aclose()
    state.db.close()


def close_services_sync(app: FastAPI) -> None:
    """For callers that built the app without running its lifespan (tests, tools)."""
    if getattr(app.state, "closed", True):
        return
    asyncio.run(close_services(app))
