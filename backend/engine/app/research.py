"""Web material through the Starling world service (CONTRACTS §4.2).

The engine makes no web requests of its own. What it reads from the web comes
from the services process:

* page text for a public URL: `POST /v1/fetch` (the service resolves the host,
  refuses private addresses, connects to the validated IP, re-validates every
  redirect and caps size and time);
* summaries for AUTHORED queries: `POST /v1/summary` with an id from
  `AUTHORED_QUERIES`, never free text;
* the shared news pool: `GET /v1/news/headlines` (see `news.py`);
* one web search for a press: `POST /v1/search` (W4), only through
  `search_web(..., claim_check=True)`, which only the claim check of the post
  being answered calls. Every other `search_web` caller gets an empty list, which
  it already treats as "no evidence found".

`STARLING_RESEARCH=off` stops all of it; `proxy` (the default) allows it.

What can leave the engine, stated precisely:

* URLs the user pasted into chat, or that appear in the post being answered;
* the ids of authored queries;
* one sentence of the PUBLIC post being answered, as a web search query, when
  it carries a checkable, time-sensitive claim its own links and the news pool
  do not cover (W4);
* nothing derived from the account's own archive, and never the text of a
  protected post (callers check visibility before asking).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

from . import cloud
from .safety import safe_source_url
from .storage import connect, _utc

PAGE_TEXT_LIMIT = 6_000
TREND_TTL_MINUTES = 45
GENERAL_TTL_HOURS = 24

# AUTHORED research topics, shared with the world service by id. The engine never
# sends free text to `/v1/summary`: a question that is not one of these is
# answered without web research.
AUTHORED_QUERIES: dict[str, str] = {
    "ai-model-releases": "AI model release",
    "coding-agents": "coding agent",
    "llm-pricing": "LLM pricing",
    "ai-developer-tools": "developer tools AI",
    "open-weight-models": "open source model",
}
AUTHORED_QUERY_TEXTS: tuple[str, ...] = tuple(AUTHORED_QUERIES.values())


def research_mode() -> str:
    """`off` or `proxy`. Anything other than an explicit off means proxy."""
    value = os.environ.get("STARLING_RESEARCH", "proxy").strip().lower()
    return "off" if value in ("off", "0", "false", "no", "none") else "proxy"


def research_enabled() -> bool:
    return research_mode() != "off"


def authored_query_id(query: str) -> str | None:
    """The id of an authored query, matched by id or by its exact text."""
    wanted = re.sub(r"\s+", " ", str(query or "")).strip().lower()
    if not wanted:
        return None
    for query_id, text in AUTHORED_QUERIES.items():
        if wanted in (query_id, text.lower()):
            return query_id
    return None


def _safe_to_fetch(url: str) -> bool:
    """A syntactic pre-check only. The world service resolves the host, refuses
    private addresses and re-validates every redirect before it connects."""
    return safe_source_url(url)


class _TextExtractor(HTMLParser):
    """HTML in, readable blocks out — the page the way a reader sees it.

    THIS USED TO RETURN A SOUP. Every text node was appended to one list and
    the caller joined it with spaces, so the page arrived as a single
    whitespace-collapsed line with no boundary anywhere. Three things went
    wrong with that, and all three reached the model as fact:

      * ADJACENT NODES FUSED. A heading ran into its first paragraph and a
        table's cells ran into each other, so "Model" / "Price" / "$0.90"
        arrived as "Model Price $0.90" and a figure could be read against the
        wrong label. Nothing downstream could tell a sentence boundary from a
        cell boundary because the document no longer had either.
      * THE ARTICLE WAS OPTIONAL. Text was taken in document order and cut at
        `PAGE_TEXT_LIMIT`, and on a news page the first few thousand
        characters are the cookie notice, the nav and the promo rail — so a
        long page could be truncated before the piece it was fetched for.
      * STRUCTURE WAS DISCARDED. A list of five model prices and a paragraph
        mentioning one price are different evidence, and both arrived as prose.

    So block elements now close a block, headings and list items keep a marker,
    and `<main>`/`<article>` content is preferred when the page marks it. The
    output is small, plain and markdown-ish, which is what the reader of this
    text is: a small model that has to answer a question from it.
    """

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header",
            # Boilerplate that reliably survives the tag filter and reads as
            # content: consent dialogs, subscribe forms, and the button text
            # scattered through every modern page ("Accept all", "Sign in").
            "aside", "form", "button", "select", "option", "template",
            "dialog", "figcaption", "picture", "video", "audio", "canvas"}

    # Anything that ends a line for a reader.
    BLOCK = {"p", "div", "br", "section", "article", "main", "ul", "ol", "li",
             "table", "tr", "td", "th", "thead", "tbody", "blockquote", "pre",
             "h1", "h2", "h3", "h4", "h5", "h6", "dl", "dt", "dd", "figure",
             "hr", "address", "fieldset", "details", "summary"}

    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    # Where the page says its own content lives.
    ARTICLE = {"main", "article"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []          # every block, in document order
        self.article: list[str] = []        # only what <main>/<article> held
        self._skip_depth = 0
        self._article_depth = 0
        self._buffer: list[str] = []
        self._prefix = ""
        # THE PAGE'S OWN PREVIEW IMAGE, taken on the way past.
        #
        # `og:image` is the picture a publisher emits specifically so that it
        # appears when the article is shared. We already download this HTML to
        # extract facts and then let the markup go out of scope, so the image
        # cost us a request we had already paid for. Captured here rather than
        # in a second fetch.
        #
        # First one wins: pages that emit several are ordering them by
        # preference, and later tags are usually per-network variants.
        self.og_image: str | None = None
        # THE IMAGE'S OWN WORDS, from the same <head>.
        #
        # `score_visual_candidate` asks whether a candidate DEPICTS the brief's
        # subject, and it reads a text description to decide. Handing it the
        # radar topic instead of the image's own metadata is handing a
        # text-matching gate the wrong text — it then rejects good article
        # images and the rejection means nothing. og:title and og:description
        # are what the publisher says this page is about, in the same parse.
        self.og_title: str | None = None
        self.og_description: str | None = None

    # -- block assembly ----------------------------------------------------
    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        self._buffer.clear()
        prefix, self._prefix = self._prefix, ""
        if not text:
            return
        block = f"{prefix}{text}"
        self.parts.append(block)
        if self._article_depth:
            self.article.append(block)

    def _capture_meta(self, attrs: list) -> None:
        """Record og:image / og:title / og:description if this <meta> has one.

        First value of each wins: a page emitting several is ordering them by
        preference, and later tags are usually per-network variants.
        """
        pairs = {str(k).lower(): str(v or "") for k, v in attrs}
        key = (pairs.get("property") or pairs.get("name") or "").lower()
        value = pairs.get("content", "").strip()
        if key in ("og:title", "twitter:title") and not self.og_title:
            self.og_title = value or None
            return
        if (key in ("og:description", "twitter:description", "description")
                and not self.og_description):
            self.og_description = value or None
            return
        if key not in ("og:image", "og:image:url", "twitter:image"):
            return
        # FIRST IMAGE WINS. Restructuring this function to also capture the
        # title and description dropped the early return that enforced it, and
        # a page emitting two og:image tags began keeping the LAST. The test
        # caught it; the guard is explicit now rather than positional.
        if self.og_image:
            return
        # A PAGE CAN SHIP A PLACEHOLDER AND STILL PARSE. Measured on
        # qwenlm.github.io, whose og:image content was the literal string
        # "<link or path of image for opengraph...>" — URL-encoded by the time
        # it is resolved, fetchable, and useless. A "has og:image" check passes
        # it; this does not.
        if not value or "<" in value or "%3c" in value.lower():
            return
        self.og_image = value

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "meta":
            self._capture_meta(attrs)
        if tag in self.SKIP:
            self._flush()
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK:
            self._flush()
        if tag in self.ARTICLE:
            self._article_depth += 1
        # Markers, not markup: "## " and "- " survive whitespace collapsing and
        # tell the reader what it is looking at, where <h2> and <li> could not.
        if tag in self.HEADINGS:
            self._prefix = "#" * int(tag[1]) + " "
        elif tag == "li":
            self._prefix = "- "

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        # <br/> and friends never reach handle_starttag as a pair. <meta/> is
        # usually written self-closing, so it arrives here and not above.
        if tag == "meta":
            self._capture_meta(attrs)
        if tag in self.BLOCK and not self._skip_depth:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK:
            self._flush()
        if tag in self.ARTICLE and self._article_depth:
            self._article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if not data.strip():
            # A WHITESPACE-ONLY NODE IS A WORD BOUNDARY, and dropping it fused
            # the words on either side. The old extractor got this for free by
            # joining every node with a space; buffering the text made the gap
            # between two inline tags load-bearing, and the guard that skipped
            # empty nodes threw it away. Found on a real page, not a fixture:
            # `<a>user123</a> <span>40 minutes ago</span>` came back as
            # "user12340 minutes ago", and "Hacker News" ran into the nav.
            if self._buffer and not self._buffer[-1].endswith(" "):
                self._buffer.append(" ")
            return
        # Kept with its surrounding spaces: inline tags (<a>, <em>, <b>) are
        # NOT block elements, so their text rejoins the sentence it belongs to
        # rather than becoming a block of its own.
        self._buffer.append(data)

    def close(self) -> None:                        # noqa: D102
        self._flush()
        super().close()

    def text(self) -> str:
        """The page's own content when it marks it, the whole page otherwise."""
        blocks = self.parts
        # A <main> holding one breadcrumb is not the article. The threshold is
        # deliberately low — any real body of text beats the full page, which
        # carries the nav and the footer with it.
        joined_article = "\n".join(self.article)
        if len(joined_article) >= 400:
            blocks = self.article
        out: list[str] = []
        for block in blocks:
            # A page that repeats its own nav labels in-body ("Home", "Menu")
            # is common; one-word blocks carry nothing either way.
            if len(block.strip(" #-")) < 2:
                continue
            if out and block == out[-1]:
                continue
            out.append(block)
        return "\n".join(out).strip()


