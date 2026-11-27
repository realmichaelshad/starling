"""Exceptions that shape refusals for the API (CONTRACTS 6.3) and for website routes.

All three types subclass `HTTPException`, so a router that does not use `StarlingRoute`
still returns the right status code and headers through FastAPI's default handler. Routers
built with `APIRouter(route_class=StarlingRoute)` (every server core router) return the
contract body instead: `{"error": {"code", "message", ...}}` for `ApiError`, a real 303
redirect for `WebRedirect`, and a minimal HTML page for `PageError`.

`shaped_exception_handler` can be registered app-wide by the app factory so other routers
get the same bodies (see the server core report).
"""
from __future__ import annotations

import logging
from html import escape
from http import HTTPStatus
from typing import Any, Callable, Coroutine
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import clock

log = logging.getLogger(__name__)


class ApiError(HTTPException):
    """A refusal on `/api/v1`, rendered as the error body of CONTRACTS section 6.3."""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        *,
        retry_after_s: int | None = None,
        usage: dict[str, Any] | None = None,
        upgrade_path: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        merged = dict(headers or {})
        if retry_after_s is not None:
            merged["Retry-After"] = str(max(0, int(retry_after_s)))
        super().__init__(status_code=http_status, detail=message, headers=merged or None)
        self.code = code
        self.message = message
        self.retry_after_s = None if retry_after_s is None else max(0, int(retry_after_s))
        self.usage = usage
        self.upgrade_path = upgrade_path

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after_s is not None:
            error["retry_after_s"] = self.retry_after_s
        if self.usage is not None:
            error["usage"] = self.usage
        if self.upgrade_path is not None:
            error["upgrade_path"] = self.upgrade_path
        return {"error": error}

    def to_response(self) -> Response:
        headers = dict(self.headers or {})
        headers.setdefault("Cache-Control", "no-store")
        return JSONResponse(self.body(), status_code=self.status_code, headers=headers)


class WebRedirect(HTTPException):
    """Send a website request elsewhere with 303 See Other (for example to /login?next=...)."""

    def __init__(self, location: str, headers: dict[str, str] | None = None) -> None:
        merged = dict(headers or {})
        merged["Location"] = location
        super().__init__(status_code=303, detail="See Other", headers=merged)
        self.location = location

    def to_response(self) -> Response:
        extra = {k: v for k, v in (self.headers or {}).items() if k.lower() != "location"}
        extra.setdefault("Cache-Control", "no-store")
        return RedirectResponse(self.location, status_code=303, headers=extra)


class PageError(HTTPException):
    """A website refusal (403 CSRF, 404, 413, 429, 503), shown as the site's problem page.

    `kind` names the refusals that deserve their own wording and way forward: "csrf" (an expired
    or cross-site form) and "unverified" (an address that is not confirmed yet). `to_response` is
    the bare fallback for when the website templates cannot render at all.
    """

    def __init__(self, status_code: int, message: str, headers: dict[str, str] | None = None,
                 kind: str | None = None) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.message = message
        self.kind = kind

    def to_response(self) -> Response:
        title = HTTPStatus(self.status_code).phrase
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f"<title>{escape(title)}</title></head><body><h1>{escape(title)}</h1>"
            f"<p>{escape(self.message)}</p></body></html>"
        )
        headers = dict(self.headers or {})
        headers.setdefault("Cache-Control", "no-store")
        return HTMLResponse(body, status_code=self.status_code, headers=headers)


SHAPED_ERRORS = (ApiError, WebRedirect, PageError)


def record_api_error(request: Request, exc: ApiError, route_path: str | None = None) -> None:
    """Count a refusal on `/api/` for the owner dashboard (`db.request_errors`). Never raises.

    Attribution comes from `request.state`: `account_id` (set by `auth.deps.require_device`), and
    `assist_mode` and `reservation_id` (set by the engine routes). Unmatched URLs are not counted.
    """
    if not request.url.path.startswith("/api/"):
        return
    route = route_path or getattr(request.scope.get("route"), "path", None)
    db = getattr(request.app.state, "db", None)
    if not route or db is None:
        return
    try:
        from ..db import request_errors

        request_errors.record(
            db,
            route=route,
            code=exc.code,
            http_status=exc.status_code,
            now=clock.now(),
            account_id=getattr(request.state, "account_id", None),
            mode=getattr(request.state, "assist_mode", None),
            reserved=getattr(request.state, "reservation_id", None) is not None,
        )
    except Exception:  # counting must never change the response
        log.debug("could not queue an API error record", exc_info=True)


