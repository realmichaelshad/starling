"""Read one public post on X through X's published oEmbed endpoint (the website's writing page).

People on phones can usually copy only a post's link from the X app, not its words. The writing
page (`web/write.py`) sends that link to the gateway, the gateway asks the services process
(`POST /v1/x/post`, admin token), and only the services process talks to the internet:

- `parse_status_url` accepts a link to ONE post on x.com or twitter.com and keeps its numeric id
  and the author's handle. The oEmbed request is rebuilt from those two values, so nothing else the
  pasted link carried (tracking parameters, other text) is sent anywhere.
- `XPostReader.read` calls `https://publish.twitter.com/oembed?url=<post>&omit_script=1&dnt=true`
  through the `SafeFetcher` (public addresses only, a short timeout, a size cap). It never loads
  x.com pages and never calls X's internal endpoints: oEmbed is X's published interface for
  showing a public post elsewhere.
- The returned blockquote becomes plain text, the author's name and handle, and the language.
- Answers are cached per post id: 10 minutes for a post, 2 minutes for one X would not show.

Nothing here logs a link, a handle or post text.
"""
from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from ..errors import ServiceError
from .safe_fetch import FetchError, SafeFetcher

OEMBED_ENDPOINT = "https://publish.twitter.com/oembed"
OEMBED_TIMEOUT_S = 5.0
OEMBED_MAX_BYTES = 64 * 1024
OEMBED_MAX_REDIRECTS = 2
OEMBED_ACCEPT = "application/json"
CACHE_TTL_S = 600.0
MISS_TTL_S = 120.0
CACHE_MAX_ENTRIES = 1000
MAX_TEXT_CHARS = 10_000
MAX_NAME_CHARS = 200
MAX_LINKS = 4
# Twitter's epoch for post ids (Snowflake ids carry their creation time in milliseconds).
SNOWFLAKE_EPOCH_MS = 1_288_834_974_657
SNOWFLAKE_FIRST_ID = 1 << 32  # older numeric ids carry no time

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_STATUS_RE = re.compile(
    r"(?<![A-Za-z0-9.-])(?:https?://)?(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/"
    r"(?:i/web|(?P<handle>[A-Za-z0-9_]{1,15}))/status(?:es)?/(?P<id>[0-9]{1,25})",
    re.IGNORECASE,
)
_MEDIA_HOSTS = ("pic.twitter.com/", "pic.x.com/")
_RESERVED_HANDLES = frozenset({"i", "home", "intent", "search", "hashtag", "share"})


@dataclass(frozen=True)
class StatusLink:
    status_id: str
    handle: str | None

    @property
    def url(self) -> str:
        """The post's address on x.com (the handle is only a label: X finds a post by its id)."""
        return f"https://x.com/{self.handle or 'i'}/status/{self.status_id}"

    @property
    def oembed_target(self) -> str:
        return f"https://twitter.com/{self.handle or 'i'}/status/{self.status_id}"


def parse_status_url(text: object) -> StatusLink | None:
    """The first link to one post on x.com or twitter.com in `text`, or None."""
    if not isinstance(text, str) or not text:
        return None
    match = _STATUS_RE.search(text[:4000])
    if match is None:
        return None
    status_id = match.group("id").lstrip("0")
    if not status_id:
        return None
    handle = match.group("handle")
    if handle is not None and handle.lower() in _RESERVED_HANDLES:
        handle = None
    return StatusLink(status_id, handle)


