"""Typo-tolerant normalisation of user messages, for INTENT DETECTION only.

A three-stage,
zero-dependency normaliser that runs before any keyword scanner, so a
misspelling cannot change what the product decides to do.

The rule that makes this safe: the ORIGINAL text is what reaches the model. The
normalised copy exists purely so internal routing can recognise an intent. The
user's voice is never rewritten on their behalf.

Why this exists here: intent was matched with hand-written alternations, and
every real message found a new hole in them — "ap sot", "psot", "repsly",
"cna u reply", "waht hsodu i do". Each miss made the product answer "I can't
write that for you" instead of writing it, which is the worst failure it has.
Patching spellings one at a time does not converge; matching by shape does.

Stages, in order (first hit wins):
  1. `_TYPO_MAP`     - curated corrections, including short tokens fuzzy
                       matching would never risk touching ("cna", "u").
  2. transposition   - one adjacent swap away from a routing keyword ("psot").
  3. difflib         - close match against `_CANONICAL` for longer tokens.
"""
from __future__ import annotations

import re
from difflib import get_close_matches
from functools import lru_cache

# Words that actually change routing in this product. Kept deliberately small:
# a token is only ever corrected TOWARD one of these, so normalisation cannot
# invent an intent that shares no shape with what was typed.
_CANONICAL = frozenset({
    "post", "posts", "reply", "replies", "quote", "thread", "tweet", "draft",
    "write", "create", "make", "compose", "delete", "remove", "trending",
    "trend", "topic", "topics", "should", "what", "when", "image", "picture",
    "photo", "followers", "views", "likes", "schedule", "improve", "rewrite",
    "shorter", "longer", "angle", "hook", "audience", "worth", "publish",
    # THE QUESTION WORDS, which were missing — and a token is only ever
    # corrected TOWARD this set, so an intent it could not reach was an intent
    # a typo could not survive. Measured by typing the way he actually types:
    # "hwo mnay folwers do i have", "whats my raech", "am i psoting to much"
    # and "waht was my mroe recnt psot" ALL missed their deterministic answers
    # and were handed to the model, which replied "your reach is limited" and
    # "you're posting 3 times a day" — the exact fabrications those answers
    # exist to prevent. Correct input was never the problem; his input was.
    "how", "many", "much", "more", "most", "recent", "recently", "last",
    "best", "worst", "highest", "lowest", "reach", "engagement", "impressions",
    "week", "month", "today", "yesterday", "time", "times", "often", "name",
    "account", "doing", "growing", "grow", "wait", "waiting", "why", "which",
    "where", "your", "name", "stats", "numbers", "metrics", "analytics",
    "posting", "replied", "average", "median", "total", "count",
})

