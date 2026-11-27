"""Releases and shipping news — the events this account exists to reply to.

The Opus 5 failure: X's trend blurb read "Anthropic's Claude Opus 5 Misses
July 23 Launch Amid Rumors and Tests" while the web already said "Anthropic
releases Claude Opus 5". The radar treated X's editorial blurb as the truth,
so the biggest launch of the year produced a post idea about *anticipation*
and no reply lane at all. Two lessons, both encoded here:

1. An X trend blurb is a LAGGING SUMMARY, not a fact. For a launch topic the
   web is the authority, and when they disagree about whether a thing shipped,
   the web wins and the stale framing must never reach the writer.
2. A release is not a topic to muse about — it is a CONVERSATION happening
   right now under the vendor's own announcement. For an account built on
   replying to releases and bug updates, "what should I do" must name that
   conversation, not say "find a fresh post with traction".

Nothing here invents a status: if the evidence does not clearly say a thing
shipped, the status is "unclear" and callers degrade honestly.
"""
from __future__ import annotations

import re
from typing import Any

# Launch-shaped topics. Deliberately broad on the INTAKE side (cheap to test,
# and a missed launch is the expensive error), then narrowed by evidence.
_RELEASE_TOPIC_RE = re.compile(
    r"\b(launch(?:es|ed|ing)?|releas(?:e|es|ed|ing)|ship(?:s|ped|ping)?|"
    r"roll(?:s|ing|ed)?[ -]out|drop(?:s|ped|ping)?|out now|available|"
    r"announc(?:e|es|ed|ement)|unveil(?:s|ed)?|debut(?:s|ed)?|"
    r"update|patch|version|beta|preview|general availability|\bGA\b|"
    r"delay(?:s|ed)?|miss(?:es|ed)\s+(?:its\s+)?launch|postpone)",
    re.IGNORECASE)

# Evidence that the thing IS OUT. Past/present tense of shipping verbs, plus
# the phrases vendors and press actually use on launch day.
_SHIPPED_RE = re.compile(
    r"\b(releases?|released|launch(?:es|ed)|ships?|shipped|"
    r"is (?:here|out|live|now available)|now available|available now|"
    r"rolling out|rolls out|introduc(?:es|ing)|announc(?:es|ed)|"
    r"out today|goes live|went live|general availability)\b",
    re.IGNORECASE)

# Evidence that it has NOT shipped. Checked FIRST on each result, because
# "misses its launch" contains "launch" and would otherwise read as shipped.
_PENDING_RE = re.compile(
    r"\b(miss(?:es|ed)\s+(?:its\s+)?launch|delay(?:s|ed)|postponed|"
    r"not yet|no release date|rumou?r(?:s|ed)?|expected|reportedly|"
    r"leak(?:s|ed)?|coming soon|ahead of (?:its|the) launch|"
    r"could (?:launch|arrive)|may (?:launch|arrive)|when will)\b",
    re.IGNORECASE)

# Where a launch conversation actually lives. Curated because guessing a
# handle is a fabrication risk: a wrong @mention in a draft is worse than
# none, so an unknown vendor simply yields no handle.
_OFFICIAL_HANDLES: dict[str, tuple[str, ...]] = {
    "anthropic": ("@AnthropicAI", "@claudeai"),
    "claude": ("@AnthropicAI", "@claudeai"),
    "opus": ("@AnthropicAI",),
    "sonnet": ("@AnthropicAI",),
    "haiku": ("@AnthropicAI",),
    "openai": ("@OpenAI",),
    "chatgpt": ("@OpenAI",),
    "gpt": ("@OpenAI",),
    "codex": ("@OpenAI",),
    "google": ("@GoogleDeepMind",),
    "gemini": ("@GoogleDeepMind",),
    "deepmind": ("@GoogleDeepMind",),
    "mistral": ("@MistralAI",),
    "meta": ("@AIatMeta",),
    "llama": ("@AIatMeta",),
    "xai": ("@xai",),
    "grok": ("@xai",),
    "kimi": ("@Kimi_Moonshot",),
    "moonshot": ("@Kimi_Moonshot",),
    "deepseek": ("@deepseek_ai",),
    "qwen": ("@Alibaba_Qwen",),
    "cursor": ("@cursor_ai",),
    "github": ("@github",),
    "microsoft": ("@Microsoft",),
    "nvidia": ("@nvidia",),
    "perplexity": ("@perplexity_ai",),
    "hugging": ("@huggingface",),
}

