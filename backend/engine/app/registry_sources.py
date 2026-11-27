"""Shaping for two kinds of story that are identified by WHERE they appeared.

Every other filter in this product is a keyword filter. That is fine until a lab
ships under a codename chosen precisely so that nobody has seen the word before:
`builder_fit("Ox Alpha")` and `names_notable("Ox Alpha")` are both False, so a
keyword gate drops the story whatever the query set finds. The instruments that
caught such a story were provenance-based and asked no question at all:

    a model registry listing   (a new entry under /api/v1/models)
    Hacker News by host        (a loud recent story linking a lane-only host)

THE ENGINE DOES NOT FETCH THESE. All web data comes through the services world
service (CONTRACTS §4.2), whose shared news pool may carry such records. What
stays here is the pure shaping: given raw records a caller already holds, emit
news-pool stories with honest titles, evidence in the summary and the lane
declared from provenance. Without `_fixture` records the producers return an
empty list.

THE RECORD CONTRACT:

    {"title": str, "url": str, "source": str,
     "points": int, "created_at": int, "summary": str}

    created_at   0 means UNKNOWN and the caller must make no time claim.
    summary      IS the evidence. `topic_audition.MATERIAL_FLOOR` is 120 chars
                 beyond the label, so a record without real material is dropped
                 here rather than downstream.
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable
from urllib.parse import urlsplit

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"

SOURCE_OPENROUTER = "OpenRouter"
# Deliberately the existing Hacker News source name: host-hot results merge into
# the same bucket instead of doubling that source's share of the round-robin.
SOURCE_HACKER_NEWS = "Hacker News"
SOURCE_NAMES: frozenset[str] = frozenset({SOURCE_OPENROUTER, SOURCE_HACKER_NEWS})

# Must match `lane.FRONTIER` exactly; a test asserts the two agree.
LANE_FRONTIER = "frontier models"

# A model listed in the last 48 h is news; older is the back catalogue.
MODEL_MAX_AGE_S = 48 * 3600

# HN host-hot: a story needs real conversation behind it before it is worth a
# reader's attention.
HN_MIN_POINTS = 50
HN_HITS = 100

# AUTHORED hosts: a story is on the lane because of WHERE it was published, which
# needs no vocabulary. `github.com` is narrowed to specific orgs, because the bare
# host pulls in everything from editors to web servers.
LANE_HOSTS: tuple[str, ...] = (
    "openrouter.ai",
    "anthropic.com",
    "openai.com",
    "deepmind.google",
    "simonwillison.net",
    "huggingface.co",
    "cursor.com",
    "github.blog",
)
LANE_REPO_PREFIXES: tuple[str, ...] = (
    "github.com/anthropics/",
    "github.com/openai/",
    "github.com/sst/opencode",
    "github.com/google-gemini/",
    "github.com/cline/",
    "github.com/BerriAI/",
)

_MIN_SUMMARY = 120          # topic_audition.MATERIAL_FLOOR
_MAX_SUMMARY = 700          # news._MAX_SUMMARY_CHARS
_WS = re.compile(r"\s+")


def _clean(text: Any, limit: int = _MAX_SUMMARY) -> str:
    return _WS.sub(" ", str(text or "")).strip()[:limit]


def _ago(hours: float) -> str:
    """An observed age in words, singular-correct ("1 day ago", never "1 days ago")."""
    if hours < 1:
        return "in the last hour"
    if hours < 24:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = int(hours // 24)
    return f"{n} day{'s' if n != 1 else ''} ago"


def _plain(html: Any) -> str:
    """HTML -> text. Returns "" rather than a half-stripped fragment."""
    raw = str(html or "")
    if not raw.strip():
        return ""
    import html as _html
    text = re.sub(r"<[^>]+>", " ", raw)
    text = _html.unescape(text)
    # A URL that survived the tag strip is noise in prose, not evidence.
    text = re.sub(r"https?://\S+", "", text)
    text = _clean(text, 300)
    # A link-only comment leaves punctuation and nothing else once tags and URLs
    # are gone. Anything without a letter or digit is not material.
    return text if re.search(r"[A-Za-z0-9]", text) else ""


def _host_of(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().lstrip("www.")
    except ValueError:
        return ""


def is_lane_host(url: str) -> bool:
    """Does this URL come from somewhere that only publishes this lane?

    Host-level for publishers, org-prefixed for GitHub. No vocabulary anywhere.
    """
    host = _host_of(url)
    if not host:
        return False
    if host in LANE_HOSTS:
        return True
    stripped = re.sub(r"^https?://(?:www\.)?", "", str(url or ""), flags=re.I)
    return any(stripped.lower().startswith(p) for p in LANE_REPO_PREFIXES)


def model_title(name: str, model_id: str = "") -> str:
    """Report WHERE the record was found; never claim anything about the thing.

    `builder_fit("Ox Alpha")` is False; `builder_fit("Ox Alpha, a newly listed
    model")` is True because "model" is builder vocabulary, and every word of the
    suffix is an observation about the endpoint, not a claim that the model is
    good, released or real.

    The host is deliberately NOT in the title. Appended there it became a
    SUBJECT for exactly the codename case (two codenamed listings collided as the
    same subject), and it lent the registry's notability to an unknown name. It
    rides in `summary` instead, where it is evidence rather than subject.
    """
    label = _clean(name or model_id, 90) or "An unnamed model"
    return f"{label}, a newly listed model"


def openrouter_models(*, limit: int = 12, timeout: float = 8.0,
                      _fixture: Iterable[dict] | None = None,
                      now: float | None = None) -> list[dict]:
    """Models listed in the last 48 hours, shaped from caller-supplied records.

    The engine performs no network request here: without `_fixture` the result
    is empty.
    """
    del timeout
    if _fixture is None:
        return []
    clock = time.time() if now is None else float(now)
    out: list[dict] = []
    for model in list(_fixture):
        if not isinstance(model, dict):
            continue
        try:
            created = int(model.get("created") or 0)
        except (TypeError, ValueError):
            created = 0
        # 0 means UNKNOWN. An undated model cannot be called new, and "new" is the
        # only reason this source exists, so it is dropped rather than guessed at.
        if created <= 0 or (clock - created) > MODEL_MAX_AGE_S:
            continue
        summary = _clean(model.get("description"))
        if len(summary) < _MIN_SUMMARY:
            continue
        model_id = _clean(model.get("id"), 120)
        name = _clean(model.get("name"), 120) or model_id.split("/")[-1]
        out.append({
            "title": model_title(name, model_id),
            "url": f"https://openrouter.ai/{model_id}" if model_id else OPENROUTER_MODELS,
            "source": SOURCE_OPENROUTER,
            "points": 0,
            "created_at": created,
            # THE LANE IS KNOWN FROM PROVENANCE, NOT FROM THE WORDS. A model
            # appearing in a model registry for the first time IS the frontier
            # models lane, by where it was found; `radar` prefers this over
            # classifying a codename it has no vocabulary for.
            "lane": LANE_FRONTIER,
            "lane_source": "registry",
            "summary": f"Listed on OpenRouter as {model_id}. {summary}" if model_id
                       else f"Listed on OpenRouter. {summary}",
        })
        if len(out) >= limit:
            break
    return out


def hacker_news_by_host(*, limit: int = 12, timeout: float = 8.0,
                        _fixture: Iterable[dict] | None = None,
                        now: float | None = None) -> list[dict]:
    """The loudest recent stories published on lane-only hosts, shaped from
    caller-supplied search hits. No network request; empty without `_fixture`.

    The points and comment counts are observed, and they go in the summary so the
    writer has the number rather than an assertion that people are talking.
    """
    del timeout
    if _fixture is None:
        return []
    clock = time.time() if now is None else float(now)
    out: list[dict] = []
    for hit in list(_fixture):
        if not isinstance(hit, dict):
            continue
        url = _clean(hit.get("url"), 500)
        if not url or not is_lane_host(url):
            continue
        try:
            created = int(hit.get("created_at_i") or 0)
        except (TypeError, ValueError):
            created = 0
        if created <= 0:
            continue
        title = _clean(hit.get("title"), 300)
        if not title:
            continue
        try:
            points = int(hit.get("points") or 0)
            comments = int(hit.get("num_comments") or 0)
        except (TypeError, ValueError):
            points, comments = 0, 0
        hours = max(0.0, (clock - created) / 3600.0)
        when = _ago(hours)
        # Story text arrives as HTML with entity-escaped URLs; strip to text or
        # drop it, so no anchor tags reach the writer's evidence.
        story = _plain(hit.get("story_text"))
        summary = (f"On Hacker News: {points} points and {comments} comments, "
                   f"posted {when}, linking {_host_of(url)}.")
        if story:
            summary = f"{summary} {story}"[:_MAX_SUMMARY]
        out.append({
            "title": title,
            "url": url,
            "source": SOURCE_HACKER_NEWS,
            "points": points,
            "created_at": created,
            "summary": summary,
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda r: (-int(r.get("points") or 0), -int(r.get("created_at") or 0)))
    return out[:limit]
