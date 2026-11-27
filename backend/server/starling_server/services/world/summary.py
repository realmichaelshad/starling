"""`POST /v1/summary`: a cached, cited digest for an authored query id (never free text).

The engine sends only authored ids (news-pool-shape.md). The digest is written from the shared news
pool (no customer data) through the proxy logic with bucket `system`, so owner pause and global
budgets apply and the spend is recorded in `ai_calls`. Results are cached for 60 minutes per id;
concurrent requests for one id share a single generation. When generation fails, a stale digest is
served if one exists.

Response: `{"query_id", "answer", "summary" (alias of answer), "sources": [{"title", "url",
"published_at", "snippet"}] (at most 6), "generated_at"}`. An empty `answer` means "unavailable".
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ...config import Settings
from ..errors import ServiceError
from ..metering import BUCKET_SYSTEM, CallContext
from ..proxy import ChatProxy, ChatRequest, model_for_lane
from . import sources as src
from .news import NewsPool

log = logging.getLogger(__name__)

SUMMARY_TTL_S = 3_600
MAX_SOURCES = 6
SNIPPET_CHARS = 280
MAX_ANSWER_CHARS = 1_200
SUMMARY_LANE = "starling-fast"
SUMMARY_MAX_OUTPUT_TOKENS = 300
SYSTEM_IDENTITY = "starling-world-summary"
NO_ANSWER = "NONE"
SYSTEM_PROMPT = (
    "You write a short news digest for a writer. Use only the numbered sources provided. "
    "Write at most five short sentences about what happened recently on the topic. "
    "After each sentence, cite the sources it relies on as [n]. "
    "Do not add facts, names, dates or numbers that are not in the sources. "
    f"If the sources say nothing relevant about the topic, reply with {NO_ANSWER} only."
)


def _answer_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return ""
    text = " ".join(content.split())
    if not text or text.strip(" .").upper() == NO_ANSWER:
        return ""
    return text[:MAX_ANSWER_CHARS]


class SummaryService:
    def __init__(
        self,
        pool: NewsPool,
        proxy: ChatProxy,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        offline: bool = False,
    ) -> None:
        self._pool = pool
        self._proxy = proxy
        self._settings = settings
        self._clock = clock
        # STARLING_WORLD_OFFLINE: the pool is never refreshed, so a digest would have no
        # sources. It is refused with the world service's own `fetch_failed` instead of
        # spending an AI call on nothing (CONTRACTS 4.2).
        self.offline = bool(offline)
        self._cache: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, query_id: object) -> dict[str, Any]:
        query = src.QUERIES_BY_ID.get(query_id) if isinstance(query_id, str) else None
        if query is None:
            raise ServiceError(400, "bad_request", "query_id must be one of the authored query ids")
        if self.offline:
            raise ServiceError(502, "fetch_failed", "the world service is offline; no digest is generated")
        cached = self._fresh(query.id)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(query.id, asyncio.Lock())
        async with lock:
            cached = self._fresh(query.id)
            if cached is not None:
                return cached
            try:
                payload = await self._generate(query)
            except ServiceError:
                stale = self._cache.get(query.id)
                if stale is not None:
                    return dict(stale)
                raise
            if payload["sources"]:
                self._cache[query.id] = payload
            return dict(payload)

    def status(self) -> dict[str, Any]:
        now = self._clock()
        return {
            query_id: {
                "generated_at": payload["generated_at"],
                "age_s": int(now - payload["generated_at"]),
                "sources": len(payload["sources"]),
                "answered": bool(payload["answer"]),
            }
            for query_id, payload in sorted(self._cache.items())
        }

    def _fresh(self, query_id: str) -> dict[str, Any] | None:
        cached = self._cache.get(query_id)
        if cached is not None and self._clock() - cached["generated_at"] < SUMMARY_TTL_S:
            return dict(cached)
        return None

    async def _generate(self, query: src.AuthoredQuery) -> dict[str, Any]:
        now = int(self._clock())
        items = self._pool.candidates(query, MAX_SOURCES)
        sources = [
            {
                "title": item["title"],
                "url": item["url"],
                "published_at": item["published_at"],
                "snippet": item["summary"][:SNIPPET_CHARS],
            }
            for item in items
        ]
        if not sources:
            return {"query_id": query.id, "answer": "", "summary": "", "sources": [], "generated_at": now}
        prompt = self._prompt(query, items)
        chat = ChatRequest(
            lane=SUMMARY_LANE,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            max_output_tokens=min(SUMMARY_MAX_OUTPUT_TOKENS, self._settings.ai_max_output_tokens),
            input_chars=len(SYSTEM_PROMPT) + len(prompt),
        )
        ctx = CallContext(bucket=BUCKET_SYSTEM, lane=SUMMARY_LANE, model=model_for_lane(self._settings, SUMMARY_LANE))
        payload, _ = await self._proxy.complete_json(chat, ctx, SYSTEM_IDENTITY)
        answer = _answer_text(payload)
        log.info("summary generated query=%s sources=%d answered=%s", query.id, len(sources), bool(answer))
        return {"query_id": query.id, "answer": answer, "summary": answer, "sources": sources, "generated_at": now}

    @staticmethod
    def _prompt(query: src.AuthoredQuery, items: list[dict[str, Any]]) -> str:
        lines = [f"Topic: {query.topic}", "", "Sources:"]
        for index, item in enumerate(items, 1):
            published = item["published_at"]
            when = (
                datetime.fromtimestamp(published, tz=timezone.utc).strftime("%Y-%m-%d") if published else "date unknown"
            )
            lines.append(f"[{index}] {item['title']} ({item['source']}, {when})")
            if item["summary"]:
                lines.append(item["summary"][:SNIPPET_CHARS])
        return "\n".join(lines)
