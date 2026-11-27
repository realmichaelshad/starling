"""Caller authentication and request plumbing shared by the services routes."""
from __future__ import annotations

import json
import re
import time

import anyio
from fastapi import Request

from . import tokens
from .errors import ServiceError
from .metering import BUCKET_BACKGROUND, BUCKET_PRESS

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")


def authenticate(request: Request, *, tenant: bool, admin: bool) -> tokens.Subject:
    """Verify `Authorization: Bearer <services token>` and the subject kinds this route accepts."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    token = value.strip()
    if scheme.lower() != "bearer" or not token:
        raise ServiceError(401, "unauthorized", "a services token is required")
    state = request.app.state
    try:
        subject = tokens.verify(state.settings.services_token_key, token, state.clock())
    except tokens.TokenError:
        raise ServiceError(401, "unauthorized", "unknown services token") from None
    if (subject.is_admin and not admin) or (not subject.is_admin and not tenant):
        raise ServiceError(401, "unauthorized", "this token cannot use this route")
    return subject


# How long an "active" answer is reused. A services token lives for a day and cannot be revoked (it is
# verified without a lookup, CONTRACTS 4.3), so the account behind it is what gets checked instead.
ACTIVE_CACHE_S = 30.0
_ACTIVE_CACHE_MAX = 10_000


def _account_status(db, tenant_id: str) -> str | None:
    with db.read() as conn:
        row = conn.execute("SELECT status FROM accounts WHERE id = ?", (tenant_id,)).fetchone()
    return row["status"] if row is not None else None


async def require_active_account(request: Request, subject: tokens.Subject) -> None:
    """A customer's token is good only while the customer is: suspended and deleted accounts get 401.

    The AI proxy always checked this (`Metering._admit`); the page and image fetcher did not, so a
    token taken from a compromised engine went on working as an outbound fetch proxy, from the
    owner's address, for the rest of its day after the owner had suspended the account and stopped
    the engine. Only "active" is remembered, and only briefly: a suspension takes effect within
    `ACTIVE_CACHE_S` seconds and costs one indexed read per customer per interval.
    """
    if subject.is_admin or subject.tenant_id is None:
        return
    state = request.app.state
    cache: dict[str, float] = getattr(state, "active_accounts", None) or {}
    state.active_accounts = cache
    now = time.monotonic()
    if cache.get(subject.tenant_id, 0.0) > now:
        return
    status = await anyio.to_thread.run_sync(_account_status, state.db, subject.tenant_id)
    if status != "active":
        cache.pop(subject.tenant_id, None)
        raise ServiceError(401, "unauthorized", "unknown services token")
    if len(cache) >= _ACTIVE_CACHE_MAX:
        cache.clear()
    cache[subject.tenant_id] = now + ACTIVE_CACHE_S


def metering_headers(request: Request) -> tuple[str | None, str]:
    """Exactly one of `X-Starling-Request-Id` (press) or `X-Starling-Bucket: background`."""
    request_id = request.headers.get("x-starling-request-id")
    bucket = request.headers.get("x-starling-bucket")
    if request_id is not None and bucket is not None:
        raise ServiceError(400, "bad_request", "send either X-Starling-Request-Id or X-Starling-Bucket, not both")
    if request_id is not None:
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise ServiceError(400, "bad_request", "X-Starling-Request-Id is not a valid request id")
        return request_id, BUCKET_PRESS
    if bucket is not None:
        if bucket.strip().lower() != BUCKET_BACKGROUND:
            raise ServiceError(400, "bad_request", "X-Starling-Bucket must be background")
        return None, BUCKET_BACKGROUND
    raise ServiceError(400, "bad_request", "X-Starling-Request-Id or X-Starling-Bucket: background is required")


async def read_body(request: Request, limit: int, too_large: ServiceError) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise ServiceError(400, "bad_request", "Content-Length is not a number") from None
        if size > limit:
            raise too_large
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json_object(request: Request, *, limit: int, fields: frozenset[str]) -> dict:
    raw = await read_body(request, limit, ServiceError(413, "payload_too_large", "the request body is too large"))
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ServiceError(400, "bad_request", "the body must be a JSON object") from None
    if not isinstance(data, dict):
        raise ServiceError(400, "bad_request", "the body must be a JSON object")
    if set(data) - fields:
        raise ServiceError(400, "bad_request", "the body contains a field that is not accepted")
    return data
