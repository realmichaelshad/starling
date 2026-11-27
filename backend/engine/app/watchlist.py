"""The accounts this one actually orbits, and what to do when they move.

He asked for it in these words (2026-07-29): "an account that I mainly post
on just had this post and it's a big one — what will the AI do? It should
have a watcher on these big accounts, the ones I'm typically going to post
on, and these should be the official ones, like Anthropic."

The watchlist is DERIVED, never declared. Which accounts matter is already
written down in the archive: he names Anthropic in 24 of 40 posts, OpenAI in
10, xAI in 7, Moonshot in 6. A different user's archive yields a different
watchlist with no code change, which is the standing rule for everything in
this product that looks like a preference.

What a watcher can actually see matters more than what it would like to see.
There is no X API here, so "watch @AnthropicAI" cannot mean reading their
timeline. In practice a lab's big post lands in three places at once: their
own newsroom, the press, and Hacker News. Two of those are already sources,
and the first is added per lab where a feed exists — probed live on
2026-07-29, Google, Mistral and Qwen publish one; ANTHROPIC PUBLISHES
NONE (every documented RSS path 404s), so the lab he named is covered by the
press feeds that report on it, which measured 8 Anthropic-notable items in
the same pool.

So this module answers one question — "is this topic about an account this
user orbits?" — and the radar uses it as the first sort key, above general
notability. A watched account moving is the strongest signal this product
has, and it should never lose to a stranger's Show HN launch.
"""
from __future__ import annotations

from .subject import subjects

# Subject key -> the official account behind it, for the card. Purely
# presentational: the SET of watched subjects comes from the archive.
_OFFICIAL_HANDLES = {
    "anthropic": "@AnthropicAI",
    "openai": "@OpenAI",
    "google": "@GoogleDeepMind",
    "xai": "@xai",
    "moonshot": "@Kimi_Moonshot",
    "meta": "@AIatMeta",
    "mistral": "@MistralAI",
    "deepseek": "@deepseek_ai",
    "alibaba": "@Alibaba_Qwen",
    "cursor": "@cursor_ai",
    "perplexity": "@perplexity_ai",
    "nvidia": "@nvidia",
    "microsoft": "@microsoft",
    "apple": "@Apple",
    "midjourney": "@midjourney",
}

_MIN_MENTIONS = 2
_WATCH_LIMIT = 10
_CACHE: tuple[int, dict[str, int]] | None = None


def _archive_texts() -> list[str]:
    try:
        from .storage import connect
        with connect() as conn:
            return [r[0] for r in conn.execute(
                "select text from posts where text is not null") if r[0]]
    except Exception:                                    # noqa: BLE001
        return []


def watched_subjects() -> dict[str, int]:
    """Subject keys this account returns to, with how often, most first.

    Two mentions is the floor for the same reason `notable_subjects` uses it:
    one mention is a passing joke, not a beat. Cached against archive size so
    an import refreshes it.
    """
    global _CACHE
    texts = _archive_texts()
    if _CACHE is not None and _CACHE[0] == len(texts):
        return _CACHE[1]
    counts: dict[str, int] = {}
    for text in texts:
        for key in subjects(text):
            counts[key] = counts.get(key, 0) + 1
    watched = {key: seen for key, seen in sorted(
        counts.items(), key=lambda pair: -pair[1])[:_WATCH_LIMIT]
        if seen >= _MIN_MENTIONS}
    _CACHE = (len(texts), watched)
    return watched


def reset_watchlist_cache() -> None:
    global _CACHE
    _CACHE = None


def is_watched(topic: str) -> bool:
    """Does this topic concern an account the user orbits?"""
    return bool(subjects(topic or "") & set(watched_subjects()))


def watched_handle(topic: str) -> str:
    """The official handle to name on the card, or the subject key itself.

    Falls back to the key so an account the user watches that has no entry in
    the presentational map above still gets an honest label.
    """
    named = subjects(topic or "") & set(watched_subjects())
    if not named:
        return ""
    key = sorted(named, key=lambda k: -watched_subjects().get(k, 0))[0]
    return _OFFICIAL_HANDLES.get(key, key)
