"""What is new in THIS account's beat: stored research, the shared pool, ranked for Create post.

API-NATIVE PASS (2026-09-22). Create post and chat ideas used to come only from an authored
AI/developer-tools news pool filtered by AI-builder heuristics, so a strength coach or a
SaaS founder got "I have nothing current worth posting about right now" with no AI call at
all. Topics now come from the account's own beat:

  * THE BEATS are the voice card's (model-written, `voice_card.beats`) or, from the first
    press, the deterministic niche read (`niche.beats`);
  * BACKGROUND RESEARCH: while the account is active, at most every `RESEARCH_INTERVAL_S`
    and `DAILY_RUNS` times a UTC day, one web search for the stalest beat, sent as
    background work (services `/v1/search` with `X-Starling-Bucket: background` and
    `purpose: "topics"`, metered inside the account's background share and ceiling, with
    the service's own daily cap). It runs after a press, never on it (`background.py`);
  * A PRESS-TIME SEARCH only when a Create post or an ideas question finds nothing stored
    and nothing in the pool: one search inside the press's own caps, kept for next time;
  * THE SHARED POOL stays one source among others, matched to the beats by words.

Every candidate keeps its page (url, title) so a post built on it lists it on the card
(`decision.sources`, at most three: no fact without its link). Ranking: freshness first,
then velocity (several sources, seen again), fit to the beat, fit to what performed for
this account; a subject the account posted about recently is not offered again.

PRIVACY: the query is built only from beat labels (short topic phrases; see `niche.py` for
what may become one), never from a post, a draft or a note's sentences.
"""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

RESEARCH_INTERVAL_S = 45 * 60      # per account, while active
ACTIVE_WINDOW_S = 24 * 3600        # "active": a press in the last day
DAILY_RUNS = 8                     # background runs a UTC day (the service caps at 12)
REFUSED_BACKOFF_S = 6 * 3600       # after the service says the cap or the share is used up
FAILED_BACKOFF_S = 20 * 60
FRESH_S = 72 * 3600                # older findings are not offered as news
USED_RECENTLY_S = 3 * 86400
MAX_STORED = 200
OPTIONS = 3                        # a created post is offered at most three stories (three pages)
FACT_CAP = 330
POOL_FIT_FLOOR = 0.25
_STATE_KEY = "topic_research_state"
_LOCK = threading.Lock()


# ---------------------------------------------------------------- storage