# AUTHORED release-status subjects. Release queries are built from this list and
# nothing else; the account's own posts may reorder it locally but never add a word.
AUTHORED_RELEASE_SUBJECTS: tuple[str, ...] = (
    "anthropic latest model release",
    "openai latest model release",
    "google latest model release",
    "mistral latest model release",
    "meta latest model release",
    "deepseek latest model release",
)


# Release news changes on the scale of hours, and the same launch gets asked
# about repeatedly in one session. Caching keeps a VERIFIED answer alive across
# a search outage — the difference between "Opus 5 shipped" and a shrug when
# the search backend rate-limits mid-session. A failed check is remembered only
# briefly so recovery is quick.
_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATUS_TTL_S = 1800.0          # 30 min for a real answer
_UNAVAILABLE_TTL_S = 120.0      # 2 min for "search could not answer"


def _cached_status(query: str) -> dict[str, Any] | None:
    import time
    entry = _STATUS_CACHE.get(query)
    if not entry:
        return None
    stamped, value = entry
    ttl = _UNAVAILABLE_TTL_S if value.get("status") == "unavailable" else _STATUS_TTL_S
    if time.monotonic() - stamped > ttl:
        _STATUS_CACHE.pop(query, None)
        return None
    return dict(value)


def _remember_status(query: str, value: dict[str, Any]) -> None:
    import time
    _STATUS_CACHE[query] = (time.monotonic(), dict(value))
    if len(_STATUS_CACHE) > 64:                 # bounded; oldest first
        for key in sorted(_STATUS_CACHE, key=lambda k: _STATUS_CACHE[k][0])[:16]:
            _STATUS_CACHE.pop(key, None)


def is_release_topic(text: str) -> bool:
    """Does this topic look like shipping news? Cheap pre-filter."""
    return bool(text and _RELEASE_TOPIC_RE.search(str(text)))


def official_handles(subject: str) -> list[str]:
    """Accounts whose own post IS the launch conversation. Never guessed."""
    lowered = str(subject or "").lower()
    found: list[str] = []
    for key, handles in _OFFICIAL_HANDLES.items():
        if re.search(rf"\b{re.escape(key)}", lowered):
            for handle in handles:
                if handle not in found:
                    found.append(handle)
    return found[:3]


def conversation_url(subject: str, handles: list[str] | None = None) -> str:
    """A LIVE X search for the launch conversation, newest first.

    A search URL, never a fabricated status link: the specific post id is
    unknowable from here, and a dead link would be worse than a search that
    always resolves. `f=live` puts the newest posts first, which is exactly
    the early window a reply needs.
    """
    # The CLEANED subject: a search for "Opus 5 Misses July Launch Amid Rumors"
    # finds the rumor thread, not the launch thread — sending him to argue with
    # a stale story instead of joining the live one.
    subject = search_subject(subject) or " ".join(str(subject or "").split()[:6])
    query = subject
    if handles:
        query = f"{subject} from:{handles[0].lstrip('@')} OR {subject}"
    from urllib.parse import quote
    return f"https://x.com/search?q={quote(query)}&f=live"


def _classify(results: list[dict]) -> tuple[str, str, list[dict]]:
    """(status, headline, supporting) from search results.

    Pending patterns are tested first on each result because launch-miss
    headlines contain launch words. A single clear "releases X" outweighs
    rumor chatter — that is precisely the Opus 5 case, where the rumor
    headline was X's and the shipping headline was the press's.
    """
    shipped, pending = [], []
    for item in results:
        if not isinstance(item, dict):
            continue
        blob = " ".join(str(item.get(key) or "") for key in ("title", "snippet"))
        if not blob.strip():
            continue
        if _PENDING_RE.search(blob):
            pending.append(item)
        elif _SHIPPED_RE.search(blob):
            shipped.append(item)
    if shipped:
        headline = str(shipped[0].get("title") or "").strip()
        return "shipped", headline, shipped[:3]
    if pending:
        headline = str(pending[0].get("title") or "").strip()
        return "pending", headline, pending[:3]
    return "unclear", "", []


