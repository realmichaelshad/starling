"""What happened today, from the shared news pool (CONTRACTS §4.2).

The world service refreshes its pool from authored sources (Hacker News and
OpenRouter every 2.5 minutes, the feeds every 5) and serves it at
`GET /v1/news/headlines` (shape: docs/architecture/news-pool-shape.md). This
module reads it, cleans every story the same way for every consumer, and keeps a
per-customer copy for 2.5 minutes (T1: it was 15, on top of the pool's own 15).
A pool whose `generated_at` has not moved is not processed again.

The request carries nothing about the account. Personalisation happens after the
pool arrives: `radar._web_candidates` filters it against the account's themes.
"""
from __future__ import annotations

import html
import math
import re
import time
from typing import Any

from . import cloud
from .storage import get_job_state, set_job_state

_CACHE_KEY = "news_headlines"
_CACHE_TTL_S = 150.0        # T1: the shared pool's fast tick
# A forced refresh still respects a floor: a pool fetched seconds ago is already
# what a refresh would return.
_FORCE_REFRESH_FLOOR_S = 60.0
_POOL_LIMIT = 150
_MAX_TITLE_CHARS = 300
_MAX_SOURCE_CHARS = 80
_MAX_SUMMARY_CHARS = 700
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


# BLOG-INDEX FURNITURE IS NOT A FACT. A landing-page summary arrives as rows
# of "Jun 29, 2026 · Undo makes AI coding agents effective..." and the writer
# copies the row whole — measured live, a post shipped containing "Jul 13,
# 2026 · AI agents are becoming more capable." verbatim, date dot and all. The
# dates are index chrome, not evidence; stripping them keeps the sentence and
# loses the furniture. Applied at ingest so every consumer of the summary —
# the writer, the support checker, the figure gate — sees the same text.
_DATE_FURNITURE_RE = re.compile(
    r"(?:^|(?<=[.!?]\s))(?:[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})"
    r"\s*[·•:\-–—]\s*")

# EMBED CHROME IS NOT A FACT EITHER. A scraped page carries its social widgets
# into the summary — measured live, a Snapshield summary arrived with "Copy
# link.Học Claude Code từ con số 0 …" (a video embed row) and the draft came
# out ABOUT the widget: "The 5-minute AI video got 358k views." Sentences that
# are platform chrome — share buttons, subscribe rows, view counts — are cut
# whole at ingest, same as the date rows and for the same reason: every
# consumer of the summary must see facts, not furniture.
_EMBED_CHROME_RE = re.compile(
    r"[^.!?]*\b(?:copy link|subscribe|sign up|watch now|read more|"
    r"\d[\d.,]*[km]?\s+views|share this)\b[^.!?]*[.!?]?", re.IGNORECASE)

# THE AUTHOR'S VOICE IS NOT A FACT — the same rule source_facts applies to
# fetched pages, missed here on the summary path. An HN story_text is the
# submitter talking ("We did it with Exabase", "I tested it against..."),
# and carried whole into evidence it becomes the WRITER's claim: two live
# drafts on 2026-07-28 said "We did it with Exabase" and "My local LLM
# scored 6/6" in his voice. First-person sentences are cut at ingest, same
# stage and same reason as the furniture rows. The guard matches
# source_facts's: "my phone", "our team's" style possessives survive.
_FIRST_PERSON_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:I|I'?m|I'?ve|I'?d|my|we|we'?re|we'?ve|our)\b"
    r"(?!\s*(?:phone|team's))[^.!?]*[.!?]?", re.IGNORECASE)


def _strip_furniture(text: str) -> str:
    text = _DATE_FURNITURE_RE.sub("", text or "")
    text = _EMBED_CHROME_RE.sub(" ", text)
    text = _FIRST_PERSON_SENTENCE_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _epoch(value: Any) -> int:
    """Whole unix seconds, or 0 meaning UNKNOWN for anything that is not a real time."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return int(value) if value > 0 else 0


def _story(raw: Any) -> dict[str, Any] | None:
    """One pool item as the engine's story record, or None when it is unusable."""
    if not isinstance(raw, dict):
        return None
    title = _clean(str(raw.get("title") or ""))[:_MAX_TITLE_CHARS]
    url = str(raw.get("url") or "").strip()
    if not title or not url.lower().startswith(("https://", "http://")):
        return None
    points = raw.get("points")
    story: dict[str, Any] = {
        "title": title,
        "url": url,
        "source": _clean(str(raw.get("source") or ""))[:_MAX_SOURCE_CHARS] or "News",
        "points": points if isinstance(points, int) and not isinstance(points, bool) and points > 0 else 0,
        # The pool says `published_at`; every consumer in the engine reads
        # `created_at`. 0 = UNKNOWN, and no time claim may be made from it.
        "created_at": _epoch(raw.get("published_at")),
        "summary": _strip_furniture(_clean(str(raw.get("summary") or "")))[:_MAX_SUMMARY_CHARS],
    }
    lane = raw.get("lane")
    if isinstance(lane, str) and lane.strip():
        story["lane"] = lane.strip()
    # T2: how big and how fast the story is, when the pool says so (additive
    # fields: an older pool without them yields a story without them).
    cluster_size = raw.get("cluster_size")
    if isinstance(cluster_size, int) and not isinstance(cluster_size, bool) and cluster_size > 0:
        story["cluster_size"] = min(cluster_size, 50)
    first_seen = _epoch(raw.get("first_seen_at"))
    if first_seen:
        story["first_seen_at"] = first_seen
    velocity = raw.get("points_per_hour")
    if (isinstance(velocity, (int, float)) and not isinstance(velocity, bool)
            and math.isfinite(velocity) and velocity > 0):
        story["points_per_hour"] = round(float(velocity), 1)
    if raw.get("breaking") is True:
        story["breaking"] = True
    return story