def snowflake_time(status_id: str | int | None, now: float | None = None) -> int | None:
    """When a post was created (unix seconds), read from its id; None for ids that carry no time."""
    try:
        value = int(status_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value < SNOWFLAKE_FIRST_ID:
        return None
    seconds = ((value >> 22) + SNOWFLAKE_EPOCH_MS) // 1000
    latest = (now if now is not None else time.time()) + 86_400
    if seconds < 1_288_834_974 or seconds > latest:
        return None
    return int(seconds)


class _QuoteText(HTMLParser):
    """Plain text of the first <p> inside the oEmbed blockquote, its links and its language."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth_blockquote = 0
        self.in_p = False
        self.done = False
        self.parts: list[str] = []
        self.lang: str | None = None
        self.links: list[str] = []
        self.has_media = False
        self._anchor_href: str | None = None
        self._anchor_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        values = {name: value or "" for name, value in attrs}
        if tag == "blockquote":
            self.depth_blockquote += 1
        elif tag == "p" and self.depth_blockquote and not self.in_p:
            self.in_p = True
            lang = values.get("lang", "").strip()
            self.lang = lang[:16] if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", lang) else None
        elif tag == "br" and self.in_p:
            self.parts.append("\n")
        elif tag == "a" and self.in_p:
            self._anchor_href = values.get("href", "")
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if self.done:
            return
        if tag == "a" and self.in_p and self._anchor_text is not None:
            text = "".join(self._anchor_text)
            href = self._anchor_href or ""
            if text.strip().lower().startswith(_MEDIA_HOSTS):
                self.has_media = True  # the photo or video itself; oEmbed gives no description of it
            else:
                self.parts.append(text)
                host = (urlsplit(href).hostname or "").lower() if href.startswith(("https://", "http://")) else ""
                if host == "t.co" and href not in self.links and len(self.links) < MAX_LINKS:
                    self.links.append(href[:2000])
            self._anchor_href = None
            self._anchor_text = None
        elif tag == "p" and self.in_p:
            self.in_p = False
            self.done = True
        elif tag == "blockquote" and self.depth_blockquote:
            self.depth_blockquote -= 1

    def handle_data(self, data: str) -> None:
        if self.done or not self.in_p:
            return
        if self._anchor_text is not None:
            self._anchor_text.append(data)
        else:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts).replace("\r\n", "\n").replace("\r", "\n")
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:MAX_TEXT_CHARS]


def parse_oembed(payload: object, link: StatusLink) -> dict[str, Any]:
    """The post as the writing page uses it, from an oEmbed answer. Raises ServiceError when the
    answer is not an oEmbed answer for a post."""
    if not isinstance(payload, dict) or not isinstance(payload.get("html"), str):
        raise ServiceError(502, "fetch_failed", "X's embed service answered without a post")
    parser = _QuoteText()
    try:
        parser.feed(payload["html"][:OEMBED_MAX_BYTES])
        parser.close()
    except Exception:  # malformed markup reads as no text rather than as a crash
        parser.parts = []
    author_name = payload.get("author_name")
    author_name = " ".join(author_name.split())[:MAX_NAME_CHARS] if isinstance(author_name, str) else ""
    handle = link.handle
    author_url = payload.get("author_url")
    if isinstance(author_url, str):
        parts = urlsplit(author_url)
        candidate = parts.path.strip("/").split("/")[0] if (parts.hostname or "").lower() in (
            "twitter.com", "www.twitter.com", "x.com", "www.x.com") else ""
        if _HANDLE_RE.match(candidate) and candidate.lower() not in _RESERVED_HANDLES:
            handle = candidate
    resolved = StatusLink(link.status_id, handle)
    return {
        "url": resolved.url,
        "status_id": link.status_id,
        "author_name": author_name,
        "author_handle": handle,
        "text": parser.text(),
        "lang": parser.lang,
        "links": parser.links,
        "has_media": parser.has_media,
    }


class XPostReader:
    """oEmbed lookups with a per-post cache. One instance per services process."""

    def __init__(self, fetcher: SafeFetcher, *, monotonic: Callable[[], float] = time.monotonic,
                 endpoint: str = OEMBED_ENDPOINT) -> None:
        self._fetcher = fetcher
        self._monotonic = monotonic
        self._endpoint = endpoint
        self._cache: OrderedDict[str, tuple[float, dict[str, Any] | ServiceError]] = OrderedDict()

    def oembed_url(self, link: StatusLink) -> str:
        return f"{self._endpoint}?url={quote(link.oembed_target, safe='')}&omit_script=1&dnt=true"

    def _cached(self, status_id: str) -> dict[str, Any] | ServiceError | None:
        entry = self._cache.get(status_id)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= self._monotonic():
            self._cache.pop(status_id, None)
            return None
        self._cache.move_to_end(status_id)
        return value

    def _remember(self, status_id: str, value: dict[str, Any] | ServiceError, ttl_s: float) -> None:
        self._cache[status_id] = (self._monotonic() + ttl_s, value)
        self._cache.move_to_end(status_id)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    async def read(self, raw_url: object) -> dict[str, Any]:
        link = parse_status_url(raw_url)
        if link is None:
            raise ServiceError(400, "bad_request", "url must be a link to one post on x.com or twitter.com")
        cached = self._cached(link.status_id)
        if isinstance(cached, ServiceError):
            raise cached
        if cached is not None:
            return {**cached, "cached": True}
        try:
            result = await self._fetcher.get(
                self.oembed_url(link),
                max_bytes=OEMBED_MAX_BYTES,
                timeout_s=OEMBED_TIMEOUT_S,
                max_redirects=OEMBED_MAX_REDIRECTS,
                accept=OEMBED_ACCEPT,
                truncate=False,
            )
        except FetchError as exc:
            if exc.http_status == 413:
                raise ServiceError(502, "fetch_failed", "X's embed service sent more than expected") from None
            raise
        if result.status in (403, 404, 410):
            # Deleted, protected, age-restricted or never existed: X will not show it outside X.
            missing = ServiceError(404, "post_unavailable", "X does not show this post outside X")
            self._remember(link.status_id, missing, MISS_TTL_S)
            raise missing
        if not 200 <= result.status < 300:
            raise ServiceError(502, "fetch_failed", f"X's embed service answered with HTTP {result.status}")
        try:
            payload = json.loads(result.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ServiceError(502, "fetch_failed", "X's embed service sent something that is not JSON") from None
        post = parse_oembed(payload, link)
        self._remember(link.status_id, post, CACHE_TTL_S)
        return {**post, "cached": False}
