"""Make X's trend cards mean something, so a topic can be CHOSEN rather than listed.

What is actually stored, verified against the live database:

    name="xAI Unveils Grok 4.5 as Top Efficiency Leader"
    category=None                volume_text="1 day ago · News · 19K posts"

    name="Ghost Rider"
    category="Entertainment · Trending"      volume_text=None

Two different card shapes, neither parsed. Across fourteen stored rows, ZERO
carried a numeric volume — the post count was inside a string that also held
the age and the section, and the consumer took `name` alone and dropped the
rest. So "Ghost Rider" and a 19K-post launch on his exact beat arrived at topic
selection as indistinguishable strings, and 48 hours of trends were all treated
as equally current.

Parsing happens on READ, not on write: the rows already in the database are
mis-shaped, and a reader that understands both shapes fixes the history as well
as everything captured from here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# "19K posts", "1,234 posts", "2.3M posts"
_VOLUME_RE = re.compile(r"([\d.,]+)\s*([KkMm])?\s*posts?\b")
# "2 hours ago", "1 day ago", "35 minutes ago", "Trending now"
_AGE_RE = re.compile(r"\b(\d+)\s*(minute|hour|day|week)s?\s+ago\b", re.I)
_NOW_RE = re.compile(r"\btrending now\b", re.I)
# The section X files a trend under, wherever it ended up.
_SECTIONS = ("News", "Technology", "Business", "Politics", "Sports",
             "Entertainment", "Music", "Gaming", "Science", "Other")

_AGE_HOURS = {"minute": 1 / 60, "hour": 1.0, "day": 24.0, "week": 168.0}


@dataclass
class Trend:
    """One trend, with the numbers pulled out of the strings."""
    name: str
    category: str | None
    post_count: int | None
    age_hours: float | None
    rank: int | None

    @property
    def is_fresh(self) -> bool:
        """Still moving. A trend a day old is not what 'trending' means."""
        return self.age_hours is None or self.age_hours <= 12.0


def _volume(text: str) -> int | None:
    match = _VOLUME_RE.search(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    scale = (match.group(2) or "").lower()
    return int(value * {"k": 1_000, "m": 1_000_000}.get(scale, 1))


def _age_hours(text: str) -> float | None:
    if _NOW_RE.search(text or ""):
        return 0.0
    match = _AGE_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)) * _AGE_HOURS[match.group(2).lower()]


def _category(text: str) -> str | None:
    for section in _SECTIONS:
        if re.search(rf"\b{section}\b", text or "", re.I):
            return section
    return None


def parse(row: dict) -> Trend:
    """One stored row -> a trend whose numbers are usable.

    Both fields are searched for everything, because which one holds what
    depends on the card shape and neither is reliable.
    """
    blob = " ".join(str(row.get(key) or "") for key in ("category", "volume_text"))
    rank = row.get("rank")
    try:
        rank = int(rank) if rank is not None else None
    except (TypeError, ValueError):
        rank = None
    return Trend(name=str(row.get("name") or "").strip(),
                 category=_category(blob), post_count=_volume(blob),
                 age_hours=_age_hours(blob), rank=rank)


_INTEREST_CACHE: dict[str, object] = {"at": 0.0, "themes": []}


def _account_interests() -> list[str]:
    """The themes this account writes about, learned once and cached."""
    import time as _time
    now = _time.monotonic()
    if now - float(_INTEREST_CACHE["at"]) < 600 and _INTEREST_CACHE["themes"]:
        return list(_INTEREST_CACHE["themes"])          # type: ignore[arg-type]
    themes: list[str] = []
    try:
        from .storage import performance_records
        from .subject import interests
        themes = interests([str(row.get("text") or "") for row in performance_records()])
    except Exception:
        themes = []
    _INTEREST_CACHE.update(at=now, themes=themes)
    return themes


def relevance(trend: Trend, beat: frozenset[str]) -> float:
    """How much this trend belongs to HIS account, 0..1.

    Scored against the THEMES he writes about — model releases, pricing,
    coding tools, performance — and not against the companies in his archive.
    `beat` is accepted for callers that still pass a vocabulary; it is no
    longer consulted, because that vocabulary was the problem.
    """
    if not trend.name:
        return 0.0
    # HIS THEMES, NOT HIS BACK CATALOGUE. Relevance used to be overlap with the
    # entity names in his archive, where one company is 55% of all mentions —
    # so a topic could only look relevant by naming something he had already
    # covered, and the account could never be told to write about anything new.
    # Whoever shipped the thing is incidental, which is why an unknown
    # startup's coding agent now outscores a familiar name.
    from .subject import interest_fit
    hit = interest_fit(trend.name, _account_interests())
    if trend.category in ("Technology", "Science", "Business", "News"):
        hit += 0.25
    elif trend.category in ("Entertainment", "Sports", "Music", "Gaming"):
        hit -= 0.35
    return max(0.0, min(1.0, hit))


def rank_for_account(rows: list[dict], beat: frozenset[str]) -> list[Trend]:
    """Trends worth his attention, best first.

    Size and freshness only break ties — a huge trend about a film he has never
    mentioned is still not a topic for this account, which is the mistake the
    unranked list made every time.
    """
    # How lopsided the recent run already is. Blocking an exact repeat is not
    # enough on its own: the last six from-scratch topics were five Anthropic
    # posts and one other, every one of them passing the repeat check. An
    # account has to be about more than one company, so a subject that already
    # dominates gets actively pushed down rather than merely not-blocked.
    from .subject import subject_pressure, subjects
    try:
        from .radar import recent_post_topics
        pressure = subject_pressure(recent_post_topics())
    except Exception:
        pressure = {}

    scored: list[tuple[float, Trend]] = []
    for row in rows:
        trend = parse(row)
        if not trend.name:
            continue
        fit = relevance(trend, beat)
        if fit <= 0.0:
            continue
        size = min(1.0, (trend.post_count or 0) / 50_000.0)
        fresh = 1.0 if trend.age_hours is None else max(0.0, 1.0 - trend.age_hours / 24.0)
        score = fit * 0.6 + size * 0.2 + fresh * 0.2
        # A subject filling most of the recent run loses up to half its score.
        # Never zero: if a company he covers ships something genuinely big it
        # should still win, just not by default.
        crowding = max((pressure.get(subject, 0.0) for subject in subjects(trend.name)),
                       default=0.0)
        score *= 1.0 - min(0.5, crowding * 0.6)
        scored.append((score, trend))
    scored.sort(key=lambda pair: -pair[0])
    return [trend for _, trend in scored]
