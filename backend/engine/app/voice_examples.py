"""Which of the writer's own texts the draft is shown as its voice (V2, 2026-09-21).

WHAT THIS REPLACED, and why. The prompt used to get five texts chosen to AVOID
the subject in front of it (an on-topic past post was treated as an answer key)
and ranked by whether they contained one person's slang ("kinda", "honestly",
"bro", "lol" — the original author's register, `_HIS_REGISTER`). So every
customer's model was shown the posts least like the one it had to write, ordered
by how much they sounded like somebody else. A new customer got `[]`.

What a writer's voice looks like on THIS kind of post is best shown by the posts
they wrote that are most like it, so this now does the opposite:

  * replies are shown the writer's own REPLY PAIRS — the post they answered and
    what they wrote back — ranked by word-level BM25 similarity to the post being
    answered, with a recency bonus, plus their best-performing replies;
  * posts and quotes are shown their most similar posts plus their top performers;
  * nothing is excluded for being on topic. The copy guards stay where they were
    and still run on every draft: `copied_voice_example` (a draft that repeats one
    of these) and `copies_the_post` (a reply that hands the post back);
  * the account's own PROTECTED posts are never shown (`shareable`), because the
    prompt leaves the engine for the AI proxy.

THE SIMILARITY IS WORDS, deliberately. The engine's embedder is a random
projection of hashed buckets (`embeddings.py`), which scores unrelated text
near its own posts; BM25 over plain word tokens is exact about what it measures
and needs no model. The pool is at most a few hundred short texts, so it is
computed per call.

Examples are returned as `VoiceExample` — a `str` subclass carrying the parent
post and the kind — so every consumer that treats examples as text (the copy
guard, the novelty and voice scores, the figure corpus) keeps working, and the
prompt builder can render a reply as the pair it was.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

# Budget for the whole block. Short writers get a dozen examples; long-form
# writers get fewer, never cut mid-thought, so the model sees their real length.
TARGET_EXAMPLES = 10
MAX_EXAMPLES = 12
MIN_EXAMPLES = 3
BLOCK_CHAR_BUDGET = 6_000          # ~1,500 tokens
EXAMPLE_CHAR_CAP = 700
PARENT_CHAR_CAP = 220
TOP_PERFORMERS = {"reply": 2, "quote": 2, "post": 3}
ENHANCE_EXAMPLES = 3
SHITPOST_MAX_WORDS = 30

# BM25 constants (Robertson/Sparck Jones defaults).
_K1 = 1.2
_B = 0.75
_RECENCY_WEIGHT = 0.15
_RECENCY_HALF_LIFE_DAYS = 120.0

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+.#-]*[a-z0-9+#]|[a-z0-9]")
_STOP = frozenset("""
a about above after again against all am an and any are aren't as at be because been before
being below between both but by can can't cannot could couldn't did didn't do does doesn't doing
don't down during each few for from further had hadn't has hasn't have haven't having he he'd
he'll he's her here here's hers herself him himself his how how's i i'd i'll i'm i've if in into
is isn't it it's its itself just let's me more most mustn't my myself no nor not now of off on
once only or other ought our ours ourselves out over own same shan't she she'd she'll she's
should shouldn't so some such than that that's the their theirs them themselves then there
there's these they they'd they'll they're they've this those through to too under until up
very was wasn't we we'd we'll we're we've were weren't what what's when when's where where's
which while who who's whom why why's will with won't would wouldn't you you'd you'll you're
you've your yours yourself yourselves also like get got really even still much many one two
""".split())


class VoiceExample(str):
    """One of the writer's own texts. `parent` is the post it answered, when it was a reply."""

    parent: str = ""
    kind: str = "post"
    reason: str = "similar"

    def __new__(cls, text: str, *, parent: str = "", kind: str = "post",
                reason: str = "similar") -> "VoiceExample":
        obj = super().__new__(cls, text)
        obj.parent = parent
        obj.kind = kind
        obj.reason = reason
        return obj


def tokens(text: str) -> list[str]:
    return [token for token in _WORD_RE.findall(str(text or "").lower())
            if token not in _STOP and len(token) > 1]


def bm25_scores(query: str, documents: list[str]) -> list[float]:
    """BM25 of `query` against each document, normalised to 0..1 by the best one."""
    query_terms = set(tokens(query))
    doc_tokens = [tokens(doc) for doc in documents]
    if not query_terms or not doc_tokens:
        return [0.0] * len(documents)
    count = len(doc_tokens)
    average = sum(len(doc) for doc in doc_tokens) / max(1, count) or 1.0
    frequency = Counter(term for doc in doc_tokens for term in set(doc))
    scores = []
    for doc in doc_tokens:
        counts = Counter(doc)
        score = 0.0
        for term in query_terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1.0 + (count - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * len(doc) / average))
        scores.append(score)
    best = max(scores) if scores else 0.0
    return [score / best if best > 0 else 0.0 for score in scores]


def _age_days(created_at: Any) -> float | None:
    if not created_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0)


def _recency(created_at: Any) -> float:
    age = _age_days(created_at)
    if age is None:
        return 0.0
    return _RECENCY_WEIGHT * math.exp(-age / _RECENCY_HALF_LIFE_DAYS)


def _surface(kind: str) -> str:
    kind = str(kind or "post").lower()
    if kind in ("reply", "replies"):
        return "reply"
    if kind in ("quote", "quotes"):
        return "quote"
    return "post"