def _ensure_table(conn) -> None:
    conn.execute("""
      CREATE TABLE IF NOT EXISTS topic_candidates (
        url TEXT PRIMARY KEY,
        beat TEXT NOT NULL,
        title TEXT NOT NULL,
        fact TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        sentence_key TEXT NOT NULL DEFAULT '',
        found_at REAL NOT NULL,
        first_seen REAL NOT NULL,
        seen_count INTEGER NOT NULL DEFAULT 1,
        origin TEXT NOT NULL DEFAULT 'background',
        used_at REAL
      )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_topic_candidates_found ON topic_candidates(found_at DESC)")


def _connect():
    from .storage import connect
    return connect()


def stored(*, max_age_s: float = FRESH_S, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    try:
        with _connect() as conn:
            _ensure_table(conn)
            rows = conn.execute("SELECT * FROM topic_candidates WHERE found_at >= ? ORDER BY found_at DESC",
                                (now - max_age_s,)).fetchall()
    except Exception:                                        # noqa: BLE001
        return []
    return [dict(row) for row in rows]


def _store(findings: list[dict], beat: str, origin: str, now: float) -> int:
    if not findings:
        return 0
    try:
        with _connect() as conn:
            _ensure_table(conn)
            for item in findings:
                conn.execute("""
                  INSERT INTO topic_candidates(url,beat,title,fact,source,sentence_key,found_at,first_seen,
                                               seen_count,origin)
                  VALUES(?,?,?,?,?,?,?,?,1,?)
                  ON CONFLICT(url) DO UPDATE SET found_at=excluded.found_at, fact=excluded.fact,
                    title=excluded.title, seen_count=topic_candidates.seen_count + 1""",
                             (item["url"], beat, item["title"], item["fact"], item["source"],
                              item["sentence_key"], now, now, origin))
            conn.execute("DELETE FROM topic_candidates WHERE url NOT IN "
                         "(SELECT url FROM topic_candidates ORDER BY found_at DESC LIMIT ?)", (MAX_STORED,))
    except Exception:                                        # noqa: BLE001
        return 0
    return len(findings)


def mark_used(url: str, now: float | None = None) -> None:
    if not url:
        return
    try:
        with _connect() as conn:
            _ensure_table(conn)
            conn.execute("UPDATE topic_candidates SET used_at=? WHERE url=?", (time.time() if now is None else now, url))
    except Exception:                                        # noqa: BLE001
        pass


def clear_for_harness() -> None:
    """Measurement harness only: forget every stored finding and the schedule."""
    try:
        with _connect() as conn:
            _ensure_table(conn)
            conn.execute("DELETE FROM topic_candidates")
        from .storage import set_job_state
        set_job_state(_STATE_KEY, {})
    except Exception:                                        # noqa: BLE001
        pass


# ---------------------------------------------------------------- schedule

def _state() -> dict:
    try:
        from .storage import get_job_state
        return get_job_state(_STATE_KEY) or {}
    except Exception:                                        # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        from .storage import set_job_state
        set_job_state(_STATE_KEY, state)
    except Exception:                                        # noqa: BLE001
        pass


def mark_active(now: float | None = None) -> None:
    """A press happened: the account is active (the research job only runs while it is)."""
    with _LOCK:
        state = _state()
        state["last_press_at"] = time.time() if now is None else now
        _save_state(state)


def _utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def due(now: float | None = None) -> tuple[bool, str, str]:
    """(run now?, the beat to research, why) for the background job."""
    now = time.time() if now is None else now
    state = _state()
    if now - float(state.get("last_press_at") or 0) > ACTIVE_WINDOW_S:
        return False, "", "inactive"
    if now < float(state.get("backoff_until") or 0):
        return False, "", "backoff"
    if now - float(state.get("last_run_at") or 0) < RESEARCH_INTERVAL_S:
        return False, "", "recent"
    runs = state.get("runs") or {}
    if int(runs.get(_utc_day(now)) or 0) >= DAILY_RUNS:
        return False, "", "daily_cap"
    beats = _beats()
    if not beats:
        return False, "", "no_beats"
    last = state.get("beat_last") or {}
    beat = min(beats, key=lambda item: (float(last.get(item) or 0), beats.index(item)))
    return True, beat, "due"


def _beats() -> list[str]:
    try:
        from .niche import beats
        return [str(beat).strip() for beat in beats() if str(beat).strip()]
    except Exception:                                        # noqa: BLE001
        return []


# ---------------------------------------------------------------- the search

def query_for(beat: str, others: list[str] | None = None) -> str:
    """The web-search query for one beat: only beat labels, never any text of the account's."""
    beat = " ".join(str(beat or "").split())[:80]
    also = [" ".join(str(item).split())[:40] for item in (others or []) if item and item != beat][:2]
    query = (f"What is new in the last few days in {beat}? Recent news, releases, studies, launches "
             f"and debates about {beat}")
    if also:
        query += " (the writer also covers " + ", ".join(also) + ")"
    return query[:300]


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _sentence_key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower())[:12])


