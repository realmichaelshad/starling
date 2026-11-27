"""Read the actual article, not just its headline.

Every from-scratch post was grounded in ONE sentence — the headline — and then
told "the evidence is background, write what YOU think about it". There was
nothing to think about. So the drafts came back like this, verbatim from a live
run on 2026-07-26:

    "The model's seamless interface hides a messy reality. Developers spend
     hours debugging what users never see. It's not just about speed. It's
     about who gets to control the invisible layers. If the model is ready,
     why isn't the code?"

Five sentences, no subject, no fact, no point a reader could repeat to anyone.
That is not a writing failure — a person handed one headline and asked for an
opinion writes exactly this. The model was starved and filled the space with
shapes that sound like opinions.

This module fetches the page behind a headline and pulls out the sentences that
actually SAY something: what changed, how much, who did it, what it costs. With
three concrete facts in hand the same model writes a post with a point, because
there is finally a point available to make.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .research import fetch_page_text, search_web
from .subject import subjects as _subject_keys

# Pages that are never the article. The Midjourney fetch landed on the brand's
# X profile and came back with "1,542 posts · A community supported research
# lab", which is worse than no evidence: it is confident noise.
_NOT_AN_ARTICLE = (
    "x.com", "twitter.com", "linkedin.com", "facebook.com", "instagram.com",
    "youtube.com", "tiktok.com", "reddit.com", "pinterest.com",
)

# Sentences worth handing to a writer: something changed, or something is
# measured. Everything else is throat-clearing.
_CHANGE_VERBS = re.compile(
    r"\b(?:launch(?:ed|es|ing)?|releas(?:ed|es|ing)|ship(?:ped|s|ping)?|"
    r"announc(?:ed|es|ing)|rais(?:ed|es|ing)|acquir(?:ed|es|ing)|cost(?:s)?|"
    r"charg(?:es|ed)|price[ds]?|drop(?:ped|s)?|cut(?:s)?|add(?:ed|s)?|"
    r"support(?:s|ed)?|replac(?:ed|es)|deprecat(?:ed|es)|limit(?:s|ed)?|"
    r"beat(?:s)?|outperform(?:s|ed)?|scor(?:ed|es)|now available|rolls out)\b",
    re.IGNORECASE)
_HAS_NUMBER = re.compile(r"\d")
_BOILERPLATE = re.compile(
    r"\b(?:cookie|subscribe|newsletter|sign in|log in|advertisement|"
    r"all rights reserved|privacy policy|terms of service|share this|"
    r"read more|follow us|comments?\s*\(\d+\)|"
    # GitHub page chrome rode a repo fetch into evidence on 2026-07-27:
    # "GitHub Skip to content You signed in with another tab or window."
    r"skip to content|signed in|signed out|tab or window|reload to refresh)\b",
    re.IGNORECASE)

# Forum furniture and code. Both slipped through the first pass: a Hacker News
# page contributed "help bruce511 15 hours ago | next [-] Imagine you are on a
# beach. Good luck. reply Consider applying for YC's", and an API reference
# contributed a curl invocation. Neither is a fact about anything, and handed
# to a writer as evidence they produce a post about nothing.
_FURNITURE = re.compile(
    r"(?:\|\s*next\b|\breply\b\s*$|\bparent\b\s*\||\d+\s*points?\s+by\b|"
    r"\bhours?\s+ago\s*\||\bConsider applying for\b|\bLog in\b|\bSign up\b|"
    r"\bUpvote\b|\bcollapse\b)", re.IGNORECASE)
_CODE = re.compile(
    r"(?:curl\s+-|\s-H\s|\$[A-Z_]{3,}|```|\bnpm i\b|\bpip install\b|"
    r"\{\s*\"|\bfunction\s*\(|=>)")

# Sentences in the source author's own voice. Lifting these makes their
# experience read as his.
_FIRST_PERSON_RE = re.compile(
    r"\b(?:I|I'?m|I'?ve|I'?d|my|we|we'?re|we'?ve|our)\b(?!\s*(?:phone|team's))",
    re.IGNORECASE)

# Code and query samples: uppercase keyword runs, spaced-out punctuation, the
# shape of a snippet rather than a sentence. The two additions cover the
# variant that got through on 2026-07-27 — `NUMBER ) . column ( "signup_date"`
# — where the original classes excluded the very periods the fragment
# contains: a space-padded bracket appearing twice is code, and so is a
# bracket-dot-word chain. A nav row is the same shape in prose ("Platform |
# Docs | Discord"): two pipes with short segments between them.
_QUERY_SAMPLE_RE = re.compile(
    r"\b(?:SELECT|INSERT|PREDICT|WHERE|GROUP BY|ORDER BY|OVER|JOIN|FROM)\b"
    r"[^.!?]{0,40}[(){}]|\w\s+\.\s+\w|[(){}][^.!?]{0,20}[(){}]"
    r"|(?:\s[(){}]\s.*){2}|[)}\]]\s*\.\s*\w+\s*[({\[]"
    r"|\|[^|]{1,30}\|")


_MIN_SENTENCE = 40
_MAX_SENTENCE = 240
_MAX_FACTS = 4


def _sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text or "")
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'“])", cleaned)
    return [p.strip() for p in parts if p.strip()]


def _is_article_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return bool(host) and not any(host == bad or host.endswith("." + bad)
                                  for bad in _NOT_AN_ARTICLE)


def _score(sentence: str) -> float:
    """How much a writer could actually DO with this sentence."""
    if not (_MIN_SENTENCE <= len(sentence) <= _MAX_SENTENCE):
        return 0.0
    if (_BOILERPLATE.search(sentence) or _FURNITURE.search(sentence)
            or _CODE.search(sentence)):
        return 0.0
    # THE AUTHOR'S VOICE IS NOT A FACT. Profiling 12 live runs found the real
    # cause of the "invented experience" drafts: the evidence for a C++ vs Rust
    # article contained "I asked Claude to create 3 independent sub-agents",
    # written by the ARTICLE'S author — and the model copied it, producing "I
    # asked Claude to build a parity harness". Nothing was invented. A
    # first-person sentence lifted into evidence becomes the writer's own claim.
    if _FIRST_PERSON_RE.search(sentence):
        return 0.0
    # A QUERY EXAMPLE IS NOT A DESCRIPTION. The RelativeDB evidence was its
    # headline followed by SQL — "PREDICT SUM ( transactions . price ) OVER (
    # 90 DAYS FOLLOWING )" — which scores high on numbers and named things. The
    # draft then described a query engine in revenue-forecasting terms, which
    # was an accurate reading of the only material it had.
    if _QUERY_SAMPLE_RE.search(sentence):
        return 0.0
    # A short sentence that is mostly a URL is a link, not a fact.
    if "http" in sentence and len(sentence) < 120:
        return 0.0
    score = 0.0
    if _CHANGE_VERBS.search(sentence):
        score += 1.0
    if _HAS_NUMBER.search(sentence):
        score += 0.8
    # Named things — products, companies, versions.
    capitals = len(re.findall(r"\b[A-Z][a-zA-Z0-9.\-]{2,}", sentence))
    score += min(0.6, capitals * 0.2)
    # A quote is somebody committing to a claim on the record.
    if '"' in sentence or "“" in sentence:
        score += 0.3
    return score


def facts_for(title: str, url: str | None = None, *,
              budget_seconds: float = 8.0) -> dict[str, Any]:
    """The substantive lines behind a headline, best first.

    Returns {"facts": [...], "url": str|None, "source": "page"|"search"|"none"}.
    Never raises: no facts is a normal answer, and the caller degrades to the
    headline alone rather than failing the whole post.
    """
    # THE OFF SWITCH REACHES HERE TOO. This module had zero references to
    # `research_enabled`, and `fetch_page_text` checks only `_safe_to_fetch`
    # (an SSRF guard, not a consent one) -- so a third-party article the user's
    # browser never requested was fetched with research turned off. Nine other
    # call sites honour the switch; this was the tenth that did not.
    from .research import research_enabled
    if not research_enabled():
        return {"facts": [], "url": None, "source": "none"}

    title = str(title or "").strip()
    candidates: list[str] = []
    resolved: str | None = None

    if url and _is_article_url(url):
        try:
            body = fetch_page_text(url)
        except Exception:                       # noqa: BLE001
            body = None
        if body:
            candidates = _sentences(body)
            resolved = url

    # No usable page: ask the web what this headline is about. The search
    # snippets are themselves written to summarise, so they are good evidence.
    source = "page" if candidates else "none"
    if not candidates and title:
        try:
            results = search_web(title[:160], 5)
        except Exception:                       # noqa: BLE001
            results = []
        # A HIT ABOUT A DIFFERENT PRODUCT IS NOT EVIDENCE. The search for
        # "Snapshield – an undo button for AI coding agents" returned five
        # hits; only the first was about Snapshield. The others were Undo (a
        # debugging company) and essays on undo buttons, and their sentences
        # became "facts" about Snapshield — the draft then blended two
        # products into one. Live on 2026-07-27 (Zencoder's design-system
        # pitch inside Snapshield evidence), reproduced same day with Undo.
        # A hit contributes only if it shares a subject with the title —
        # both sides run through subject.subjects(), so "Claude" matches
        # "Anthropic". When the title names nothing checkable there is no
        # test to apply, so only the FIRST contributing hit is taken: every
        # fact then has one provenance instead of five.
        wanted = _subject_keys(title)
        taken = 0
        for hit in results:
            hit_text = f"{hit.get('title') or ''}. {hit.get('snippet') or ''}"
            if wanted:
                if not (wanted & _subject_keys(hit_text)):
                    continue
            elif taken >= 1:
                break
            snippet = str(hit.get("snippet") or "").strip()
            if snippet:
                candidates.extend(_sentences(snippet))
                taken += 1
            if not resolved and _is_article_url(str(hit.get("url") or "")):
                resolved = str(hit["url"])
        if candidates:
            source = "search"

    seen: set[str] = set()
    ranked: list[tuple[float, str]] = []
    for sentence in candidates:
        key = sentence.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        value = _score(sentence)
        if value > 0:
            ranked.append((value, sentence))
    ranked.sort(key=lambda pair: pair[0], reverse=True)

    facts = [sentence for _, sentence in ranked[:_MAX_FACTS]]
    return {"facts": facts, "url": resolved, "source": source if facts else "none"}


def evidence_block(title: str, url: str | None = None) -> str:
    """The facts as one prompt-ready block, headline first for context."""
    found = facts_for(title, url)
    lines = [title.strip()] if title.strip() else []
    lines.extend(found["facts"])
    # Deduplicate a fact that merely restates the headline.
    out: list[str] = []
    for line in lines:
        if out and line.lower()[:50] == out[0].lower()[:50]:
            continue
        out.append(line)
    return " ".join(out)[:900]
