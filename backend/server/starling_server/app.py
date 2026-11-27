"""FastAPI application factory for the public gateway process."""
from __future__ import annotations

import importlib
import importlib.util
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.templating import Jinja2Templates

from . import logs
from .config import Settings, ensure_valid, get_settings
from .db import Database
from .security.errors import SHAPED_ERRORS, ApiError, shaped_exception_handler
from .security.middleware import RequestIdMiddleware, SecurityHeadersMiddleware, StaticAssetsMiddleware

log = logging.getLogger(__name__)

# Each module exposes `router = APIRouter()` and is owned by one workstream
# (docs/architecture/CONTRACTS.md section 14).
ROUTER_MODULES: tuple[str, ...] = (
    "system.routes",
    "web.pages",
    "web.write",
    "auth.routes",
    "auth.google_routes",
    "account.routes",
    "devices.routes",
    "api.session_routes",
    "api.engine_routes",
    "billing.routes",
    "admin.routes",
)

# Optional lifecycle hooks: a module may define `async def startup(app)` and
# `async def shutdown(app)` to start and stop background work (sweepers, queues, pools).
LIFECYCLE_MODULES: tuple[str, ...] = (
    "usage.lifecycle",
    "billing.lifecycle",
    "engines.lifecycle",
    "account.lifecycle",  # after engines: resuming deletions needs the engine pool
    "email.lifecycle",
    "admin.lifecycle",  # owner alert emails and the request_errors retention sweep
)


def _load_optional(module_name: str):
    target = f"{__package__}.{module_name}"
    try:
        return importlib.import_module(target)
    except ModuleNotFoundError as exc:
        # Absent hook module (or package): fine. A missing import inside it: a real error.
        if exc.name and (target == exc.name or target.startswith(exc.name + ".")):
            return None
        raise


async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    """Unknown API URLs get the contract error body (CONTRACTS 6.3); website URLs get the 404 page."""
    if request.url.path.startswith("/api/"):
        if exc.status_code == 404:
            return ApiError("not_found", 404, "Not found.").to_response()
        if exc.status_code == 405:
            return ApiError("bad_request", 405, "This method is not allowed here.").to_response()
    from .web.render import http_exception_handler  # imported late: web.render imports most packages

    return await http_exception_handler(request, exc)


async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    if request.url.path.startswith("/api/"):
        return ApiError("bad_request", 400, "The request is not valid.").to_response()
    from fastapi.exception_handlers import request_validation_exception_handler

    return await request_validation_exception_handler(request, exc)


def website_templates(web_dir: Path) -> Jinja2Templates | None:
    """Templates rendered with the website's own Jinja environment (`web/templating.py`).

    The templates rely on its filters, tests and globals, so a plain environment cannot compile them.
    Returns None when the website is not installed; `/readyz` then reports the missing templates.
    """
    templates_dir = web_dir / "templates"
    templating_file = web_dir / "templating.py"
    if not templates_dir.is_dir():
        return None
    if not templating_file.is_file():
        log.error("website templates found without %s; pages cannot render", templating_file)
        return None
    spec = importlib.util.spec_from_file_location("starling_web_templating", templating_file)
    if spec is None or spec.loader is None:
        log.error("cannot load %s", templating_file)
        return None
    templating = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(templating)
    return Jinja2Templates(env=templating.create_environment(web_dir))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.is_production:  # development and e2e keep uvicorn's readable lines
        logs.configure(settings.log_level)
    problems = ensure_valid(settings, "gateway")
    for problem in problems:
        log.warning("configuration: %s", problem)
    if settings.secret_key_ephemeral:
        log.warning("STARLING_SECRET_KEY is not set; using an ephemeral key (sessions reset on restart)")

    database = Database(settings.control_db_path)
    lifecycle = [m for m in (_load_optional(name) for name in LIFECYCLE_MODULES) if m is not None]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = []
        try:
            for module in lifecycle:
                hook = getattr(module, "startup", None)
                if hook is not None:
                    await hook(app)
                    started.append(module)
            yield
        finally:
            for module in reversed(started):
                hook = getattr(module, "shutdown", None)
                if hook is not None:
                    try:
                        await hook(app)
                    except Exception:  # keep shutting the rest down
                        log.exception("shutdown hook failed in %s", module.__name__)
            database.close()

    app = FastAPI(
        title="Starling",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.config_problems = problems

    app.state.templates = website_templates(settings.web_dir)
    static_dir = settings.web_dir / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Middleware added last runs first: request id wraps the security headers, which wrap the
    # static files' compression and caching.
    app.add_middleware(StaticAssetsMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(RequestIdMiddleware)

    # Every router gets the contract error bodies, including routers that don't use StarlingRoute.
    for exc_type in SHAPED_ERRORS:
        app.add_exception_handler(exc_type, shaped_exception_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)

    for module_name in ROUTER_MODULES:
        module = importlib.import_module(f"{__package__}.{module_name}")
        app.include_router(module.router)
    return app
