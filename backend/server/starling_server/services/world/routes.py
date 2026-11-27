"""World service routes (CONTRACTS 4.2; response fields per docs/architecture/news-pool-shape.md).

Every route needs a services token (engine or admin). Page and image fetches are limited per caller
so the fetcher cannot be used as an open proxy. `POST /v1/x/post` (admin token only: the gateway's
writing page) reads one public post on X through X's oEmbed endpoint; see x_post.py. `POST /v1/search` is one metered web search, for a
customer's press (W4: capped per press, per account and day, and by the plan) or as the account's
background topic research (its own daily cap, inside the background share); see search.py.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import authenticate, metering_headers, read_json_object, require_active_account
from ..errors import ServiceError
from ..media import MAX_IMAGE_BYTES, sniff_image_type, to_data_url
from ..proxy import LANES
from .extract import extract_page, extract_plain_text
from .news import POOL_LIMIT, NewsPool
from .safe_fetch import FetchError, SafeFetcher
from .search import WebSearchService
from .summary import SummaryService
from .x_post import XPostReader

router = APIRouter()

FETCH_MAX_BYTES = 500 * 1024
FETCH_TIMEOUT_S = 8.0
FETCH_MAX_REDIRECTS = 3
IMAGE_TIMEOUT_S = 10.0
BODY_LIMIT_BYTES = 16 * 1024
FETCHES_PER_MINUTE = 60
# Every oEmbed lookup comes from the gateway (one admin subject), which limits each account itself;
# this is the ceiling for the whole service, and the per-post cache answers repeats.
X_POST_LOOKUPS_PER_MINUTE = 120
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
PAGE_ACCEPT = "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,*/*;q=0.1"
IMAGE_ACCEPT = "image/png,image/jpeg,image/webp,image/gif;q=0.9,*/*;q=0.1"
NO_STORE = {"Cache-Control": "no-store"}


class FetchRateLimiter:
    """At most `per_minute` page or image fetches per caller in any 60-second window."""

    def __init__(self, per_minute: int = FETCHES_PER_MINUTE, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = per_minute
        self._monotonic = monotonic
        self._events: dict[str, deque[float]] = {}

    def check(self, key: str) -> None:
        now = self._monotonic()
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= 60.0:
            events.popleft()
        if len(events) >= self._per_minute:
            retry = max(1, math.ceil(60.0 - (now - events[0])))
            raise ServiceError(429, "rate_limited", "too many fetches; slow down", retry)
        events.append(now)


class World:
    def __init__(self, fetcher: SafeFetcher, pool: NewsPool, summaries: SummaryService, limiter: FetchRateLimiter,
                 search: WebSearchService | None = None, x_posts: XPostReader | None = None,
                 x_post_limiter: FetchRateLimiter | None = None) -> None:
        self.fetcher = fetcher
        self.pool = pool
        self.summaries = summaries
        self.limiter = limiter
        self.search = search
        self.x_posts = x_posts
        self.x_post_limiter = x_post_limiter or FetchRateLimiter(per_minute=X_POST_LOOKUPS_PER_MINUTE)


def _world(request: Request) -> World:
    return request.app.state.world


def _media_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _looks_like_html(content: bytes) -> bool:
    return content.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))


@router.get("/v1/news/headlines")
async def headlines(request: Request) -> JSONResponse:
    await require_active_account(request, authenticate(request, tenant=True, admin=True))
    raw = request.query_params.get("limit")
    limit = POOL_LIMIT
    if raw is not None:
        if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= POOL_LIMIT:
            raise ServiceError(400, "bad_request", "limit must be an integer from 1 to 150")
        limit = int(raw)
    return JSONResponse(_world(request).pool.headlines(limit), headers=NO_STORE)


@router.post("/v1/fetch")
async def fetch_page(request: Request) -> JSONResponse:
    subject = authenticate(request, tenant=True, admin=True)
    await require_active_account(request, subject)
    body = await read_json_object(request, limit=BODY_LIMIT_BYTES, fields=frozenset({"url"}))
    world = _world(request)
    world.limiter.check(subject.value)
    result = await world.fetcher.get(
        body.get("url"),
        max_bytes=FETCH_MAX_BYTES,
        timeout_s=FETCH_TIMEOUT_S,
        max_redirects=FETCH_MAX_REDIRECTS,
        accept=PAGE_ACCEPT,
        truncate=True,
    )
    if not 200 <= result.status < 300:
        raise FetchError(502, "fetch_failed", f"the destination answered with HTTP {result.status}")
    content_type = result.headers.get("content-type")
    kind = _media_type(content_type)
    if kind in HTML_TYPES or (not kind and _looks_like_html(result.content)):
        page = extract_page(result.content, content_type)
    elif kind == "text/plain":
        page = extract_plain_text(result.content, content_type)
    else:
        raise FetchError(415, "unsupported_content", "the destination is not an HTML or plain text page")
    return JSONResponse(
        {"final_url": result.url, "title": page.title, "text": page.text, "published_at": page.published_at},
        headers=NO_STORE,
    )


@router.post("/v1/image")
async def fetch_image(request: Request) -> JSONResponse:
    subject = authenticate(request, tenant=True, admin=True)
    await require_active_account(request, subject)
    body = await read_json_object(request, limit=BODY_LIMIT_BYTES, fields=frozenset({"url"}))
    world = _world(request)
    world.limiter.check(subject.value)
    result = await world.fetcher.get(
        body.get("url"),
        max_bytes=MAX_IMAGE_BYTES,
        timeout_s=IMAGE_TIMEOUT_S,
        max_redirects=FETCH_MAX_REDIRECTS,
        accept=IMAGE_ACCEPT,
        truncate=False,
    )
    if not 200 <= result.status < 300:
        raise FetchError(502, "fetch_failed", f"the destination answered with HTTP {result.status}")
    if not _media_type(result.headers.get("content-type")).startswith("image/"):
        raise FetchError(415, "unsupported_content", "the destination is not an image")
    mime = sniff_image_type(result.content)
    if mime is None:
        raise FetchError(415, "unsupported_content", "the image is not PNG, JPEG, WebP or GIF")
    return JSONResponse({"data_url": to_data_url(mime, result.content), "mime": mime}, headers=NO_STORE)


@router.post("/v1/summary")
async def summary(request: Request) -> JSONResponse:
    await require_active_account(request, authenticate(request, tenant=True, admin=True))
    body = await read_json_object(request, limit=BODY_LIMIT_BYTES, fields=frozenset({"query_id"}))
    return JSONResponse(await _world(request).summaries.get(body.get("query_id")), headers=NO_STORE)


@router.post("/v1/search")
async def web_search(request: Request) -> JSONResponse:
    """One web search: for the press named by `X-Starling-Request-Id` (W4), or background topic
    research (`X-Starling-Bucket: background`, purpose "topics"); see search.py for every cap.

    Body `{"query": str (8-300 chars), "timeout_s"?: number, "purpose"?: "claim" | "topics"}`.
    Response `{"query", "answer", "sources": [{"title", "url", "snippet"}] (at most 4), "searches",
    "queries"}`.
    """
    subject = authenticate(request, tenant=True, admin=False)
    await require_active_account(request, subject)
    reservation_id, bucket = metering_headers(request)
    body = await read_json_object(request, limit=BODY_LIMIT_BYTES,
                                  fields=frozenset({"query", "timeout_s", "purpose"}))
    search = _world(request).search
    if search is None:
        raise ServiceError(503, "search_unavailable", "web search is not available in this process")
    result = await search.search(body.get("query"), account_id=str(subject.tenant_id),
                                 reservation_id=reservation_id, bucket=bucket, timeout_s=body.get("timeout_s"),
                                 purpose=body.get("purpose"))
    return JSONResponse(result, headers=NO_STORE)


@router.post("/v1/x/post")
async def x_post(request: Request) -> JSONResponse:
    """One public post on X, read through X's oEmbed endpoint for the website's writing page.

    Body `{"url": <link to one post on x.com or twitter.com>}`. Response `{"url", "status_id",
    "author_name", "author_handle", "text", "lang", "links", "has_media", "cached"}`. 404
    `post_unavailable` when X will not show the post outside X (deleted, protected, restricted).
    """
    authenticate(request, tenant=False, admin=True)
    body = await read_json_object(request, limit=BODY_LIMIT_BYTES, fields=frozenset({"url"}))
    world = _world(request)
    if world.x_posts is None:
        raise ServiceError(503, "x_post_unavailable", "reading posts on X is not available in this process")
    world.x_post_limiter.check("x-oembed")
    return JSONResponse(await world.x_posts.read(body.get("url")), headers=NO_STORE)


@router.get("/v1/models")
async def models(request: Request) -> JSONResponse:
    authenticate(request, tenant=True, admin=True)
    data = [{"id": lane, "object": "model", "owned_by": "starling"} for lane in LANES]
    return JSONResponse({"object": "list", "data": data}, headers=NO_STORE)