def findings_from(answer: dict | None) -> list[dict]:
    """The developments a search answer reports: one per cited page, with its sentence."""
    if not isinstance(answer, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for source in answer.get("sources") or []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url.lower().startswith(("https://", "http://")) or url in seen or any(ch.isspace() for ch in url):
            continue
        fact = " ".join(str(source.get("snippet") or "").split())[:FACT_CAP]
        if len(fact) < 20:
            continue
        seen.add(url)
        title = " ".join(str(source.get("title") or "").split())[:160] or _host(url)
        out.append({"url": url, "title": title, "fact": fact, "source": _host(url),
                    "sentence_key": _sentence_key(fact)})
    return out


def run(beat: str, *, background: bool = True, now: float | None = None) -> tuple[list[dict], str]:
    """One research search for `beat`. Returns (the findings stored, the search status)."""
    from . import cloud
    now = time.time() if now is None else now
    beats = _beats()
    query = query_for(beat, [item for item in beats if item != beat])
    answer, status = cloud.search(query, purpose="topics", background=background)
    findings = findings_from(answer) if status == "ok" else []
    _store(findings, beat, "background" if background else "press", now)
    return findings, status


def run_if_due(now: float | None = None) -> dict:
    """The background job: research the stalest beat when one is due. Never raises."""
    now = time.time() if now is None else now
    with _LOCK:
        ok, beat, why = due(now)
        if not ok:
            return {"ran": False, "why": why}
        state = _state()
        state["last_run_at"] = now
        runs = {day: count for day, count in (state.get("runs") or {}).items() if day == _utc_day(now)}
        runs[_utc_day(now)] = int(runs.get(_utc_day(now)) or 0) + 1
        state["runs"] = runs
        last = dict(state.get("beat_last") or {})
        last[beat] = now
        state["beat_last"] = last
        _save_state(state)
    try:
        findings, status = run(beat, background=True, now=now)
    except Exception:                                        # noqa: BLE001
        findings, status = [], "unavailable"
    if status in ("capped", "refused"):
        # The service says when its cap opens again (a paced slot, the next day, the period's
        # end); without that, the fixed back-off.
        retry = getattr(status, "retry_after_s", None)
        wait = float(retry) if retry else REFUSED_BACKOFF_S
        with _LOCK:
            state = _state()
            state["backoff_until"] = now + max(FAILED_BACKOFF_S, min(wait, 31 * 86_400.0))
            state["refused_reason"] = getattr(status, "reason", None)
            _save_state(state)
    elif status != "ok":
        with _LOCK:
            state = _state()
            state["backoff_until"] = now + FAILED_BACKOFF_S
            _save_state(state)
    return {"ran": True, "beat": beat, "status": str(status), "reason": getattr(status, "reason", None),
            "found": len(findings)}


def press_search(now: float | None = None) -> tuple[list[dict], str]:
    """A press found nothing stored and nothing in the pool: one search now, inside its caps."""
    beats = _beats()
    if not beats:
        return [], "no_beats"
    now = time.time() if now is None else now
    state = _state()
    last = state.get("beat_last") or {}
    beat = min(beats, key=lambda item: (float(last.get(item) or 0), beats.index(item)))
    try:
        findings, status = run(beat, background=False, now=now)
    except Exception:                                        # noqa: BLE001
        return [], "unavailable"
    with _LOCK:
        state = _state()
        last = dict(state.get("beat_last") or {})
        last[beat] = now
        state["beat_last"] = last
        _save_state(state)
    return findings, status


# ---------------------------------------------------------------- the candidates

def _pool_stories() -> list[dict]:
    try:
        from .news import headlines
        return list(headlines(None, limit=150))
    except Exception:                                        # noqa: BLE001
        return []


def _top_texts(limit: int = 20) -> list[str]:
    """The account's best-performing own texts, for "fit to what performed"."""
    try:
        from .intelligence import get_personal_model
        rows = [row for row in get_personal_model().example_rows
                if row.get("shareable") is not False and float(row.get("performance") or 0) > 0]
    except Exception:                                        # noqa: BLE001
        return []
    rows.sort(key=lambda row: -float(row.get("performance") or 0))
    return [str(row.get("text") or "") for row in rows[:limit]]


def _recent_topics() -> list[str]:
    try:
        from .radar import recent_post_topics
        return recent_post_topics()
    except Exception:                                        # noqa: BLE001
        return []


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9][a-z0-9'+#-]{2,}", str(text or "").lower())}


def _repeats(title: str, recent: list[str]) -> bool:
    words = _words(title)
    for topic in recent[:6]:
        other = _words(topic)
        if words and other and len(words & other) / max(1, min(len(words), len(other))) >= 0.6:
            return True
    return False


def _age_label(seconds: float) -> str:
    if seconds < 3600:
        return f"found {max(1, int(seconds // 60))} min ago"
    if seconds < 48 * 3600:
        return f"found {int(seconds // 3600)} h ago"
    return f"found {int(seconds // 86400)} days ago"


def candidates(limit: int = OPTIONS, *, now: float | None = None) -> list[dict]:
    """The best `limit` stories for a Create post or an ideas answer, freshest first.

    Each is {"title", "fact", "url", "source", "age_s", "velocity", "fit", "origin", "score"}.
    """
    from .niche import current, fit
    from .voice_examples import bm25_scores
    now = time.time() if now is None else now
    niche = current()
    recent = _recent_topics()
    pool: dict[str, dict] = {}

    # Stored research (this account's own beat, from the web).
    rows = stored(now=now)
    by_sentence: dict[str, int] = {}
    for row in rows:
        by_sentence[row.get("sentence_key") or row["url"]] = by_sentence.get(row.get("sentence_key") or row["url"], 0) + 1
    for row in rows:
        if row.get("used_at") and now - float(row["used_at"]) < USED_RECENTLY_S:
            continue
        sources = by_sentence.get(row.get("sentence_key") or row["url"], 1)
        pool[row["url"]] = {
            "title": row["title"], "fact": row["fact"], "url": row["url"], "source": row["source"],
            "age_s": max(0.0, now - float(row["found_at"])),
            "velocity": math.log1p(sources) + 0.3 * max(0, int(row.get("seen_count") or 1) - 1),
            "fit": max(0.6, fit(f"{row['title']} {row['fact']}", niche)["score"]),   # researched FOR the beat
            "origin": "research", "beat": row.get("beat"),
        }
    # The shared pool, where it touches the beat.
    for story in _pool_stories():
        url = str(story.get("url") or "")
        if not url or url in pool:
            continue
        text = f"{story.get('title') or ''} {story.get('summary') or ''}"
        story_fit = fit(text, niche)["score"]
        if story_fit < POOL_FIT_FLOOR:
            continue
        published = float(story.get("created_at") or 0) or float(story.get("first_seen_at") or 0)
        fact = " ".join(str(story.get("summary") or "").split())
        fact = (f"{story.get('title')}. {fact}" if fact else str(story.get("title") or ""))[:FACT_CAP]
        pool[url] = {
            "title": str(story.get("title") or "")[:160], "fact": fact, "url": url,
            "source": str(story.get("source") or _host(url)),
            "age_s": max(0.0, now - published) if published else 36 * 3600.0,
            "velocity": math.log1p(int(story.get("cluster_size") or 1)) + (0.8 if story.get("breaking") else 0.0),
            "fit": story_fit, "origin": "pool",
        }
    items = [item for item in pool.values() if not _repeats(item["title"], recent)]
    if not items:
        return []
    performed = _top_texts()
    if performed:
        documents = [f"{item['title']} {item['fact']}" for item in items]
        # How close each story is to the account's best posts (0..1, BM25 against their words).
        scores = [0.0] * len(items)
        for text in performed[:10]:
            for index, value in enumerate(bm25_scores(text, documents)):
                scores[index] = max(scores[index], value)
    else:
        scores = [0.0] * len(items)
    for item, performance in zip(items, scores):
        freshness = math.exp(-item["age_s"] / (12 * 3600.0))
        item["score"] = round(2.0 * freshness + 0.6 * item["velocity"] + 1.2 * item["fit"]
                              + 0.4 * performance, 4)
        item["age"] = _age_label(item["age_s"])
    items.sort(key=lambda item: (-item["score"], item["age_s"], item["url"]))
    return items[:max(0, limit)]


def options_text(options: list[dict]) -> str:
    """The numbered stories as the writer's facts block (never a URL: links stay on the card)."""
    lines = []
    for index, item in enumerate(options, start=1):
        fact = item["fact"]
        title = item["title"]
        body = fact if fact.lower().startswith(title.lower()[:40]) else f"{title}. {fact}"
        lines.append(f"[{index}] {body} ({item['source']}, {item['age']})")
    return "\n".join(lines)


def grounding_for(options: list[dict], chosen: int | None = None) -> dict:
    """The grounding the card's `decision.sources` reads: the chosen story's page first."""
    order = list(range(len(options)))
    if chosen is not None and 0 <= chosen < len(options):
        order = [chosen] + [index for index in order if index != chosen]
    sources = [{"kind": "search" if options[index].get("origin") == "research" else "news",
                "url": options[index]["url"], "title": options[index]["title"],
                "fact": options[index]["fact"]} for index in order]
    return {"sources": sources, "topic": options[order[0]]["title"] if options else None}


def ideas_for_chat(limit: int = 3) -> list[dict]:
    """Post ideas for chat: the same candidates, as plain dicts for the evidence brief."""
    return [{"story": item["title"], "fact": item["fact"], "source": item["source"], "url": item["url"],
             "age": item["age"]} for item in candidates(limit)]


def status() -> dict:
    state = _state()
    return {"last_run_at": state.get("last_run_at"), "backoff_until": state.get("backoff_until"),
            "refused_reason": state.get("refused_reason"),
            "stored": len(stored()), "beats": _beats(), "runs": state.get("runs") or {}}


__all__ = ["candidates", "due", "findings_from", "grounding_for", "ideas_for_chat", "mark_active",
           "mark_used", "options_text", "press_search", "query_for", "run", "run_if_due", "status", "stored"]