class SearchUnavailable(Exception):
    """A claim check that produced no results, and why (W4): "capped", "refused",
    "late" or "unavailable" (see `cloud.search`). Callers that treat any failure as
    "no evidence found" can keep catching Exception."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def search_web(query: str, max_results: int = 6, *, claim_check: bool = False,
               timeout_s: float | None = None) -> list[dict[str, str]]:
    """General web search, for exactly one caller: the claim check of a public post (W4).

    The service runs the provider's web search tool, metered as AI cost, at most once
    per press and never past the account's plan (services/world/search.py). The only
    caller allowed to spend that is `radar.context_grounding`, which passes
    `claim_check=True` after deciding that the post it answers carries a checkable,
    time-sensitive claim that the post's own links and the news pool do not cover.
    Every other caller (the release radar, source facts, the analyzer's corroboration)
    keeps the empty answer it always had, so no path can spend a search by accident.

    Results are `{"title", "url", "snippet"}`, where the snippet is the sentence of the
    provider's answer that cites the page: OpenAI requires anything drawn from web
    results to be shown with its source, so a sentence never travels without its URL.
    Raises `SearchUnavailable` when the check could not run.
    """
    if not claim_check or not research_enabled():
        return []
    text = " ".join(str(query or "").split())[:300]
    if len(text) < 8:
        return []
    data, status = cloud.search(text, timeout_s=cloud.SEARCH_TIMEOUT_S if timeout_s is None else timeout_s)
    if data is None:
        raise SearchUnavailable(status)
    results: list[dict[str, str]] = []
    for item in list(data.get("sources") or []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("https://", "http://")) or any(ch.isspace() for ch in url):
            continue
        snippet = " ".join(str(item.get("snippet") or "").split())[:300]
        if not snippet:
            continue                     # nothing to cite this page FOR
        results.append({"title": " ".join(str(item.get("title") or "").split())[:200],
                        "url": url, "snippet": snippet})
        if len(results) >= max(1, int(max_results)):
            break
    return results


def fetch_page_text(url: str) -> str | None:
    """Read one public page through the world service. None on any failure."""
    result = _fetch_page(url)
    return result[0] if result else None


def fetch_page_text_and_image(url: str) -> tuple[str, str | None] | None:
    """The same single read, as (text, preview image URL).

    The world service returns no preview image, so the second value is None.
    """
    result = _fetch_page(url)
    return (result[0], result[1]) if result else None


def fetch_page_preview(url: str, *, timeout_s: float | None = None) -> dict[str, str | None] | None:
    """One read: the page text, its title, and (always None here) a preview image."""
    result = _fetch_page(url, timeout_s=timeout_s)
    if not result:
        return None
    text, image, title, description = result
    return {"text": text, "image": image, "title": title, "description": description}


# S4: a page read for one step is often read again moments later (the audition's
# prefetch, then its own read; a second press on the same post). A short, small,
# in-process cache answers those, and a read already in flight is joined, not repeated:
# the world service allows 60 fetches a minute per caller. A failed read is remembered
# only briefly, so a transient failure is retried on the next press.
PAGE_CACHE_TTL_S = 600.0
PAGE_FAILURE_TTL_S = 30.0
_PAGE_CACHE_MAX = 64
_PAGE_CACHE: dict[str, tuple[float, Any]] = {}
_PAGE_INFLIGHT: dict[str, Any] = {}
_PAGE_LOCK = threading.Lock()


def _page_cached(url: str) -> tuple[bool, Any]:
    hit = _PAGE_CACHE.get(url)
    if hit is None:
        return False, None
    ttl = PAGE_CACHE_TTL_S if hit[1] is not None else PAGE_FAILURE_TTL_S
    if time.monotonic() - hit[0] >= ttl:
        _PAGE_CACHE.pop(url, None)
        return False, None
    return True, hit[1]


def reset_page_cache() -> None:
    """Test hook: forget every cached page read."""
    with _PAGE_LOCK:
        _PAGE_CACHE.clear()


def _fetch_page(url: str, *, timeout_s: float | None = None
                ) -> tuple[str, str | None, str | None, str | None] | None:
    if not research_enabled() or not _safe_to_fetch(url):
        return None
    wait_s = float(timeout_s if timeout_s is not None else cloud.WORLD_TIMEOUT_S)
    with _PAGE_LOCK:
        found, value = _page_cached(url)
        if found:
            return value
        event = _PAGE_INFLIGHT.get(url)
        owner = event is None
        if owner:
            event = _PAGE_INFLIGHT[url] = threading.Event()
    if not owner:
        # The same page is being read right now: wait for that read (within this
        # caller's own time) instead of asking the service twice.
        event.wait(timeout=max(0.1, wait_s))
        with _PAGE_LOCK:
            found, value = _page_cached(url)
        return value if found else None
    result = None
    try:
        result = _fetch_page_uncached(url, timeout_s=timeout_s)
        return result
    finally:
        with _PAGE_LOCK:
            _PAGE_CACHE[url] = (time.monotonic(), result)
            if len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
                for stale in sorted(_PAGE_CACHE, key=lambda key: _PAGE_CACHE[key][0])[:len(_PAGE_CACHE) - _PAGE_CACHE_MAX]:
                    _PAGE_CACHE.pop(stale, None)
            _PAGE_INFLIGHT.pop(url, None)
        event.set()


def _fetch_page_uncached(url: str, *, timeout_s: float | None = None
                         ) -> tuple[str, str | None, str | None, str | None] | None:
    data = cloud.fetch(url) if timeout_s is None else cloud.fetch(url, timeout_s=timeout_s)
    if not data:
        return None
    text = "\n".join(line.strip() for line in str(data.get("text") or "").splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= 100:
        return None
    title = str(data.get("title") or "").strip() or None
    if len(text) > PAGE_TEXT_LIMIT:
        # Cut at a block, not mid-sentence: half a clause reads as a whole claim.
        cut = text[:PAGE_TEXT_LIMIT]
        boundary = cut.rfind("\n")
        text = (cut[:boundary] if boundary > PAGE_TEXT_LIMIT // 2 else cut).strip()
    return text, None, title, None


def _cache_lookup(query_hash: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT answer,sources_json,created_at,expires_at FROM research_cache WHERE query_hash=? ORDER BY created_at DESC LIMIT 1",
            (query_hash,)).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(str(row["expires_at"]))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return None
    except ValueError:
        return None
    return {"status": "cached", "answer": str(row["answer"]),
            "sources": json.loads(row["sources_json"] or "[]"), "disagreements": [],
            "limitation": "Cached research summary."}


def _cache_store(query_hash: str, query: str, answer: str, sources: list[dict], trend_query: bool) -> None:
    ttl = timedelta(minutes=TREND_TTL_MINUTES) if trend_query else timedelta(hours=GENERAL_TTL_HOURS)
    expires = (datetime.now(timezone.utc) + ttl).isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO research_cache(id,query_hash,query,answer,sources_json,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (query_hash, query_hash, query[:400], answer, json.dumps(sources), _utc(), expires))


def _unavailable(answer: str, limitation: str) -> dict[str, Any]:
    return {"status": "unavailable", "answer": answer, "sources": [],
            "disagreements": [], "limitation": limitation}


def research(query: str, client: Any = None, budget_seconds: float = 12.0) -> dict[str, Any]:
    """A world-service summary for an authored query. Honest on failure.

    `client` and `budget_seconds` are accepted for the callers' signature; the
    summary is prepared by the service and the call is capped by the request's
    own deadline.
    """
    del client, budget_seconds
    if not research_enabled():
        return _unavailable("Live web research is switched off (STARLING_RESEARCH=off).",
                            "Research disabled by configuration.")
    query = str(query or "").strip()
    if not query:
        return _unavailable("There is nothing to research.", "Empty query.")
    query_id = authored_query_id(query)
    if query_id is None:
        return _unavailable(
            "Web research covers Starling's curated topics only, so I will not present "
            "unverified claims as researched facts.",
            "Free-text web research is not available.")
    query_hash = hashlib.sha256(f"summary:{query_id}".encode("utf-8")).hexdigest()
    cached = _cache_lookup(query_hash)
    if cached:
        return cached
    data = cloud.summary(query_id) or {}
    answer = str(data.get("answer") or data.get("summary") or "")
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL).strip()
    if not answer:
        return _unavailable(
            "The research summary for this topic is not available right now, so I will "
            "not present unverified claims as researched facts.",
            "Summary unavailable.")
    sources: list[dict[str, Any]] = []
    for item in list(data.get("sources") or [])[:6]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("https://", "http://")):
            continue
        published = item.get("published_at")
        sources.append({
            "title": str(item.get("title") or "").strip()[:200],
            "url": url,
            "published_at": published if isinstance(published, int) and published > 0 else None,
            "snippet": str(item.get("snippet") or "").strip()[:200],
        })
    _cache_store(query_hash, AUTHORED_QUERIES[query_id], answer, sources, trend_query=True)
    return {"status": "used", "answer": answer, "sources": sources, "disagreements": [],
            "limitation": "Summary of curated sources prepared by the Starling world service; "
                          "verify before citing numbers."}