# Status words and date fragments belong to the BLURB, not the subject. Asking
# the web "Claude Opus 5 Misses July 23 Launch Amid Rumors and Tests" retrieves
# the rumor coverage that phrasing came from and confirms the stale story —
# the exact circular trap this module exists to break. Ask about the THING.
# A calendar date IN the blurb ("July 23"), removed while the month is still
# next to it. Order matters: strip status words first and "Opus 5 ... July 23"
# collapses to "Opus 5 23", where no rule can tell the version from the day —
# the first attempt duly kept "23" and threw away the "5".
_DATE_PAIR_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{0,2}(?:st|nd|rd|th)?\b",
    re.IGNORECASE)
_QUERY_NOISE_RE = re.compile(
    r"\b(miss(?:es|ed)|launch(?:es|ed|ing)?|releas(?:e|es|ed|ing)|amid|rumou?rs?|"
    r"tests?|delay(?:s|ed)?|postponed|reportedly|expected|leak(?:s|ed)?|"
    r"ahead|coming soon|update|news|today|yesterday|tomorrow)\b",
    re.IGNORECASE)
_QUERY_STOPWORDS = frozenset(
    "and the a an of for its it to in on at with as but or new".split())


def search_subject(subject: str) -> str:
    """The entity to ask the web about, with the blurb's framing stripped.

    Version numbers are the ONE kind of number that must survive: "Opus 5" is
    the subject, "July 23" is the blurb's editorial detail.
    """
    cleaned = _DATE_PAIR_RE.sub(" ", str(subject or ""))
    cleaned = _QUERY_NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"['’]s\b", "", cleaned)
    words = [w for w in re.split(r"[^\w.\-]+", cleaned) if w
             and w.lower() not in _QUERY_STOPWORDS]
    return " ".join(words[:6]).strip()


def release_status(subject: str, *, max_results: int = 5) -> dict[str, Any]:
    """Did it actually ship? Answered from the WEB, not from an X blurb.

    Returns {status: shipped|pending|unclear, headline, sources, handles,
    conversation_url, subject}. Never raises; "unclear" on any failure, and
    callers must treat "unclear" as "say nothing new".
    """
    subject = str(subject or "").strip()
    out: dict[str, Any] = {"subject": subject, "status": "unclear", "headline": "",
                           "sources": [], "handles": [], "conversation_url": ""}
    if not subject:
        return out
    query = search_subject(subject) or subject
    out["query"] = query
    cached = _cached_status(query)
    if cached is not None:
        return cached
    try:
        from . import research
        if not research.research_enabled():
            out["status"] = "unavailable"
            return out
        results = research.search_web(query, max_results=max_results) or []
    except Exception:
        out["status"] = "unavailable"
        return out
    if not results:
        # NO ANSWER IS NOT "NOTHING IS HAPPENING". Search rate-limits and
        # outages are silent, and reporting them as "nothing on the radar" is
        # how launch day produced "I can't find anything to post about".
        out["status"] = "unavailable"
        _remember_status(query, out)
        return out
    status, headline, supporting = _classify(list(results))
    handles = official_handles(subject)
    out.update({
        "status": status,
        "headline": headline,
        "sources": [{"title": str(item.get("title") or "").strip(),
                     "url": str(item.get("url") or "").strip()}
                    for item in supporting if item.get("title") or item.get("url")],
        "handles": handles,
        "conversation_url": conversation_url(subject, handles),
    })
    _remember_status(query, out)
    return out


def account_vendors(limit: int = 3) -> list[str]:
    """The vendors THIS account actually writes about, from its own archive.

    Intersecting the archive's entity lexicon with the known-handle map keeps
    this grounded: no vendor is invented, and an account that never mentions
    Nvidia never gets Nvidia releases. Longer keys first so "anthropic" beats
    the substring-ish "gpt".
    """
    try:
        from .radar import _entity_lexicon
        lexicon = _entity_lexicon()
    except Exception:
        return []
    found = [key for key in _OFFICIAL_HANDLES if key in lexicon]
    found.sort(key=len, reverse=True)
    # One entry per handle group: "claude"/"opus"/"anthropic" are one vendor.
    seen: set[tuple[str, ...]] = set()
    unique: list[str] = []
    for key in found:
        handles = _OFFICIAL_HANDLES[key]
        if handles in seen:
            continue
        seen.add(handles)
        unique.append(key)
    return unique[:limit]