# Curated corrections. Short tokens are here rather than in the fuzzy stage
# because a 2-4 character token is too small to fuzzy-match safely.
_TYPO_MAP = {
    "psot": "post", "psots": "posts", "spto": "post", "sot": "post",
    # HIS OBSERVED DELETION FORMS, hand-read from every archive occurrence.
    # Not a distance rule: `delve` is edit-distance 2 from `delete` and IS in his
    # vocabulary, so a distance-2 net would eat a word he uses. These three are
    # the only forms he has typed (n=4 occurrences, all confirmed by reading).
    #
    # `dleted` -> `deleted`, NOT `delete`: the declaration path (`_DELETED_IT`,
    # strategy.py) reads the PAST TENSE. The wrong lemma would fix the question
    # case and quietly break "but i dleted it", which works today.
    "dleate": "delete", "deelte": "delete", "dleted": "deleted",
    # Dropped letters, not transpositions — the fuzzy stage will not close a
    # gap this short, so each one costs a 70-second trip to the model for an
    # answer that is already in storage.
    "wat": "what", "wht": "what", "waht": "what", "twet": "tweet",
    "twets": "tweets", "tweeet": "tweet", "psoted": "posted",
    "pots": "post", "opst": "post", "poset": "post", "psoted": "posted",
    "repsly": "reply", "repl": "reply", "rpely": "reply", "rpelys": "replies",
    "reply's": "reply", "qutoe": "quote", "quto": "quote",
    "thraed": "thread", "tehread": "thread", "tweeet": "tweet",
    "crete": "create", "creat": "create", "craete": "create",
    "wirte": "write", "wrtie": "write", "wriet": "write",
    "drfat": "draft", "darft": "draft", "mkae": "make",
    "cna": "can", "cnau": "can u", "u": "you", "ur": "your", "hte": "the",
    # Short transpositions the fuzzy stage refuses to touch (under 5 chars) and
    # the swap stage cannot reach in one adjacent move.
    "hwo": "how", "hoq": "how", "mnay": "many", "amny": "many",
    "mroe": "more", "raech": "reach", "recnt": "recent", "recnet": "recent",
    "wrost": "worst", "lsat": "last", "wek": "week", "weke": "week",
    "moth": "month", "montn": "month", "nmae": "name", "naem": "name",
    "psoting": "posting", "psotign": "posting", "folwers": "followers",
    "folowers": "followers", "followrs": "followers", "veiws": "views",
    "veiw": "view", "engagment": "engagement", "engagemnt": "engagement",
    "acount": "account", "accont": "account", "toady": "today",
    "wiat": "wait", "wiats": "wait", "waht's": "what's",
    "waht": "what", "hwat": "what", "hsodu": "should", "shodul": "should",
    "shoud": "should", "sohuld": "should", "owudl": "would", "wodul": "would",
    "trendign": "trending", "ternding": "trending", "trendig": "trending",
    "iamge": "image", "img": "image", "imgs": "images",
    "delet": "delete", "detele": "delete", "remvoe": "remove",
    "evryhtign": "everything", "sutff": "stuff", "liek": "like",
    # THE PUBLISH BOUNDARY depends on this one. The capability matcher for "can
    # you post for me" requires `automatically|automatic|for me|on my behalf|...`
    # to follow the verb, and "do u psot automaticly" normalised to "do you post
    # automaticly" — `psot` was corrected, `automaticly` was not, so the whole
    # phrase missed and the single most important answer in the product
    # ("No - I can't post, and I'm not built to") was handed to the model instead.
    #
    # It cannot go through the fuzzy stage: that only corrects TOWARD
    # `_CANONICAL`, and "automatically" is not a routing keyword in its own right.
    # Curated is also the safer half — an exact key cannot catch a real word.
    "automaticly": "automatically", "automaticaly": "automatically",
    "automatily": "automatically", "autmoatically": "automatically",
    "publsih": "publish", "publsh": "publish", "pubish": "publish",
    # THE TIMING ANSWER depends on the word "window". He typed "when is my best
    # widnwo for psotign give me the exact day and time" and got the model
    # instead of his own measured posting windows: `psotign` was corrected,
    # `widnwo` was not, and the timing matcher needs `\bbest window\b` — none of
    # its other alternatives ("when should i post", "best time to post") are in
    # that sentence, so the typo was the entire difference.
    #
    # CURATED, NOT VOCABULARY, and the measurement is the reason. Adding
    # "window" to `_CANONICAL` was the obvious fix and it fails twice over:
    # difflib scores `widnwo` at 0.667 against `window`, well under the 0.88
    # cutoff, so it would NOT have fixed his sentence — while `widow` (0.909)
    # and `windows` (0.923) both clear it, so a real word and an operating
    # system would start being rewritten. The fuzzy stage cannot reach a
    # two-swap typo without also reaching one-letter neighbours; an exact key
    # reaches his and nothing else.
    # `golas` and `bosot` reach NO handler at all — measured: "whats my golas"
    # returns nothing while "whats my goals" returns his goals, and
    # "how do i bosot this post" returns nothing while the corrected spelling
    # returns the boost instructions.
    #
    # CURATED, NOT VOCABULARY, per the rule above. Adding `goals`/`boost` to
    # `_CANONICAL` would let the transposition stage repair these for free and
    # measures clean on his 1,557 real chat tokens (0 casualties) — but it is
    # the same trade `window` lost, and an exact key cannot catch a real word.
    "golas": "goals", "bosot": "boost",
    "widnwo": "window", "widnow": "window", "windwo": "window",
    "wnidow": "window", "winodw": "window", "iwndow": "window",
}

_SKIP_SHORT_LEN = 2      # leave "a", "i", "to" alone
_FUZZY_MIN_LEN = 5       # below this, only the curated map may correct
_FUZZY_CUTOFF = 0.88     # difflib ratio; deliberately strict
_MAX_LEN = 4000          # pasted post text is not worth normalising

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_RUN_RE = re.compile(r"(.)\1{3,}")


def _try_transposition(token: str) -> str | None:
    """One adjacent-character swap away from a routing keyword."""
    for index in range(len(token) - 1):
        swapped = (token[:index] + token[index + 1] + token[index]
                   + token[index + 2:])
        if swapped in _CANONICAL:
            return swapped
    return None


# CORRECTLY-SPELLED WORDS THAT LAND INSIDE THE FUZZY CUTOFF.
#
# `_CANONICAL` is the routing vocabulary, and any token within `_FUZZY_CUTOFF`
# (0.88) of one of its members is rewritten to it. That is what makes his real
# typing reachable — measured over 1,102 of his own chat messages, the corrections
# are `psot`->post, `rpely`->reply, `folwers`->followers, `hsodu`->should, and 27
# more like them. It is a good trade and it stays.
#
# The cost is that a REAL word close to a keyword is silently rewritten too, and
# `status` -> `stats` fired five times in his own history. `status` is not a
# misspelling of anything; it is one character from `stats` and loses.
#
# Only genuinely different WORDS belong here. Morphological folding —
# `drafts`->draft, `weeks`->week, `metric`->metrics, `writes`->write — is
# deliberate and helps matching, so it is left alone; measured across 141 common
# words, that folding is most of what this does and only these few change meaning.
#
# Add to this list when another is found; it is cheaper and more predictable than
# moving the cutoff, which would take the useful corrections with it.
_PROTECTED_WORDS = frozenset({
    "status", "statuses",   # -> stats
    "states", "stated",     # -> stats
    "replay", "replays",    # -> reply
    "timer", "timers",      # -> time
    "tropic",               # -> topic
    # `deplete` scores 0.923 against `delete`, over the 0.88 cutoff — another
    # real word losing to a keyword, which is what this list is for.
    "deplete", "depletes", "depleted",   # -> delete
})


