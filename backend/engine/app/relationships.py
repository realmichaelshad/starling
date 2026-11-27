"""Author relationships (spec §2.6) and the watchlist score (§4.2).

The spec is explicit that this must not collapse into "always reply to the
biggest account" or "always reply to the newest post". A relationship is
evidence about whether the user's contribution has ever landed with a particular
audience, so every account carries interaction history, topic overlap, measured
outcomes, recency, distinctness and a sample size.

    relationship opportunity =
        0.30 audience/topic overlap
      + 0.25 historical outcome quality
      + 0.15 recency
      + 0.15 conversation accessibility
      + 0.15 unique-angle availability
      - repetition and safety penalties

Those weights are starting priors, not learned truths (§4.2), and the score
always ships with the sample size behind it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import evidence as ev
from .storage import (
    connect,
    get_author_relationship,
    list_author_relationships,
    replies_to_author,
    upsert_author_relationship,
)

WEIGHTS = {
    "overlap": 0.30,
    "outcome": 0.25,
    "recency": 0.15,
    "accessibility": 0.15,
    "angle": 0.15,
}

# A relationship stops counting as current after this long. Not a hard cut:
# recency is a graded term, and an old relationship still carries topic overlap.
RECENCY_HALF_LIFE_DAYS = 45.0

# Replying to the same account more often than this inside the window starts to
# look mechanical, which X's own author-diversity behaviour penalises (§4.4b).
REPEAT_WINDOW_DAYS = 7
REPEAT_SOFT_LIMIT = 2

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")
# X puts the replied-to handles at the START of a reply's text. The archive
# export leaves `parent_author` empty (it is only populated from the optional
# reply-context.csv), so on a real import every relationship would be invisible
# — 20 replies, 0 recoverable targets. The leading mention run recovers it.
_LEADING_MENTIONS_RE = re.compile(r"^((?:\s*@[A-Za-z0-9_]{1,15})+)")
_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")
_STOP = {
    "the", "and", "for", "that", "this", "with", "you", "your", "are", "was",
    "have", "has", "but", "not", "they", "them", "その", "from", "what", "when",
    "who", "why", "how", "all", "can", "will", "just", "about", "into", "than",
    "then", "there", "their", "been", "more", "most", "some", "such", "only",
}


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall((text or "").lower())
            if word not in _STOP}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _recency_score(last_at: Any, *, now: datetime | None = None) -> float | None:
    """1.0 today, decaying by half every RECENCY_HALF_LIFE_DAYS. None if unknown."""
    seen = ev.parse_time(last_at)
    if seen is None:
        return None
    days = max(0.0, ((now or _now()) - seen).total_seconds() / 86400.0)
    return float(0.5 ** (days / RECENCY_HALF_LIFE_DAYS))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def reply_target(row: dict) -> tuple[str | None, str]:
    """Who a reply was aimed at, and how confidently we know it.

    Returns `(handle, basis)` where basis is:
      "recorded"  — the archive gave us `parent_author` outright;
      "mention"   — recovered from the leading @mention run, which is how X
                    composes replies. Reliable in the common case and the only
                    signal a plain archive export carries;
      "unknown"   — no evidence. Never guessed from mid-text mentions, which
                    are people the user talked ABOUT, not replied TO.
    """
    recorded = str(row.get("parent_author") or "").strip().lstrip("@").lower()
    if recorded:
        return recorded, "recorded"
    lead = _LEADING_MENTIONS_RE.match(str(row.get("text") or ""))
    if not lead:
        return None, "unknown"
    handles = _MENTION_RE.findall(lead.group(1))
    # The first handle in the run is the account being replied to; the rest are
    # other thread participants X carries along.
    return (handles[0].lower(), "mention") if handles else (None, "unknown")


def rebuild(pillar_tokens: set[str] | None = None) -> int:
    """Aggregate the archive into one row per account the user has engaged.

    `parent_author` has been a column since the first migration but nothing ever
    grouped by it, so §2.6's relationship map did not exist. Reply and quote
    rows are the evidence; original posts are not an interaction with anyone.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT post_id, parent_author, post_type, text, parent_text, created_at, "
            "       first_observed_at, metrics_json "
            "  FROM posts "
            " WHERE post_type IN ('reply','quote')"
        ).fetchall()

    grouped: dict[str, list[dict]] = {}
    bases: dict[str, set[str]] = {}
    resolved: list[tuple[str, str, str]] = []
    for row in rows:
        record = dict(row)
        handle, basis = reply_target(record)
        if not handle:
            continue
        record["_target_basis"] = basis
        grouped.setdefault(handle, []).append(record)
        bases.setdefault(handle, set()).add(basis)
        if record.get("post_id"):
            resolved.append((handle, basis, str(record["post_id"])))

    # Persist the resolved target so every other query — repeat pressure,
    # conversation uniqueness, duplicate-target penalties — can use plain SQL
    # instead of re-deriving it, and so `parent_author` keeps its own meaning.
    if resolved:
        with connect() as conn:
            conn.executemany(
                "UPDATE posts SET reply_target_handle=?, reply_target_basis=? WHERE post_id=?",
                resolved)

    for handle, items in grouped.items():
        stamps = [str(item.get("created_at") or item.get("first_observed_at") or "")
                  for item in items]
        stamps = [stamp for stamp in stamps if stamp]
        own_text = " ".join(str(item.get("text") or "") for item in items)
        their_text = " ".join(str(item.get("parent_text") or "") for item in items)
        own_tokens, their_tokens = _tokens(own_text), _tokens(their_text)

        # Topic overlap is measured against the user's pillars when known,
        # otherwise against what the user actually wrote to this account. The
        # second is weaker evidence, so it is recorded but not inflated.
        if pillar_tokens:
            overlap = _jaccard(their_tokens, pillar_tokens)
        else:
            overlap = _jaccard(their_tokens, own_tokens)

        # Distinctness: how varied the user's replies to this account are. Many
        # near-identical replies is the pattern X's policy calls spam (§1.5).
        texts = [str(item.get("text") or "") for item in items if item.get("text")]
        distinctness = _distinctness(texts)

        # A target recovered from the leading mention is good evidence but not
        # the archive's own word, so it never claims better than directional.
        basis = bases.get(handle, {"unknown"})
        confidence = ev.confidence_for_samples(len(items))
        if "recorded" not in basis and confidence == ev.STRONG:
            confidence = ev.DIRECTIONAL

        upsert_author_relationship({
            "handle": handle,
            "interactions": len(items),
            "replies": sum(1 for item in items if item.get("post_type") == "reply"),
            "quotes": sum(1 for item in items if item.get("post_type") == "quote"),
            "topic_overlap": round(overlap, 4),
            "distinctness": round(distinctness, 4) if distinctness is not None else None,
            "last_interaction_at": max(stamps) if stamps else None,
            "first_interaction_at": min(stamps) if stamps else None,
            "sample_size": len(items),
            "confidence": confidence,
            "topics": sorted(list(their_tokens))[:12],
            "notes": ("target recovered from leading @mention"
                      if "recorded" not in basis else None),
        })
    return len(grouped)