def beat_handles(limit: int = 4) -> list[str]:
    """The accounts that break news on HIS beat, without the @.

    The measured watchlist is built from accounts he has replied to, and on
    this machine that is exactly one entry — @grok, from a single reply. So
    "check what is new" read one account, on a profile whose whole subject is
    model launches. These handles come from his own archive (which vendors he
    writes about) through the curated map above (which account is theirs), so
    nothing is guessed: an unknown vendor simply yields no handle, and an
    account that never mentions a lab never watches it.
    """
    out: list[str] = []

    def add(handle: str) -> None:
        bare = handle.lstrip("@")
        if bare and bare.lower() not in {item.lower() for item in out}:
            out.append(bare)

    # His own vendors first — these are the ones he has shown he cares about.
    own = account_vendors(limit=limit)
    for vendor in own:
        for handle in _OFFICIAL_HANDLES.get(vendor, ()):
            add(handle)
    if not own:
        # NOT IN THIS FIELD AT ALL. An account that writes about knitting must
        # not be handed AI labs to watch — that would be the same mistake in
        # the opposite direction, imposing an assumed subject instead of
        # inheriting one. No evidence, no watchlist.
        return []
    # THEN THE REST OF THE FIELD, even where he has never mentioned them.
    # Watching only the labs already in his archive is the circularity that
    # made every topic the same company: a launch from a lab he has not written
    # about could never reach him, so he could never start writing about it.
    # Only reached once his archive has proven he IS in this field.
    for vendor in ("anthropic", "openai", "google", "mistral", "meta"):
        if len(out) >= limit:
            break
        for handle in _OFFICIAL_HANDLES.get(vendor, ())[:1]:
            add(handle)
    return out[:limit]


def recent_releases(subjects: list[str] | None = None, limit: int = 2) -> list[dict[str, Any]]:
    """Verified-shipped releases in this account's world, asked of the WEB.

    Exists because the X trend feed goes stale the moment he stops browsing —
    15 hours stale when he opened the dock on Opus 5 day — and "I can't find
    anything" during the biggest launch of the year is the worst possible
    answer for an account built on release commentary.
    """
    out: list[dict[str, Any]] = []
    if subjects is None:
        # ORDER MATTERS. Looking first for releases from the companies the
        # account already writes about made every answer the same company, so
        # the subject covered MOST RECENTLY goes last and an equally real release
        # from anyone else is found first. The reordering is local; the subjects
        # themselves are the authored list.
        from .radar import recent_post_topics
        from .subject import subject_pressure
        pressure = subject_pressure(recent_post_topics())
        subjects = sorted(AUTHORED_RELEASE_SUBJECTS,
                          key=lambda subject: pressure.get(subject.split()[0], 0.0))
    for subject in subjects[:4]:
        if len(out) >= limit:
            break
        try:
            status = release_status(subject)
        except Exception:
            continue
        if status.get("status") == "shipped" and status.get("headline"):
            out.append(status)
    return out


def corrected_framing(trend_name: str, status: dict[str, Any]) -> str | None:
    """One plain line replacing a stale trend blurb — or None to keep quiet.

    Only speaks when the web CONTRADICTS the blurb, which is the whole point:
    the blurb said "misses its launch", the web says "releases". Silence when
    they agree keeps this from becoming another source of noise.
    """
    if not trend_name or not isinstance(status, dict):
        return None
    blurb_says_pending = bool(_PENDING_RE.search(trend_name))
    if status.get("status") == "shipped" and blurb_says_pending:
        headline = str(status.get("headline") or "").strip()
        subject = str(status.get("subject") or "this").strip()
        return (f"IT IS OUT. X's trend summary still says it has not shipped, but the web "
                f"confirms it has: {headline or subject}. Write about the actual release, "
                f"never about waiting for it.")
    return None
