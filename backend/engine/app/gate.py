"""Default-deny: nothing answers this API without the worker token.

Authentication in `main.py` is also applied per handler, as a
`Depends(require_token)` parameter. That works exactly as long as nobody forgets,
and the failure is silent, because a route missing the parameter is simply
public. This closes it from the other side: every request is denied unless it
carries the token, whatever the router does or does not do.

It is a **pure ASGI middleware**, deliberately not `BaseHTTPMiddleware`:

  * `BaseHTTPMiddleware` only sees `scope["type"] == "http"`, so a WebSocket
    handshake would slip past the check this exists to guarantee.
  * It buffers, which interferes with long-lived streaming, and
    `/browser/assist/stream` is exactly that.

Failures answer **404 with an empty body**, not 401. A 401 confirms that a path
exists, so an unauthenticated caller could map the API by watching which paths
answer differently. `/profile/export` and `/no-such-route` must be
indistinguishable, for every method.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from .security import token_is_valid

# Deliberately empty. Even `/livez` requires the worker token (CONTRACTS §3.2):
# the gateway always holds it, so there is no bootstrap problem to solve.
# `tests/test_route_auth_matrix.py` asserts this set is empty.
PUBLIC_PATHS: frozenset[str] = frozenset()

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


def _bearer(scope: Scope) -> str:
    for name, value in scope.get("headers") or ():
        if name == b"authorization":
            try:
                supplied = value.decode("latin-1")
            except Exception:
                return ""
            return supplied[7:] if supplied[:7].lower() == "bearer " else supplied
    return ""


async def _deny_http(send: Send) -> None:
    """Byte-identical to a genuine 404: no body, no `WWW-Authenticate`, nothing
    naming this service."""
    await send({"type": "http.response.start", "status": 404,
                "headers": [(b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


# A request body larger than this is refused before a byte of it is read. The
# risk here is MEMORY: 1 MiB is roughly ten times the largest legitimate assist
# payload (the gateway limits chat messages to 2,000 characters).
MAX_BODY_BYTES = 1024 * 1024


class BodyLimit:
    """Refuse oversized bodies on Content-Length, and enforce it while reading.

    Content-Length alone is not enough (a chunked request simply omits it), so
    the byte counter wraps `receive` as well and terminates the stream once the
    ceiling is passed.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        limit = MAX_BODY_BYTES

        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > limit:
                    await _too_large(send)
                    return
                break

        seen = 0

        async def counted() -> dict:
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    # Stop the stream rather than keep buffering. The handler
                    # sees a truncated body and fails validation.
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, counted, send)


async def _too_large(send: Send) -> None:
    body = b'{"detail":"Request body is too large."}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class AuthGate:
    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind == "lifespan":                       # startup/shutdown, not a request
            await self.app(scope, receive, send)
            return

        if kind == "http":
            # No CORS and therefore no preflight exemption: the engine is called
            # by the gateway only, and exempting OPTIONS would let the router
            # answer 405 for a real path and 404 for a missing one.
            if scope.get("path") in PUBLIC_PATHS or token_is_valid(_bearer(scope)):
                await self.app(scope, receive, send)
                return
            await _deny_http(send)
            return

        if kind == "websocket":
            # The engine exposes no WebSocket route. A handshake is closed
            # without being accepted unless it carries the token in its header.
            if token_is_valid(_bearer(scope)):
                await self.app(scope, receive, send)
                return
            await send({"type": "websocket.close", "code": 4401})
            return

        await self.app(scope, receive, send)