def _distinctness(texts: list[str]) -> float | None:
    """1.0 = every reply is different; low = the user repeats themselves."""
    if len(texts) < 2:
        return None
    token_sets = [_tokens(text) for text in texts if _tokens(text)]
    if len(token_sets) < 2:
        return None
    pairs = 0
    similarity = 0.0
    for index, first in enumerate(token_sets):
        for second in token_sets[index + 1:]:
            similarity += _jaccard(first, second)
            pairs += 1
    return max(0.0, 1.0 - (similarity / pairs)) if pairs else None


def repeat_pressure(handle: str, *, now: datetime | None = None) -> dict[str, Any]:
    """How mechanical another reply to this account would look right now.

    Not a prohibition — the spec explicitly protects genuine repeated
    relationships (§4.4b) — but a penalty that rises with recent volume.
    """
    since = ((now or _now()) - timedelta(days=REPEAT_WINDOW_DAYS)).isoformat()
    recent = replies_to_author(handle, since=since)
    count = len(recent)
    over = max(0, count - REPEAT_SOFT_LIMIT)
    return {
        "handle": handle,
        "recent_replies": count,
        "window_days": REPEAT_WINDOW_DAYS,
        # Saturates at 1.0 after a few extra replies rather than growing forever.
        "penalty": round(min(1.0, over / 3.0), 3),
        "note": (f"{count} repl{'y' if count == 1 else 'ies'} to @{handle} in the "
                 f"last {REPEAT_WINDOW_DAYS} days."),
    }


