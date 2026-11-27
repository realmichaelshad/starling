"""/healthz, /readyz, /robots.txt, /sitemap.xml, /.well-known/security.txt. Owner: server core workstream.

Also `GET /_dev/outbox?to=<address>` (development and e2e only): {"messages": [{"id", "to_email",
"template", "subject", "body", "created_at", "sent_at"}]}, oldest first. In production it answers
exactly like a route that does not exist.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import anyio
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from xml.sax.saxutils import escape

from ..db import migrations
from ..email.sender import outbox_messages
from ..security import clock
from ..security.errors import ApiError, StarlingRoute
from ..web.render import DOC_PAGE_RE, LEGAL_PAGES, required_templates

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

DEV_ENVIRONMENTS = ("development", "e2e")
ROBOTS_DISALLOW = (
    "/account", "/admin", "/api/", "/connect", "/billing", "/verify-email", "/reset-password", "/_dev/",
    "/write", "/offline",
)
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/healthz")
async def healthz() -> Response:
    return JSONResponse({"ok": True}, headers=NO_STORE)


def readiness_checks(app: FastAPI) -> dict[str, bool]:
    """Cheap readiness probes; results are booleans only (details go to the log, never secrets)."""
    settings = app.state.settings
    db = app.state.db
    checks: dict[str, bool] = {}
    try:
        db.write(
            lambda conn: conn.execute(
                "INSERT INTO rate_limits (bucket, key, window_start, count) VALUES ('readyz', 'probe', 0, 1) "
                "ON CONFLICT (bucket, key, window_start) DO UPDATE SET count = 1"
            ),
            timeout=5,
        )
        checks["database"] = True
    except Exception as exc:
        log.error("readiness: control database is not writable (%s)", type(exc).__name__)
        checks["database"] = False
    try:
        with db.read() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        checks["schema"] = int(row[0] or 0) == migrations.latest_version()
    except Exception as exc:
        log.error("readiness: schema version unavailable (%s)", type(exc).__name__)
        checks["schema"] = False
    templates_dir = settings.web_dir / "templates"
    missing = [name for name in required_templates() if not (templates_dir / name).is_file()]
    if missing:
        log.warning("readiness: %d website templates missing: %s", len(missing), ", ".join(missing[:25]))
    checks["templates"] = not missing
    problems = getattr(app.state, "config_problems", [])
    checks["config"] = not (settings.is_production and problems)
    return checks


READYZ_CACHE_S = 5.0


async def _cached_readiness(app: FastAPI) -> dict[str, bool]:
    """`/readyz` needs no account and its probe WRITES to the database (that is the point of it), so
    the answer is reused for a few seconds: a flood of probes costs one write per interval."""
    cached = getattr(app.state, "readyz_cache", None)
    if cached is not None and time.monotonic() - cached[0] < READYZ_CACHE_S:
        return cached[1]
    checks = await anyio.to_thread.run_sync(readiness_checks, app)
    app.state.readyz_cache = (time.monotonic(), checks)
    return checks


@router.get("/readyz")
async def readyz(request: Request) -> Response:
    checks = await _cached_readiness(request.app)
    ok = all(checks.values())
    return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503, headers=NO_STORE)


@router.get("/robots.txt")
async def robots(request: Request) -> Response:
    settings = request.app.state.settings
    if settings.is_production and not settings.payments_rehearsal:
        lines = ["User-agent: *", "Allow: /"]
        lines += [f"Disallow: {path}" for path in ROBOTS_DISALLOW]
        lines += ["", f"Sitemap: {settings.public_url}/sitemap.xml"]
    else:
        lines = ["User-agent: *", "Disallow: /"]
    return PlainTextResponse("\n".join(lines) + "\n", headers={"Cache-Control": "public, max-age=3600"})


def public_paths(request: Request) -> list[str]:
    settings = request.app.state.settings
    paths = ["/", "/pricing"] + [f"/{name}" for name in LEGAL_PAGES]
    docs_dir = settings.web_dir / "templates" / "docs"
    if docs_dir.is_dir():
        paths += sorted(f"/docs/{path.stem}" for path in docs_dir.glob("*.html") if DOC_PAGE_RE.match(path.stem))
    return paths


@router.get("/sitemap.xml")
async def sitemap(request: Request) -> Response:
    base = request.app.state.settings.public_url
    entries = "".join(f"  <url><loc>{escape(base + path)}</loc></url>\n" for path in public_paths(request))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}</urlset>\n"
    )
    return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/.well-known/security.txt")
async def security_txt(request: Request) -> Response:
    settings = request.app.state.settings
    expires = datetime.fromtimestamp(clock.now() + 365 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"Contact: mailto:{settings.support_email}",
        f"Expires: {expires}",
        "Preferred-Languages: en",
        f"Canonical: {settings.public_url}/.well-known/security.txt",
    ]
    return PlainTextResponse("\n".join(lines) + "\n", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/_dev/outbox")
async def dev_outbox(request: Request) -> Response:
    settings = request.app.state.settings
    if settings.env not in DEV_ENVIRONMENTS or not settings.dev_tools_enabled:
        raise HTTPException(status_code=404)  # identical to an unknown route
    to_email = request.query_params.get("to", "").strip()
    if not to_email or len(to_email) > 320:
        raise ApiError("bad_request", 400, "Pass ?to=<email address>.")
    messages = await anyio.to_thread.run_sync(outbox_messages, request.app.state.db, to_email)
    return JSONResponse({"messages": messages}, headers=NO_STORE)
