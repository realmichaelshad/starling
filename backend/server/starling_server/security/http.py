"""Request helpers: bounded body reading, safe `next` targets and content negotiation."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from .errors import ApiError, PageError

MAX_FORM_BYTES = 64 * 1024
MAX_JSON_BYTES = 32 * 1024
MAX_FORM_FIELDS = 50


async def read_form(request: Request, max_bytes: int = MAX_FORM_BYTES) -> FormData:
    """Parse a website form once (Starlette caches it on the request) with a size bound.

    Browsers always send `Content-Length` for form posts; a form without it is refused rather
    than read without a limit.
    """
    cached = getattr(request.state, "starling_form", None)
    if cached is not None:
        return cached
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        raise PageError(411, "The form was sent without a length. Reload the page and try again.")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise PageError(400, "The form could not be read.") from exc
    if length < 0 or length > max_bytes:
        raise PageError(413, "The form is too large.")
    try:
        form = await request.form(max_files=0, max_fields=MAX_FORM_FIELDS, max_part_size=max_bytes)
    except (StarletteHTTPException, MultiPartException) as exc:
        # A body the form parser refuses (a file part, too many fields, a broken boundary) would
        # otherwise leave the framework's own JSON body on a website route.
        raise PageError(400, "The form could not be read. Reload the page and try again.") from exc
    request.state.starling_form = form
    return form


def form_str(form: FormData, name: str, max_length: int = 1024) -> str:
    """A single text field (files and oversized values are treated as empty)."""
    value = form.get(name)
    if not isinstance(value, str) or len(value) > max_length:
        return ""
    return value


async def read_json_object(request: Request, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    """Read an `/api/v1` JSON object body with a streaming size bound."""
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            if int(raw_length) > max_bytes:
                raise ApiError("payload_too_large", 413, "The request body is too large.")
        except ValueError as exc:
            raise ApiError("bad_request", 400, "The request body could not be read.") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise ApiError("payload_too_large", 413, "The request body is too large.")
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks) or b"null")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError("bad_request", 400, "The request body must be JSON.") from exc
    if not isinstance(data, dict):
        raise ApiError("bad_request", 400, "The request body must be a JSON object.")
    return data


def safe_next(value: str | None, default: str = "/account") -> str:
    """Return `value` only when it is a same-origin path (no scheme, host or `//`)."""
    if not value or len(value) > 2048:
        return default
    if any(ord(ch) < 0x20 or ch == "\\" for ch in value):
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value


def wants_json(request: Request) -> bool:
    """True for API paths and for clients that ask for JSON rather than HTML."""
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "").lower()
    return "application/json" in accept and "text/html" not in accept