def _back_path(request: Request) -> str:
    """Where "Go back" goes: the same-origin page the request came from, or the home page."""
    from ..security.http import safe_next

    referer = request.headers.get("referer") or ""
    parts = urlsplit(referer)
    here = urlsplit(str(request.base_url))
    if parts.scheme and (parts.scheme, parts.netloc) != (here.scheme, here.netloc):
        return "/"
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return safe_next(path or None, "/")


def _problem(request: Request, exc: PageError) -> dict[str, Any]:
    """The words and the way forward for one refusal (errors/problem.html)."""
    back = _back_path(request)
    retry = (exc.headers or {}).get("Retry-After", "")
    wait = f" Try again in {max(1, -(-int(retry) // 60))} minute{'s' if int(retry) > 60 else ''}." if retry.isdigit() else ""
    if exc.kind == "csrf":
        return {
            "title": "This page expired",
            "message": "For your security, forms stop working after a while, or after you sign in or out in "
                       "another tab. Reload the page and try again.",
            "action": {"label": "Reload the page", "href": back}, "secondary": None,
        }
    if exc.kind == "unverified":
        return {
            "title": "Confirm your email first",
            "message": exc.message,
            "action": {"label": "Enter the code", "href": "/verify-email/sent"},
            "secondary": {"label": "Go to your account", "href": "/account"},
        }
    if exc.status_code == 429:
        message = exc.message if not wait else exc.message.replace(" Try again later.", "") + wait
        return {"title": "Too many attempts", "message": message, "action": {"label": "Go back", "href": back}, "secondary": None}
    if exc.status_code == 413:
        return {"title": "That was too large", "message": exc.message, "action": {"label": "Go back", "href": back}, "secondary": None}
    if exc.status_code in (400, 411):
        return {"title": "That form could not be read", "message": exc.message,
                "action": {"label": "Reload the page", "href": back}, "secondary": None}
    if exc.status_code == 503:
        again = str(request.url.path) if request.method in ("GET", "HEAD") else back
        return {"title": "Starling is briefly unavailable", "message": exc.message + wait,
                "action": {"label": "Try again", "href": again}, "secondary": None}
    return {"title": "That cannot be done", "message": exc.message,
            "action": {"label": "Go to the home page", "href": "/"}, "secondary": None}


async def page_error_response(request: Request, exc: PageError) -> Response:
    """404 and 500 use their own pages; every other refusal the problem page, with a way forward.

    An expired sign-in, sign-up or reset form is not an error to the person: it comes straight
    back with a fresh token and what they typed (`auth.routes.expired_form`).
    """
    from ..web.render import render, render_error, template_exists  # late: web.render depends on this module

    headers = dict(exc.headers or {})
    if exc.status_code == 404 or exc.status_code == 500:
        return await render_error(request, exc.status_code, exc.message, headers)
    if exc.kind == "csrf" and request.method == "POST":
        from ..auth.routes import expired_form

        fresh = await expired_form(request)
        if fresh is not None:
            return fresh
    if template_exists(request, "errors/problem.html"):
        try:
            context = {"status": exc.status_code, **_problem(request, exc)}
            return await render(request, "errors/problem.html", context, status_code=exc.status_code, headers=headers)
        except Exception as error:  # never fail while reporting a refusal
            log.error("the problem page could not render: %s", type(error).__name__)
    return exc.to_response()


async def shaped_exception_handler(request: Request, exc: Exception) -> Response:
    """App-level handler equivalent of `StarlingRoute` (for `app.add_exception_handler`)."""
    if isinstance(exc, PageError):
        return await page_error_response(request, exc)
    if isinstance(exc, ApiError):
        record_api_error(request, exc)
    if isinstance(exc, SHAPED_ERRORS):
        return exc.to_response()
    raise exc


class StarlingRoute(APIRoute):
    """Route class that turns the shaped exceptions (also from dependencies) into responses, and
    unhandled errors on website (HTML) routes into the `errors/500.html` page. API and JSON requests
    keep the framework's default handling of unexpected errors."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except PageError as exc:
                return await page_error_response(request, exc)
            except SHAPED_ERRORS as exc:
                if isinstance(exc, ApiError):
                    record_api_error(request, exc, self.path)
                return exc.to_response()
            except (StarletteHTTPException, RequestValidationError):
                raise
            except Exception:
                from ..security.http import wants_json

                if wants_json(request):
                    raise
                request_id = getattr(request.state, "request_id", None)
                log.exception("unhandled error on %s %s request_id=%s", request.method, request.url.path, request_id)
                from ..web.render import render_error

                return await render_error(request, 500)

        return route_handler