def _query_for(request: Any) -> str:
    context = getattr(request, "context", None)
    if getattr(request, "enhancement_mode", False):
        return " ".join(part for part in (str(getattr(request, "original_draft", "") or ""),
                                          str(getattr(context, "text", "") or "")) if part)
    parts = [str(getattr(context, name, "") or "")
             for name in ("text", "quote_text", "parent_text")]
    if getattr(request, "action", None) == "post":
        # A created post has no post to answer: its subject is in the direction.
        parts.append(str(getattr(request, "direction", "") or "")[:600])
    return " ".join(part for part in parts if part)


def _pool(model: Any) -> list[dict]:
    """Every usable own text, deduplicated, the account's protected posts excluded."""
    seen: set[str] = set()
    out: list[dict] = []
    for row in getattr(model, "example_rows", []) or []:
        text = str(row.get("text") or "").strip()
        if not text or text in seen:
            continue
        if row.get("shareable") is False:
            continue
        seen.add(text)
        out.append(row)
    return out


def _fits(chosen: list[VoiceExample], candidate: str, parent: str) -> bool:
    used = sum(min(len(item), EXAMPLE_CHAR_CAP) + min(len(item.parent), PARENT_CHAR_CAP)
               for item in chosen)
    cost = min(len(candidate), EXAMPLE_CHAR_CAP) + min(len(parent), PARENT_CHAR_CAP)
    return len(chosen) < MIN_EXAMPLES or used + cost <= BLOCK_CHAR_BUDGET


# API-native pass: with a fixed sample of the writer's texts in the cached prompt prefix
# (`voice_card.writer_block`), this block is only the NEAREST few for this post.
NEAREST_EXAMPLES = 6


def select(model: Any, request: Any, *, exclude: set[str] | None = None) -> list[VoiceExample]:
    """The writer's own writing for this draft: most similar first, then top performers.

    `exclude` is the text the writer block already shows (its fixed sample): those are not
    shown twice, and top performers are not added (the fixed sample carries them).
    """
    pool = _pool(model)
    if exclude:
        pool = [row for row in pool if str(row.get("text") or "").strip() not in exclude]
    if not pool:
        return []
    action = str(getattr(request, "action", "post") or "post")
    surface = "reply" if action == "reply" else ("quote" if action == "quote" else "post")
    context = getattr(request, "context", None)
    own_post_text = (str(getattr(context, "text", "") or "").strip()
                     if getattr(context, "is_own_post", False) else "")
    # The post being continued is not an example of how to continue it.
    pool = [row for row in pool if str(row.get("text") or "").strip() != own_post_text]
    if getattr(request, "post_register", None) == "shitpost":
        short = [row for row in pool if len(str(row["text"]).split()) <= SHITPOST_MAX_WORDS]
        if len(short) >= MIN_EXAMPLES:
            pool = short
    # Prefer the same surface; fall back to everything when it is thin.
    same = [row for row in pool if _surface(row.get("kind")) == surface]
    if surface == "quote":
        same = same or [row for row in pool if _surface(row.get("kind")) == "post"]
    ranked_pool = same if len(same) >= MIN_EXAMPLES else pool
    documents = [f"{row.get('parent') or ''} {row['text']}" for row in ranked_pool]
    similarity = bm25_scores(_query_for(request), documents)
    scored = sorted(
        ((similarity[index] + _recency(row.get("created_at"))
          + min(0.05, float(row.get("performance") or 0.0) / 100.0), index)
         for index, row in enumerate(ranked_pool)),
        reverse=True)
    limit = ENHANCE_EXAMPLES if getattr(request, "enhancement_mode", False) else TARGET_EXAMPLES
    if exclude is not None:
        limit = min(limit, NEAREST_EXAMPLES)
    chosen: list[VoiceExample] = []
    chosen_texts: set[str] = set()

    def take(row: dict, reason: str) -> bool:
        text = str(row["text"]).strip()
        # A reply or quote is shown with the post it answered: the pair is what
        # teaches how this writer talks BACK, which a lone reply cannot.
        parent = (str(row.get("parent") or "").strip()
                  if surface in ("reply", "quote") and _surface(row.get("kind")) == surface else "")
        if text in chosen_texts or not _fits(chosen, text, parent):
            return False
        chosen.append(VoiceExample(text, parent=parent, kind=_surface(row.get("kind")),
                                   reason=reason))
        chosen_texts.add(text)
        return True

    for _, index in scored:
        if len(chosen) >= limit:
            break
        take(ranked_pool[index], "similar")
    if not getattr(request, "enhancement_mode", False) and exclude is None:
        winners = sorted((row for row in (same or pool) if float(row.get("performance") or 0.0) > 0),
                         key=lambda row: float(row.get("performance") or 0.0), reverse=True)
        added = 0
        for row in winners:
            if added >= TOP_PERFORMERS.get(surface, 2) or len(chosen) >= MAX_EXAMPLES:
                break
            if take(row, "top_performer"):
                added += 1
    return chosen


def own_writing_count(model: Any) -> int:
    """How much of the writer's own writing the account holds (posts, replies, samples)."""
    return len(_pool(model))


def iter_texts(examples: Iterable[str]) -> list[str]:
    return [str(example) for example in examples]
