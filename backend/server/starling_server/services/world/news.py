"""The shared news pool served by `GET /v1/news/headlines` (docs/architecture/news-pool-shape.md).

A background task refreshes the pool from authored sources only: publisher RSS and Atom feeds, the
Hacker News Algolia API (a fixed list of authored queries plus recent well-discussed stories on
authored hosts) and the OpenRouter public model list. Source-specific parsers produce `RawStory`
records; `normalize_item` is the one function that defines a story's fields, and `assemble_pool`
adds the pool-level ones (T2 below).

CADENCE (T1, 2026-09-21). It used to be one refresh of everything every 15 minutes, which with the
engine's own 15-minute cache and a 30-minute candidate cache meant a new story took 15-45 minutes
to become something a customer could post about. Now Hacker News and OpenRouter (where launches
surface first) are read every REFRESH_INTERVAL_S (2.5 minutes) and the RSS feeds every
SLOW_REFRESH_INTERVAL_S (5 minutes), with conditional requests where the source supports them.
Each source group keeps its last good fetch, so a fast tick re-assembles the pool from fresh
Hacker News and OpenRouter results and the feeds it did not re-read.

CLUSTERS AND VELOCITY (T2). `assemble_pool` also says how big and how fast a story is: how many
independent sources carry it (`cluster_size`), when the pool first saw it (`first_seen_at`), how fast
its Hacker News points are rising (`points_per_hour`), and whether it is `breaking` (two or more
sources within two hours, fast points growth, or a new listing from a lab the product watches).

Politeness: requests to one host are spaced by at least a second, and every response has a size
cap. XML is parsed with the standard library after refusing documents that declare entities. A
refresh that yields nothing keeps the previous pool ("stale beats blind"); `generated_at` then stays
at the last successful refresh.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Protocol
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

from ..errors import ServiceError
from . import sources as src
from .extract import parse_timestamp
from .safe_fetch import FetchResult

log = logging.getLogger(__name__)

# T1: the fast tick (Hacker News, OpenRouter) and the slow one (the feeds). Hacker News Algolia
# allows far more than the ~150 requests an hour this makes; OpenRouter's list and the feeds are
# conditional requests, so an unchanged source costs a 304.
REFRESH_INTERVAL_S = 150
SLOW_REFRESH_INTERVAL_S = 300
POOL_LIMIT = 150
# T2: what makes a story "breaking".
BREAKING_WINDOW_S = 2 * 3600
BREAKING_POINTS_PER_HOUR = 60.0
BREAKING_LISTING_AGE_S = 6 * 3600
FIRST_SEEN_MEMORY_S = 3 * 86_400
_CLUSTER_JACCARD = 0.5
_CLUSTER_STOP = frozenset("""
the a an and or for with from into onto over under this that these those its it's is are was were be
been has have had will would can could new now today just how why what when who which your our their
about after before more most than then vs via using use uses used out up off on in at of to by as
""".split())
MAX_TITLE = 300
MAX_SUMMARY = 700
MAX_SOURCE = 80
MAX_URL = 2048
MIN_FEED_SUMMARY = 40
FEED_MAX_BYTES = 2 * 1024 * 1024
HN_MAX_BYTES = 1024 * 1024
OPENROUTER_MAX_BYTES = 8 * 1024 * 1024
SOURCE_TIMEOUT_S = 10.0
HOST_SPACING_S = 1.0
MAX_CONCURRENT_FETCHES = 6
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
JSON_ACCEPT = "application/json"

ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
DUBLIN_CORE = "{http://purl.org/dc/elements/1.1/}"
CONTENT_MODULE = "{http://purl.org/rss/1.0/modules/content/}"
SUMMARY_TAGS = (
    "description", f"{RSS1}description", f"{ATOM}summary", f"{CONTENT_MODULE}encoded", f"{ATOM}content", "summary", "content",
)
DATE_TAGS = ("pubDate", f"{ATOM}published", f"{ATOM}updated", f"{DUBLIN_CORE}date", "published", "updated")

_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_DECLARATION_RE = re.compile(rb"<!ENTITY", re.IGNORECASE)
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._:-]+)*")
# Summary furniture, removed exactly as the engine does (the operation is idempotent).
_DATE_FURNITURE_RE = re.compile(
    r"(?:^|(?<=[.!?]\s))(?:[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})\s*[·•:\-–—]\s*"
)
_EMBED_CHROME_RE = re.compile(
    r"[^.!?]*\b(?:copy link|subscribe|sign up|watch now|read more|\d[\d.,]*[km]?\s+views|share this)\b[^.!?]*[.!?]?",
    re.IGNORECASE,
)
_FIRST_PERSON_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:I|I'?m|I'?ve|I'?d|my|we|we'?re|we'?ve|our)\b(?!\s*(?:phone|team's))[^.!?]*[.!?]?",
    re.IGNORECASE,
)


class HttpGetter(Protocol):
    async def get(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_s: float,
        max_redirects: int = 3,
        accept: str = "*/*",
        truncate: bool = False,
        headers: dict[str, str] | None = None,
    ) -> FetchResult: ...


class SourceError(Exception):
    """A source answered with something unusable; the message is a short category, never a URL."""


@dataclass(frozen=True)
class RawStory:
    title: str
    url: str
    source: str
    summary: str = ""
    published_at: int = 0
    points: int = 0
    lane: str | None = None


# ---------------------------------------------------------------------------- cleaning and shape


def clean_text(value: object, limit: int) -> str:
    """Plain text: tags stripped, entities unescaped, whitespace collapsed, cut to `limit`."""
    text = _TAG_RE.sub(" ", str(value or ""))
    text = _TAG_RE.sub(" ", html.unescape(text))
    return " ".join(text.split())[:limit]


def strip_furniture(text: str) -> str:
    text = _DATE_FURNITURE_RE.sub("", text or "")
    text = _EMBED_CHROME_RE.sub(" ", text)
    text = _FIRST_PERSON_SENTENCE_RE.sub(" ", text)
    return " ".join(text.split())


def http_url(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_URL or any(ch.isspace() for ch in text):
        return ""
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not hostname:
        return ""
    return text


def _whole_seconds(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float) and math.isfinite(value):
        return int(value) if value > 0 else 0
    return 0


def normalize_item(story: RawStory) -> dict[str, Any] | None:
    """The only place that defines a pool item (news-pool-shape.md, "Item"); None drops the story."""
    title = clean_text(story.title, MAX_TITLE)
    url = http_url(story.url)
    source = clean_text(story.source, MAX_SOURCE)
    if not title or not url or not source:
        return None
    summary = strip_furniture(clean_text(story.summary, 4 * MAX_SUMMARY))[:MAX_SUMMARY].strip()
    lane = story.lane.strip() if isinstance(story.lane, str) and story.lane.strip() else None
    return {
        "title": title,
        "url": url,
        "source": source,
        "summary": summary,
        "published_at": _whole_seconds(story.published_at),
        "points": _whole_seconds(story.points),
        "lane": lane,
    }


def _title_tokens(title: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9][a-z0-9.+-]*", title.lower())
            if token not in _CLUSTER_STOP and (len(token) >= 3 or any(ch.isdigit() for ch in token))}


def _name_tokens(title: str) -> set[str]:
    """Capitalised or digit-bearing words: the names a story is ABOUT."""
    return {word.lower().strip(".,:;!?()\"'") for word in title.split()
            if (word[:1].isupper() or any(ch.isdigit() for ch in word))
            and word.lower().strip(".,:;!?()\"'") not in _CLUSTER_STOP and len(word) >= 2}


def _clusters(items: list[dict[str, Any]]) -> list[int]:
    """A cluster id per item: the same story told by more than one source (union-find)."""
    parent = list(range(len(items)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    tokens = [_title_tokens(item["title"]) for item in items]
    names = [_name_tokens(item["title"]) for item in items]
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if items[left]["source"] == items[right]["source"]:
                continue
            union = tokens[left] | tokens[right]
            jaccard = len(tokens[left] & tokens[right]) / len(union) if union else 0.0
            if jaccard >= _CLUSTER_JACCARD or len(names[left] & names[right]) >= 2:
                parent[root(left)] = root(right)
    return [root(index) for index in range(len(items))]


def _appeared_at(item: dict[str, Any], remembered: bool) -> int:
    return int(item.get("published_at") or (item.get("first_seen_at") if remembered else 0) or 0)


def _is_watched_listing(item: dict[str, Any]) -> bool:
    if item.get("source") != src.OPENROUTER_SOURCE:
        return False
    model_path = urlsplit(item.get("url") or "").path.strip("/").lower()
    return any(model_path.startswith(prefix) for prefix in src.WATCHED_LAB_PREFIXES)


def assemble_pool(stories: Iterable[RawStory], limit: int = POOL_LIMIT, *,
                  now: int | None = None, first_seen: dict[str, int] | None = None,
                  previous_points: dict[str, tuple[int, int]] | None = None) -> list[dict[str, Any]]:
    """Normalize, de-duplicate on the first 70 title characters, interleave by source (ascending
    names), then say how big and how fast each story is (T2).

    `first_seen` (url -> unix seconds) and `previous_points` (url -> (points, at)) are the pool's
    memory between refreshes; both are read, never written, here. Without them every story is
    first seen `now` and velocity falls back to points over the story's age.
    """
    seen: set[str] = set()
    by_source: dict[str, list[dict[str, Any]]] = {}
    for story in stories:
        item = normalize_item(story)
        if item is None:
            continue
        key = item["title"].lower()[:70]
        if key in seen:
            continue
        seen.add(key)
        by_source.setdefault(item["source"], []).append(item)
    pool: list[dict[str, Any]] = []
    depth = max((len(items) for items in by_source.values()), default=0)
    for index in range(depth):
        for source in sorted(by_source):
            if index < len(by_source[source]):
                pool.append(by_source[source][index])
    pool = pool[:limit]
    now = int(now if now is not None else time.time())
    # With no memory (the first refresh after a start) an undated story's appearance time is
    # unknown, so it cannot count as recent: a restart must not make every old story "new".
    remembered = bool(first_seen)
    first_seen = first_seen or {}
    previous_points = previous_points or {}
    for item in pool:
        item["first_seen_at"] = int(first_seen.get(item["url"], now))
        points = int(item.get("points") or 0)
        velocity = 0.0
        if points > 0:
            before = previous_points.get(item["url"])
            if before and now > before[1]:
                velocity = max(0.0, (points - before[0]) * 3600.0 / (now - before[1]))
            elif item["published_at"] and now > item["published_at"]:
                velocity = points * 3600.0 / max(900.0, now - item["published_at"])
        item["points_per_hour"] = round(velocity, 1)
    cluster_of = _clusters(pool)
    members: dict[int, list[dict[str, Any]]] = {}
    for item, cluster in zip(pool, cluster_of):
        members.setdefault(cluster, []).append(item)
    for item, cluster in zip(pool, cluster_of):
        group = members[cluster]
        item["cluster_size"] = len({member["source"] for member in group})
        # When it appeared: its own date when the source gives one, else when the pool first
        # saw it (only when the pool has a memory to say so).
        recent_sources = {member["source"] for member in group
                          if _appeared_at(member, remembered) and now - _appeared_at(member, remembered) <= BREAKING_WINDOW_S}
        item["breaking"] = bool(
            len(recent_sources) >= 2
            or item["points_per_hour"] >= BREAKING_POINTS_PER_HOUR
            or (_is_watched_listing(item) and item["published_at"]
                and now - item["published_at"] <= BREAKING_LISTING_AGE_S))
    return pool


# ---------------------------------------------------------------------------- source parsers


def _first_text(entry: ElementTree.Element, tags: Iterable[str]) -> str:
    for tag in tags:
        element = entry.find(tag)
        if element is not None:
            text = "".join(element.itertext())
            if text.strip():
                return text
    return ""


def _entry_link(entry: ElementTree.Element) -> str:
    for tag in ("link", f"{RSS1}link"):
        element = entry.find(tag)
        if element is not None and (element.text or "").strip():
            url = http_url(element.text)
            if url:
                return url
    links = entry.findall(f"{ATOM}link")
    alternates = [link for link in links if link.get("rel", "alternate") == "alternate"]
    for link in alternates + [link for link in links if link not in alternates]:
        url = http_url(link.get("href"))
        if url:
            return url
    guid = entry.find("guid")
    if guid is not None and guid.get("isPermaLink", "true").strip().lower() != "false":
        return http_url(guid.text)
    return ""


def _entry_summary(entry: ElementTree.Element) -> str:
    for tag in SUMMARY_TAGS:
        element = entry.find(tag)
        if element is None:
            continue
        text = clean_text("".join(element.itertext()), 4 * MAX_SUMMARY)
        if len(text) >= MIN_FEED_SUMMARY:
            return strip_furniture(text)[:MAX_SUMMARY]
    return ""


def _entry_date(entry: ElementTree.Element) -> int:
    for tag in DATE_TAGS:
        element = entry.find(tag)
        if element is not None and element.text:
            value = parse_timestamp(element.text)
            if value:
                return value
    return 0


def parse_feed(content: bytes, source: str, limit: int = src.FEED_ITEMS_PER_SOURCE) -> list[RawStory]:
    """RSS 2.0, RSS 1.0 or Atom entries (at most `limit`)."""
    if _ENTITY_DECLARATION_RE.search(content):
        raise SourceError("feed_declares_entities")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        raise SourceError("feed_not_xml") from None
    entries = root.findall(".//item") or root.findall(f".//{ATOM}entry") or root.findall(f".//{RSS1}item")
    stories: list[RawStory] = []
    for entry in entries[:limit]:
        title = clean_text(_first_text(entry, ("title", f"{ATOM}title", f"{RSS1}title")), MAX_TITLE)
        link = _entry_link(entry)
        if not title or not link:
            continue
        stories.append(
            RawStory(title=title, url=link, source=source, summary=_entry_summary(entry), published_at=_entry_date(entry))
        )
    return stories


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _hits(payload: object) -> list[dict[str, Any]]:
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        raise SourceError("hn_response_without_hits")
    return [hit for hit in hits if isinstance(hit, dict)]


def parse_hn_hits(payload: object) -> list[RawStory]:
    stories: list[RawStory] = []
    for hit in _hits(payload):
        title = clean_text(hit.get("title"), MAX_TITLE)
        if not title:
            continue
        url = http_url(hit.get("url"))
        if not url:
            object_id = str(hit.get("objectID") or "")
            if not object_id.isdigit():
                continue
            url = src.HN_ITEM_URL.format(object_id)
        summary = strip_furniture(clean_text(hit.get("story_text"), 4 * MAX_SUMMARY))[:MAX_SUMMARY]
        stories.append(
            RawStory(
                title=title,
                url=url,
                source=src.HN_SOURCE,
                summary=summary,
                published_at=max(0, _int(hit.get("created_at_i"))),
                points=max(0, _int(hit.get("points"))),
            )
        )
    return stories


def is_lane_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    if any(host == lane or host.endswith("." + lane) for lane in src.LANE_HOSTS):
        return True
    location = f"{host}{parts.path}".lower()
    return any(location.startswith(prefix) for prefix in src.LANE_REPO_PREFIXES)


def parse_hn_host_hot(payload: object, now: int) -> list[RawStory]:
    stories: list[RawStory] = []
    for hit in _hits(payload):
        url = http_url(hit.get("url"))
        if not url or not is_lane_host(url):
            continue
        created = _int(hit.get("created_at_i"))
        if created <= 0 or now - created > src.HN_HOST_HOT_MAX_AGE_S:
            continue
        title = clean_text(hit.get("title"), MAX_TITLE)
        points = max(0, _int(hit.get("points")))
        if not title or points < src.HN_HOST_HOT_MIN_POINTS:
            continue
        comments = max(0, _int(hit.get("num_comments")))
        host = (urlsplit(url).hostname or "").lower()
        summary = f"On Hacker News: {points} points and {comments} comments, linking {host}."
        story_text = strip_furniture(clean_text(hit.get("story_text"), 4 * MAX_SUMMARY))
        if story_text:
            summary = f"{summary} {story_text}"
        stories.append(
            RawStory(title=title, url=url, source=src.HN_SOURCE, summary=summary[:MAX_SUMMARY], published_at=created, points=points)
        )
    stories.sort(key=lambda story: (-story.points, -story.published_at))
    return stories[: src.HN_HOST_HOT_LIMIT]


def parse_openrouter_models(payload: object, now: int) -> list[RawStory]:
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise SourceError("openrouter_response_without_data")
    stories: list[RawStory] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        created = _int(model.get("created"))
        if created <= 0 or now - created > src.OPENROUTER_MAX_AGE_S:
            continue
        description = clean_text(model.get("description"), 4 * MAX_SUMMARY)
        if len(description) < src.OPENROUTER_MIN_DESCRIPTION:
            continue
        model_id = clean_text(model.get("id"), 120)
        if not _MODEL_ID_RE.fullmatch(model_id):
            model_id = ""
        name = clean_text(model.get("name"), 120) or (model_id.split("/")[-1] if model_id else "")
        prefix = f"Listed on OpenRouter as {model_id}." if model_id else "Listed on OpenRouter."
        stories.append(
            RawStory(
                title=f"{name or 'An unnamed model'}, a newly listed model"[:MAX_TITLE],
                url=f"https://openrouter.ai/{model_id}" if model_id else src.OPENROUTER_MODELS_URL,
                source=src.OPENROUTER_SOURCE,
                summary=f"{prefix} {description}"[:MAX_SUMMARY],
                published_at=created,
                lane=src.LANE_FRONTIER,
            )
        )
        if len(stories) >= src.OPENROUTER_LIMIT:
            break
    return stories


def _json(content: bytes) -> object:
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise SourceError("invalid_json") from None


def _describe(exc: BaseException) -> str:
    if isinstance(exc, ServiceError):
        return exc.code
    if isinstance(exc, SourceError):
        return str(exc)
    return type(exc).__name__


def _matches(item: dict[str, Any], keywords: Iterable[str]) -> bool:
    haystack = f"{item['title']} {item['summary']}".lower()
    return any(
        re.search(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])", haystack) for keyword in keywords
    )


# ---------------------------------------------------------------------------- pool


@dataclass(frozen=True)
class _Validator:
    etag: str | None
    last_modified: str | None
    stories: tuple[RawStory, ...]


class NewsPool:
    def __init__(
        self,
        http: HttpGetter,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        feeds: Iterable[src.FeedSource] = src.RSS_FEEDS,
        queries: Iterable[src.AuthoredQuery] = src.AUTHORED_QUERIES,
        host_spacing_s: float = HOST_SPACING_S,
        refresh_interval_s: float = REFRESH_INTERVAL_S,
        slow_refresh_interval_s: float = SLOW_REFRESH_INTERVAL_S,
    ) -> None:
        self._http = http
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._feeds = tuple(feeds)
        self._queries = tuple(queries)
        self._host_spacing_s = host_spacing_s
        self.refresh_interval_s = refresh_interval_s
        self.slow_refresh_interval_s = slow_refresh_interval_s
        self.generated_at = 0
        self.last_attempt_at = 0
        self.refreshing = False
        self._items: list[dict[str, Any]] = []
        self._by_query: dict[str, list[dict[str, Any]]] = {}
        self._validators: dict[str, _Validator] = {}
        # T1: each source group's last good fetch, so a fast tick that reads only Hacker News
        # re-assembles the pool with the feeds it did not re-read. Insertion order is the
        # original job order, which keeps the interleave stable.
        self._groups: dict[str, list[RawStory]] = {}
        self._last_slow_at = float("-inf")
        # T2: the pool's memory between refreshes.
        self._first_seen: dict[str, int] = {}
        self._points: dict[str, tuple[int, int]] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._refresh_lock: asyncio.Lock | None = None

    def seed(self, stories: Iterable[RawStory]) -> int:
        """Fill the pool once from given stories (STARLING_WORLD_FIXTURES, e2e only)."""
        now = int(self._clock())
        items = assemble_pool(stories, now=now)
        self._remember(items, now)
        self._items = items
        self.generated_at = now
        return len(items)

    # ---- reads

    def headlines(self, limit: int = POOL_LIMIT) -> dict[str, Any]:
        return {"generated_at": int(self.generated_at), "items": [dict(item) for item in self._items[: max(0, limit)]]}

    def candidates(self, query: src.AuthoredQuery, limit: int) -> list[dict[str, Any]]:
        """Stories for an authored query: its own Hacker News results and keyword matches, newest first."""
        chosen: list[dict[str, Any]] = []
        seen: set[str] = set()
        matches = [item for item in self._items if _matches(item, query.keywords)]
        for item in [*self._by_query.get(query.id, []), *matches]:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            chosen.append(item)
        chosen.sort(key=lambda item: item["published_at"], reverse=True)
        return [dict(item) for item in chosen[:limit]]

    def freshness(self) -> dict[str, Any]:
        now = int(self._clock())
        return {
            "generated_at": self.generated_at or None,
            "age_s": now - self.generated_at if self.generated_at else None,
            "items": len(self._items),
            "refreshing": self.refreshing,
            "last_attempt_at": self.last_attempt_at or None,
            "refresh_interval_s": self.refresh_interval_s,
            "slow_refresh_interval_s": self.slow_refresh_interval_s,
            "sources": {name: dict(entry) for name, entry in sorted(self._health.items())},
        }

    # ---- refresh

    async def run(self) -> None:
        """Refresh now and then every `refresh_interval_s` until cancelled.

        Every tick reads Hacker News and OpenRouter; the feeds are read when
        `slow_refresh_interval_s` has passed since they last were (T1).
        """
        while True:
            try:
                await self.refresh(slow=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("news refresh failed (%s)", type(exc).__name__)
            await self._sleep(self.refresh_interval_s)

    async def refresh(self, *, slow: bool | None = True) -> int:
        """Refresh the pool. `slow=True` reads every source; `None` reads the slow ones only when due."""
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            self.refreshing = True
            try:
                if slow is None:
                    slow = self._monotonic() - self._last_slow_at >= self.slow_refresh_interval_s
                return await self._refresh(slow=bool(slow))
            finally:
                self.refreshing = False

    async def _refresh(self, *, slow: bool = True) -> int:
        now = int(self._clock())
        self.last_attempt_at = now
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        # (group key, health name, job). The order is the pool's interleave order.
        jobs: list[tuple[str, str, Awaitable[Any]]] = [("hn:queries", src.HN_SOURCE, self._hacker_news_queries(now))]
        if slow:
            jobs += [(f"feed:{feed.name}", feed.name, self._feed(feed)) for feed in self._feeds]
        jobs += [("openrouter", src.OPENROUTER_SOURCE, self._openrouter(now))]
        jobs += [("hn:hot", src.HN_SOURCE, self._hacker_news_host_hot(now))]
        results = await asyncio.gather(*(job for _, _, job in jobs), return_exceptions=True)
        if slow:
            self._last_slow_at = self._monotonic()

        by_query: dict[str, list[RawStory]] = {}
        counts: dict[str, int] = {}
        errors: dict[str, str] = {}
        succeeded = False
        for (key, name, _), result in zip(jobs, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                errors[name] = _describe(result)
                continue
            found: list[RawStory] = []
            if isinstance(result, dict):
                for query_id, query_stories in result.items():
                    by_query[query_id] = query_stories
                    found.extend(query_stories)
            else:
                found = list(result)
            if found:
                succeeded = True
            self._groups[key] = found
            counts[name] = counts.get(name, 0) + len(found)
        self._note_health(now, counts, errors)

        stories = [story for group in self._groups.values() for story in group]
        items = assemble_pool(stories, now=now, first_seen=self._first_seen, previous_points=self._points)
        if not items or not succeeded:
            log.warning("news refresh produced no items; the previous pool is kept")
            return 0
        self._remember(items, now)
        self._items = items
        merged = dict(self._by_query)
        for query_id, query_stories in by_query.items():
            merged[query_id] = [item for item in (normalize_item(story) for story in query_stories) if item]
        self._by_query = merged
        self.generated_at = int(self._clock())
        log.info("news pool refreshed items=%d", len(items))
        return len(items)

    def _remember(self, items: list[dict[str, Any]], now: int) -> None:
        """First sightings and the latest points, kept for FIRST_SEEN_MEMORY_S (T2)."""
        for item in items:
            self._first_seen.setdefault(item["url"], int(item.get("first_seen_at") or now))
            if int(item.get("points") or 0) > 0:
                self._points[item["url"]] = (int(item["points"]), now)
        horizon = now - FIRST_SEEN_MEMORY_S
        self._first_seen = {url: at for url, at in self._first_seen.items() if at >= horizon}
        self._points = {url: value for url, value in self._points.items() if value[1] >= horizon}

    def _note_health(self, now: int, counts: dict[str, int], errors: dict[str, str]) -> None:
        for name in set(counts) | set(errors):
            entry = self._health.setdefault(
                name, {"items": 0, "last_ok_at": None, "last_error": None, "last_error_at": None}
            )
            if name in counts:
                entry["items"] = counts[name]
                entry["last_ok_at"] = now
            if name in errors:
                entry["last_error"] = errors[name]
                entry["last_error_at"] = now

    async def _get(self, url: str, *, max_bytes: int, accept: str, conditional: bool = False) -> FetchResult:
        host = (urlsplit(url).hostname or "").lower()
        lock = self._host_locks.get(host)
        if lock is None:
            lock = self._host_locks[host] = asyncio.Lock()
        async with lock:
            wait = self._host_last.get(host, float("-inf")) + self._host_spacing_s - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
            headers: dict[str, str] = {}
            validator = self._validators.get(url) if conditional else None
            if validator is not None:
                if validator.etag:
                    headers["If-None-Match"] = validator.etag
                if validator.last_modified:
                    headers["If-Modified-Since"] = validator.last_modified
            semaphore = self._semaphore or asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
            try:
                async with semaphore:
                    return await self._http.get(
                        url,
                        max_bytes=max_bytes,
                        timeout_s=SOURCE_TIMEOUT_S,
                        max_redirects=3,
                        accept=accept,
                        headers=headers or None,
                    )
            finally:
                self._host_last[host] = self._monotonic()

    async def _feed(self, feed: src.FeedSource) -> list[RawStory]:
        result = await self._get(feed.url, max_bytes=FEED_MAX_BYTES, accept=FEED_ACCEPT, conditional=True)
        validator = self._validators.get(feed.url)
        if result.status == 304 and validator is not None:
            return list(validator.stories)
        if result.status != 200:
            raise SourceError(f"http_{result.status}")
        stories = parse_feed(result.content, feed.name)
        etag, modified = result.headers.get("etag"), result.headers.get("last-modified")
        if etag or modified:
            self._validators[feed.url] = _Validator(etag, modified, tuple(stories))
        else:
            self._validators.pop(feed.url, None)
        return stories

    async def _hacker_news_queries(self, now: int) -> dict[str, list[RawStory]]:
        cutoff = now - src.HN_QUERY_MAX_AGE_S
        found: dict[str, list[RawStory]] = {}
        failures = 0
        for query in self._queries:
            params = {
                "query": query.topic,
                "tags": "story",
                "hitsPerPage": src.HN_PER_QUERY,
                "numericFilters": f"created_at_i>{cutoff}",
            }
            url = str(httpx.URL(src.HN_SEARCH_BY_DATE_URL, params=params))
            try:
                result = await self._get(url, max_bytes=HN_MAX_BYTES, accept=JSON_ACCEPT)
                if result.status != 200:
                    raise SourceError(f"http_{result.status}")
                found[query.id] = parse_hn_hits(_json(result.content))
            except (ServiceError, SourceError):
                failures += 1
        if self._queries and failures == len(self._queries):
            raise SourceError("every_hn_query_failed")
        return found

    async def _hacker_news_host_hot(self, now: int) -> list[RawStory]:
        params = {
            "tags": "story",
            "hitsPerPage": src.HN_HOST_HOT_HITS,
            "numericFilters": f"created_at_i>{now - src.HN_HOST_HOT_MAX_AGE_S},points>{src.HN_HOST_HOT_MIN_POINTS}",
        }
        result = await self._get(str(httpx.URL(src.HN_SEARCH_URL, params=params)), max_bytes=HN_MAX_BYTES, accept=JSON_ACCEPT)
        if result.status != 200:
            raise SourceError(f"http_{result.status}")
        return parse_hn_host_hot(_json(result.content), now)

    async def _openrouter(self, now: int) -> list[RawStory]:
        # Conditional like the feeds (T1): the model list is megabytes, and it is read on every
        # fast tick because a new listing from a watched lab is breaking news (T2).
        result = await self._get(src.OPENROUTER_MODELS_URL, max_bytes=OPENROUTER_MAX_BYTES,
                                 accept=JSON_ACCEPT, conditional=True)
        validator = self._validators.get(src.OPENROUTER_MODELS_URL)
        if result.status == 304 and validator is not None:
            # An unchanged list is still read through the 48-hour window.
            return [story for story in validator.stories if now - story.published_at <= src.OPENROUTER_MAX_AGE_S]
        if result.status != 200:
            raise SourceError(f"http_{result.status}")
        stories = parse_openrouter_models(_json(result.content), now)
        etag, modified = result.headers.get("etag"), result.headers.get("last-modified")
        if etag or modified:
            self._validators[src.OPENROUTER_MODELS_URL] = _Validator(etag, modified, tuple(stories))
        else:
            self._validators.pop(src.OPENROUTER_MODELS_URL, None)
        return stories
