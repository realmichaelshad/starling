"""Pure ASGI middleware (safe for streaming responses): request ids and security headers."""
from __future__ import annotations

import re
import uuid
from typing import Any

from ..auth import google
from ..config import Settings

# The billing forms POST to this site and are answered with a redirect to Stripe's hosted pages.
# A browser applies `form-action` to that redirect too, so every Stripe origin is named here:
# without them "Choose Starter" does nothing at all in a real browser, and no HTTP-level test
# notices, because only a browser enforces the policy.
# Checkout, the Customer Portal, and the hosted page of an invoice (where a customer whose first
# payment was declined finishes paying: `billing.service.unpaid_first_invoice_url`).
PAYMENT_PAGE_ORIGINS = ("https://checkout.stripe.com", "https://billing.stripe.com", "https://invoice.stripe.com")
_LOOPBACK_ORIGIN_RE = re.compile(r"^https://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$")


def content_security_policy(settings: Settings | None = None) -> str:
    """The policy for every HTML page.

    Development and e2e may name ONE more origin for `form-action`: the stand-in payment pages
    of the test harness (`STARLING_E2E_PAYMENT_PAGES_ORIGIN`, loopback https only, refused in
    production by the configuration check). Found by the first browser journey that tried to
    pay: the redirect to the stand-in pages was blocked, exactly as a real browser would block
    a redirect to a Stripe domain that was missing from this list.
    """
    origins = list(PAYMENT_PAGE_ORIGINS)
    extra = settings.e2e_payment_pages_origin if settings is not None else ""
    if extra and not settings.is_production and _LOOPBACK_ORIGIN_RE.match(extra):
        origins.append(extra)
    if settings is not None and settings.google_sign_in_enabled:
        # "Connect Google" on the Security page is a form (it changes a signed-in account), and it is
        # answered with a redirect to Google's sign-in page, which `form-action` governs too.
        google_origin = _google_origin(settings)
        if google_origin:
            origins.append(google_origin)
    return (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; manifest-src 'self'; "
        f"form-action 'self' {' '.join(origins)}; "
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    )


_LOOPBACK_ANY_ORIGIN_RE = re.compile(r"^https?://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$")


def _google_origin(settings: Settings) -> str:
    origin = google.authorize_origin(settings)
    if origin == google.AUTHORIZE_ORIGIN:
        return origin
    # The e2e harness's stand-in for Google: loopback only, never in production.
    return origin if not settings.is_production and _LOOPBACK_ANY_ORIGIN_RE.match(origin) else ""


CONTENT_SECURITY_POLICY = content_security_policy()


# Files that are compressed already: gzip would only spend CPU making them slightly bigger.
_COMPRESSED_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".zip", ".gz")


class StaticAssetsMiddleware:
    """Gzip and long caching for `/static/` files; every other response passes through untouched.

    Only static files, on purpose:
    - HTML pages carry a CSRF token next to text a visitor can influence (a `next` path, a typed
      address), and compressing that is exactly the BREACH side channel. The CSS and JavaScript are
      most of the bytes anyway (74.6 KB of render-blocking CSS becomes about 19 KB).
    - The extension API streams drafts as NDJSON; a compressor in the middle holds tokens back until
      its buffer fills, which is the wait streaming exists to remove.
    URLs made by the templates' `static()` helper carry `?v=<content hash>`, so a changed file gets a
    new URL and the old one may be cached for a year. Anything else is cached for an hour.
    """

    def __init__(self, app: Any, minimum_size: int = 1024) -> None:
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=6)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith("/static/") or scope.get("method") not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        fingerprinted = b"v=" in scope.get("query_string", b"")
        cache = "public, max-age=31536000, immutable" if fingerprinted else "public, max-age=3600"

        async def send_with_cache(message: dict) -> None:
            if message["type"] == "http.response.start" and message.get("status", 200) == 200:
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", cache.encode()))
                message["headers"] = headers
            await send(message)

        target = self.app if path.lower().endswith(_COMPRESSED_SUFFIXES) else self.gzip
        await target(scope, receive, send_with_cache)


class RequestIdMiddleware:
    """Assign every request an id (`scope["state"]["request_id"]`) and echo it as `X-Request-Id`."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_id)


class SecurityHeadersMiddleware:
    """Add hardening headers to every HTTP response without overriding route-specific ones."""

    def __init__(self, app: Any, settings: Settings) -> None:
        self.app = app
        self.hsts = settings.public_url.startswith("https://")
        self.csp = content_security_policy(settings)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                content_type = next((v for n, v in headers if n.lower() == b"content-type"), b"")

                def add(name: bytes, value: str) -> None:
                    if name not in present:
                        headers.append((name, value.encode()))

                add(b"x-content-type-options", "nosniff")
                add(b"referrer-policy", "strict-origin-when-cross-origin")
                add(b"permissions-policy", "camera=(), microphone=(), geolocation=(), payment=()")
                add(b"cross-origin-opener-policy", "same-origin")
                add(b"x-frame-options", "DENY")
                if content_type.startswith(b"text/html"):
                    add(b"content-security-policy", self.csp)
                if self.hsts:
                    add(b"strict-transport-security", "max-age=31536000; includeSubDomains")
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
