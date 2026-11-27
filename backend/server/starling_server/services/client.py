"""Gateway-side client for the services process admin endpoints (CONTRACTS 14).

`STARLING_SERVICES_URL` is `unix:/run/starling/services.sock`, `tcp:127.0.0.1:PORT` or
`http://127.0.0.1:PORT`. Each call mints a fresh admin token (5 minutes). Failures never raise: they
return `{"ok": false, "error": {"code", "message"}}` so admin pages can show them.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import Settings
from .tokens import TokenError, mint_admin_token

AI_CHECK_TIMEOUT_S = 75.0
STATUS_TIMEOUT_S = 10.0
# The services process gives X's oEmbed endpoint 5 seconds; this leaves room for the hop itself.
X_POST_TIMEOUT_S = 8.0


def _failure(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _endpoint(settings: Settings, transport: httpx.AsyncBaseTransport | None) -> tuple[str, httpx.AsyncBaseTransport | None]:
    url = (settings.services_url or "").strip()
    if url.startswith("unix:"):
        return "http://services", transport or httpx.AsyncHTTPTransport(uds=url[len("unix:"):])
    if url.startswith("tcp:"):
        return f"http://{url[len('tcp:'):]}", transport
    return url.rstrip("/"), transport


async def _call(
    settings: Settings, method: str, path: str, *, timeout_s: float, transport: httpx.AsyncBaseTransport | None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        token = mint_admin_token(settings.services_token_key, int(time.time()))
    except TokenError:
        return _failure(
            "services_not_configured",
            "STARLING_SERVICES_TOKEN_KEY is missing or too short, so the gateway cannot call the services process.",
        )
    base_url, chosen = _endpoint(settings, transport)
    try:
        async with httpx.AsyncClient(
            base_url=base_url, transport=chosen, timeout=timeout_s, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.request(method, path, headers={"Authorization": f"Bearer {token}"}, json=json_body)
    except httpx.HTTPError as exc:
        return _failure(
            "services_unreachable",
            f"The services process did not answer ({type(exc).__name__}). Check that it is running and STARLING_SERVICES_URL.",
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code == 200 and isinstance(payload, dict):
        return payload
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return _failure(error["code"], str(error.get("message") or ""))
    return _failure("services_error", f"The services process answered HTTP {response.status_code}.")


async def ai_check(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Run the AI connection test through the services process (no customer data)."""
    return await _call(settings, "POST", "/admin/ai-check", timeout_s=AI_CHECK_TIMEOUT_S, transport=transport)


async def services_status(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Budget usage, key status (masked), upstream health and world-service freshness."""
    return await _call(settings, "GET", "/admin/status", timeout_s=STATUS_TIMEOUT_S, transport=transport)


async def read_x_post(settings: Settings, url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """One public post on X, read by the services process through X's oEmbed endpoint (the website's
    writing page). `{"url", "status_id", "author_name", "author_handle", "text", "lang", "links",
    "has_media", ...}`, or `{"ok": false, "error": {"code", "message"}}` (`post_unavailable` when X
    will not show the post outside X)."""
    return await _call(settings, "POST", "/v1/x/post", timeout_s=X_POST_TIMEOUT_S, transport=transport,
                       json_body={"url": url})