def opportunity(handle: str, *, accessibility: float = 1.0,
                angle_available: float | None = None,
                safety_fit: float = 1.0,
                now: datetime | None = None) -> dict[str, Any]:
    """§4.2's relationship opportunity score, with its evidence attached.

    `accessibility` and `angle_available` come from Radar/Analyzer at call time
    (can the user participate; is there anything left to say). A missing angle
    read is treated as unknown and drops confidence rather than defaulting to
    "there is an angle", which would manufacture an opportunity.
    """
    record = get_author_relationship(handle)
    if not record:
        return {
            "handle": handle, "score": None, "confidence": ev.UNKNOWN_CONFIDENCE,
            "sample_size": 0,
            "reason": f"No recorded relationship with @{handle}.",
            "terms": {}, "penalty": 0.0,
        }

    overlap = float(record.get("topic_overlap") or 0.0)
    outcome = record.get("outcome_percentile")
    recency = _recency_score(record.get("last_interaction_at"), now=now)
    distinct = record.get("distinctness")
    angle = angle_available if angle_available is not None else distinct

    terms = {
        "overlap": overlap,
        # No measured outcome yet: contribute nothing rather than assume median
        # performance, which would invent evidence the account does not have.
        "outcome": float(outcome) if outcome is not None else 0.0,
        "recency": recency if recency is not None else 0.0,
        "accessibility": max(0.0, min(1.0, accessibility)),
        "angle": float(angle) if angle is not None else 0.0,
    }
    base = sum(WEIGHTS[name] * value for name, value in terms.items())

    pressure = repeat_pressure(handle, now=now)
    penalty = pressure["penalty"] + max(0.0, 1.0 - max(0.0, min(1.0, safety_fit)))
    score = max(0.0, min(1.0, base - penalty))

    sample = int(record.get("sample_size") or 0)
    missing = [name for name, value in
               (("outcome", outcome), ("recency", recency), ("angle", angle))
               if value is None]
    confidence = ev.confidence_for_samples(sample)
    if missing:
        # Every unmeasured term is a hole in the score; say so instead of
        # presenting a confident number built partly from zeros.
        confidence = ev.LIMITED if confidence == ev.STRONG else confidence

    return {
        "handle": handle,
        "score": round(score, 4),
        "terms": {name: round(value, 4) for name, value in terms.items()},
        "penalty": round(penalty, 4),
        "sample_size": sample,
        "confidence": confidence,
        "missing_terms": missing,
        "repeat": pressure,
        "reason": _explain(handle, terms, pressure, missing),
    }


def _explain(handle: str, terms: dict[str, float], pressure: dict[str, Any],
             missing: list[str]) -> str:
    ranked = sorted(terms.items(), key=lambda item: WEIGHTS[item[0]] * item[1], reverse=True)
    lead = ranked[0][0] if ranked else "overlap"
    readable = {
        "overlap": "the topics overlap with your pillars",
        "outcome": "your past replies here did well",
        "recency": "you engaged recently",
        "accessibility": "the conversation is open to you",
        "angle": "there is still a distinct angle",
    }[lead]
    parts = [f"@{handle}: {readable}"]
    if pressure["penalty"] > 0:
        parts.append(pressure["note"])
    if missing:
        parts.append("not yet measured: " + ", ".join(missing))
    return " · ".join(parts)


def watchlist(limit: int = 10, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Accounts worth checking, best first. Follower count is not an input."""
    ranked = []
    for record in list_author_relationships(limit=200):
        scored = opportunity(record["handle"], now=now)
        if scored.get("score") is not None:
            scored["display_name"] = record.get("display_name")
            scored["interactions"] = record.get("interactions")
            ranked.append(scored)
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]