def _dedupe_and_interleave(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First-seen title wins; then one story from each source in turn, so no
    single source can fill the pool."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = item["title"].lower()[:70]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in deduped:
        by_source.setdefault(str(item.get("source") or "?"), []).append(item)
    interleaved: list[dict[str, Any]] = []
    for index in range(max((len(group) for group in by_source.values()), default=0)):
        for source in sorted(by_source):
            if index < len(by_source[source]):
                interleaved.append(by_source[source][index])
    return interleaved


def _record_source_health(counts: dict[str, int], *, note: str = "") -> None:
    """Write down what each source delivered.

    A source that yields nothing looks exactly like a quiet news day unless it is
    counted, so the counts are merged into job_state and a source that newly
    yields nothing is written to the safety log once.
    """
    try:
        from .storage import save_safety_event
        stored = get_job_state("news_source_health") or {}
        previous = stored.get("sources") or {}
        # MERGE, never replace: a partial report must not erase the other sources.
        merged = {**previous, **counts}
        dead = sorted(name for name, count in merged.items() if not count)
        newly_dead = [name for name in counts
                      if not counts[name] and (previous.get(name, 0) > 0 or name not in previous)]
        record: dict[str, Any] = {"at": time.time(), "sources": merged, "dead": dead}
        if note:
            record["note"] = note
        set_job_state("news_source_health", record)
        if newly_dead:
            save_safety_event("news", "medium", False, ["source_yielded_nothing"],
                              {"sources": newly_dead, "counts": merged, "note": note})
    except Exception:                                    # noqa: BLE001
        pass


def _record_blackout(stale_count: int) -> None:
    """A refresh that brought nothing at all, written down where it can be seen."""
    try:
        from .storage import save_safety_event
        save_safety_event("news", "medium", False, ["news_refresh_empty"],
                          {"stale_items_served": stale_count})
    except Exception:                                    # noqa: BLE001
        pass


def pool_stamp() -> int:
    """The `generated_at` of the pool this engine holds (0 when unknown).

    Lets a consumer that caches something BUILT from the pool (radar's candidate
    list) tell that the pool under it has moved on, without a request.
    """
    cached = get_job_state(_CACHE_KEY) or {}
    try:
        return int(cached.get("generated_at") or 0)
    except (TypeError, ValueError):
        return 0


def headlines(themes: list[str] | None = None, *, limit: int = 24,
              force: bool = False) -> list[dict[str, Any]]:
    """What happened today, in this product's lane. Cached for 2.5 minutes.

    `themes` is accepted and deliberately unused: nothing derived from the account
    is sent with the request, and callers filter the returned pool themselves.
    """
    del themes
    from .research import research_enabled
    cached = get_job_state(_CACHE_KEY) or {}
    cached_items = list(cached.get("items") or [])
    if not research_enabled():
        # The off switch stops the request; a pool already stored is not one.
        return cached_items[:limit]
    age = time.time() - float(cached.get("at") or 0)
    if cached_items and not force and age < _CACHE_TTL_S:
        return cached_items[:limit]
    if cached_items and force and age < _FORCE_REFRESH_FLOOR_S:
        return cached_items[:limit]
    if not cloud.is_configured():
        return cached_items[:limit]

    pool = cloud.news_headlines(_POOL_LIMIT)
    generated_at = _epoch(pool.get("generated_at")) if isinstance(pool, dict) else 0
    raw_items = pool.get("items") if isinstance(pool, dict) else None
    if (cached_items and generated_at and isinstance(raw_items, list) and raw_items
            and generated_at == _epoch(cached.get("generated_at"))):
        # The same pool as last time: nothing to process again (T1).
        set_job_state(_CACHE_KEY, {**cached, "at": time.time()})
        return cached_items[:limit]
    stories = ([story for story in (_story(item) for item in raw_items) if story is not None]
               if isinstance(raw_items, list) else [])
    fresh = _dedupe_and_interleave(stories)
    if fresh:
        set_job_state(_CACHE_KEY, {"at": time.time(), "generated_at": generated_at,
                                   "items": fresh[:_POOL_LIMIT]})
        counts: dict[str, int] = {}
        for story in fresh:
            counts[story["source"]] = counts.get(story["source"], 0) + 1
        _record_source_health(counts)
        return fresh[:limit]
    # Stale beats blind, but say so.
    _record_blackout(len(cached_items))
    return cached_items[:limit]