def _fix_token(token: str) -> str:
    mapped = _TYPO_MAP.get(token)
    if mapped is not None:
        return mapped
    if token in _PROTECTED_WORDS:
        return token
    if len(token) <= _SKIP_SHORT_LEN or token in _CANONICAL:
        return token
    swap = _try_transposition(token)
    if swap is not None:
        return swap
    if len(token) >= _FUZZY_MIN_LEN and token.isalpha():
        matches = get_close_matches(token, _CANONICAL, n=1, cutoff=_FUZZY_CUTOFF)
        if matches:
            return matches[0]
    return token


# Curly punctuation is what models actually emit, and every pattern in this
# codebase is written with ASCII quotes. A single U+2019 silently switched off
# the scrubber that was supposed to stop "Alright, here's a draft post for..."
# from shipping — the pattern had been tested against the straight-quote
# version of the same sentence and passed. Sixty-four regexes across the app
# share that blind spot, so the text is normalised at the boundary instead of
# each pattern being taught about Unicode.
_PUNCT_FOLD = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'", "′": "'",
    "“": '"', "”": '"', "″": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...", " ": " ",
})


# Characters that carry meaning to a model and NOTHING to the human reading the
# draft before they press Post. Zero-width joiners and spaces hide text inside
# what looks like ordinary prose; U+202E and friends reorder how a line renders
# so what is displayed is not what is stored; the tag block (U+E0000-U+E007F)
# encodes entire ASCII messages invisibly. Every one is a way to smuggle
# instructions past the one review step this product relies on — the user
# looking at the draft.
#
# Stripped rather than rejected: the legitimate uses (emoji ZWJ sequences aside)
# do not appear in X post text, and dropping a post because it contains an
# invisible character would be a strange thing to explain.
_INVISIBLE = re.compile(
    "[​-‏"      # zero-width space/non-joiner/joiner, LRM/RLM
    "‪-‮"       # bidi embedding and override
    "⁠-⁤"       # word joiner, invisible operators
    "⁦-⁩"       # bidi isolates
    "﻿"              # BOM used mid-string
    "\U000e0000-\U000e007f"   # tag characters: a full invisible ASCII channel
    "]")


def strip_invisible(text: str) -> str:
    """Remove characters a reviewer cannot see. Safe on any string."""
    if not text:
        return text
    return _INVISIBLE.sub("", text)


def fold_punctuation(text: str) -> str:
    """Curly quotes and dashes to their ASCII equivalents, for MATCHING only.

    Never use the result as output: it would rewrite the user's own
    typography. It exists so a pattern written with ' still matches text
    written with the character a model actually produced.
    """
    return text.translate(_PUNCT_FOLD) if text else text


@lru_cache(maxsize=256)
def normalize_typos(message: str) -> str:
    """A cleaned copy for intent matching. NEVER shown to the user or the model."""
    if not message or len(message) > _MAX_LEN:
        return message or ""
    lowered = _RUN_RE.sub(r"\1\1", fold_punctuation(message).lower())
    return " ".join(_fix_token(token) for token in _TOKEN_RE.findall(lowered))


def corrections(message: str) -> dict[str, str]:
    """What changed, for logging when a routing decision hinged on a fix."""
    if not message or len(message) > _MAX_LEN:
        return {}
    changed: dict[str, str] = {}
    for token in _TOKEN_RE.findall(message.lower()):
        fixed = _fix_token(token)
        if fixed != token:
            changed[token] = fixed
    return changed

# Abbreviations that end in a full stop but do not end a sentence. Without
# these, "If the U.S. lets this happen" is split after "U.S." and the paragraph
# formatter starts a new block mid-clause — which shipped live as:
#
#     If the U.S.
#
#     Lets this happen, it's not just about tech.
#
# Every sentence-splitting caller shares this, so the fix lands everywhere at
# once rather than being re-derived and re-forgotten in each of them.
_ABBREVIATIONS = (
    "u.s", "u.k", "u.s.a", "e.g", "i.e", "etc", "vs", "approx", "inc", "ltd",
    "co", "corp", "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "no",
    "fig", "vol", "est", "dept", "gov", "sen", "rep", "a.m", "p.m",
)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    """Sentences, without breaking on abbreviations or initials."""
    parts = _SENTENCE_END_RE.split(text or "")
    out: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if out:
            tail = out[-1].rstrip()
            stem = tail[:-1].lower() if tail.endswith(".") else ""
            stem = stem.rsplit(" ", 1)[-1] if stem else ""
            # "U.S." / "e.g." / a lone initial ("J.") never end a sentence, and
            # neither does a decimal cut mid-number.
            if stem in _ABBREVIATIONS or (len(stem) == 1 and stem.isalpha()):
                out[-1] = tail + " " + part.strip()
                continue
        out.append(part.strip())
    return out
