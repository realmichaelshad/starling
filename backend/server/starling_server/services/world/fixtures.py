"""STARLING_WORLD_FIXTURES: public pages and news stories from a file, for e2e runs only.

With STARLING_WORLD_OFFLINE set (development and e2e; production refuses both variables in
`config.validate`), the world service makes no outbound connection. That also means the eval
never exercises the two world tools a press really uses: reading a post's linked page
(`/v1/fetch`) and matching the shared news pool (`/v1/news/headlines`). This file gives both
something to serve, still without a single connection:

    {"pages": {"https://example.org/story": {"title": "...", "text": "...", "published": "2026-09-20"}},
     "news": [{"title": "...", "url": "https://...", "source": "Example News", "summary": "...",
               "published_ago_s": 7200, "points": 120, "lane": "ai"}]}

A fetch of a URL listed under "pages" answers with that page (as a small HTML document, so it goes
through the same extractor as a real page); any other URL fails exactly as offline fetches do. The
news items seed the pool once at start (`NewsPool.seed`), dated relative to that moment. The file
is ignored unless the process is offline, so a fixture can never stand in for the real world.
"""
from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .news import RawStory
from .safe_fetch import FetchResult

log = logging.getLogger(__name__)

MAX_PAGES = 200
MAX_STORIES = 150


@dataclass(frozen=True)
class WorldFixtures:
    pages: dict[str, FetchResult] = field(default_factory=dict)
    news: tuple[RawStory, ...] = ()

    def page(self, url: object) -> FetchResult | None:
        return self.pages.get(str(url or "").strip())


def _document(title: str, text: str, published: str) -> bytes:
    meta = (f'<meta property="article:published_time" content="{html.escape(published, quote=True)}">'
            if published else "")
    paragraphs = "".join(f"<p>{html.escape(part.strip())}</p>" for part in text.split("\n\n") if part.strip())
    return (f"<!doctype html><html><head><title>{html.escape(title)}</title>{meta}</head>"
            f"<body><article><h1>{html.escape(title)}</h1>{paragraphs}</article></body></html>").encode("utf-8")


def load(path: str | Path, *, now: float | None = None) -> WorldFixtures:
    """Read the fixture file; an unreadable file is logged and serves nothing."""
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("STARLING_WORLD_FIXTURES could not be read (%s)", type(exc).__name__)
        return WorldFixtures()
    if not isinstance(data, dict):
        return WorldFixtures()
    pages: dict[str, FetchResult] = {}
    for url, page in list((data.get("pages") or {}).items())[:MAX_PAGES]:
        if not isinstance(page, dict) or not str(url).startswith(("https://", "http://")):
            continue
        body = _document(str(page.get("title") or ""), str(page.get("text") or ""), str(page.get("published") or ""))
        pages[str(url)] = FetchResult(url=str(url), status=200,
                                      headers=httpx.Headers({"content-type": "text/html; charset=utf-8"}),
                                      content=body)
    stories: list[RawStory] = []
    for item in list(data.get("news") or [])[:MAX_STORIES]:
        if not isinstance(item, dict):
            continue
        try:
            ago = max(0, int(item.get("published_ago_s") or 0))
            points = max(0, int(item.get("points") or 0))
        except (TypeError, ValueError):
            continue
        stories.append(RawStory(title=str(item.get("title") or ""), url=str(item.get("url") or ""),
                                source=str(item.get("source") or ""), summary=str(item.get("summary") or ""),
                                published_at=int(now) - ago if ago else 0, points=points,
                                lane=item.get("lane") if isinstance(item.get("lane"), str) else None))
    log.warning("STARLING_WORLD_FIXTURES is set: %d page(s) and %d news story(ies) are served from a file",
                len(pages), len(stories))
    return WorldFixtures(pages=pages, news=tuple(stories))


def load_if_offline(settings: Any) -> WorldFixtures | None:
    """The fixtures, only for an offline process with STARLING_WORLD_FIXTURES set."""
    path = str(getattr(settings, "world_fixtures", "") or "").strip()
    if not path:
        return None
    if not getattr(settings, "world_offline", False):
        log.error("STARLING_WORLD_FIXTURES is ignored: it only works with STARLING_WORLD_OFFLINE")
        return None
    return load(path)


__all__ = ["WorldFixtures", "load", "load_if_offline"]
