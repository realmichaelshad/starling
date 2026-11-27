"""lane.py - the single source of topical truth: which of his two lanes a topic is in.

He named the two lanes himself, and named them AS THE PROFILE:

    "the user profile leaks into the topics... fix it fully"
    "The user profile is AI topics, vibe coding - that's the user profile.
     Not Anthropic, xAI, OpenAI. That's stupidness."

So a lane is a THEME test and must never be a SUBJECT test. Everything here ranks by lane
membership; nothing ranks by entity name. `tests/test_subject_not_profile.py` is not an
obstacle to this module - it is its charter.

    "vibe coding" = THE PRACTICE. Building with AI: shipping real things with Claude Code /
                    Cursor / agents, what breaks, what works, what you built, the workflow.
                    Tool releases matter only when they change how you build.
    "frontier models" = releases, rumours, capabilities, benchmarks, the labs.

These are the ONLY two. A topic that is neither is off-lane and should not be offered.

## Why this is not a word list

NOUNS CANNOT DECIDE THE LANE; THE EVENT CAN. `Codex` appears in all three classes in his own
archive - a release story (frontier), a workflow remark (practice), and a joke (neither).
`Claude Code`, `Cursor`, `agent`, `terminal` sit in `subject._VIBE_CORE` today with no way to
tell which lane they mean. So:

    practice = an ACT of making, or a build SURFACE plus a workflow/breakage FRAME
    frontier = a lab/model NAME plus a release or capability EVENT

## The rule that cost a rewrite, and has a test

**NO TOKEN MAY APPEAR IN BOTH HALVES OF A CONJUNCTION.** In an earlier revision
`orchestrat\\w*` and `vibe cod\\w*` sat in both `BUILD_SURFACE` and `BUILD_FRAME`, so a single
token satisfied both sides and the conjunction was decoration. It fired PRACTICE on
"Gemini Robotics ER 2: video understanding, task orchestration, and multi-robot collaboration"
and on a VentureBeat analyst-hire headline. `test_lane.py` asserts the two halves are disjoint.

## What this module deliberately does NOT do

**No fiction guard.** The obvious kill for the Marvel/Spider-Man noise would delete five of his
eleven practice posts, because his practice lane is literally about a Spider-Man artifact.
Measured, not assumed - see `_GENRE_KILL` reuse below, which handles the real off-lane genres
(funding, policy, incident triage) without touching his subjects.

**No outbound query built from his words.** `LANE_QUERIES` is authored in-repo and enumerable.
His declared lane SELECTS a bucket; his vocabulary never leaves the machine. Per R-QUERY-1 the
ENFORCEMENT of that lives in `news.py`, deliberately, so a bug here cannot leak past it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from itertools import chain
from typing import Any, Mapping, Sequence

from .subject import _GENRE_KILL, _PARAMETER_COUNT_RE          # noqa: E402

LANES: tuple[str, str] = ("vibe coding", "frontier models")
PRACTICE, FRONTIER = LANES

# How far into the text a lane's evidence counts as "the subject of the sentence".
_HEAD_CHARS = 160


# --------------------------------------------------------------------------------------
# PRACTICE - an ACT of making, or a SURFACE plus a FRAME.
# --------------------------------------------------------------------------------------

# Someone MADE something. This alone is sufficient: the verb is the whole signal.
BUILD_ACT = re.compile(
    r"\b(?:"
    r"vibe cod\w*"
    r"|(?:i|we|you|they|someone|anyone)\s+(?:built|build|made|shipped|wrote|coded|forked|"
    r"deployed|refactored|debugged)"
    r"|built\s+(?:a|an|my|our|the|with|on|using)"
    # HE WRITES "im", NOT "i'm". Measured against his archive: the first draft of this
    # alternation required `i am` / `i'm` and MISSED "im open sourcing spoody tracker this
    # monday" - one of only eleven practice posts he has ever written, and the announcement
    # of the artifact six of them are about. A regex calibrated on standard orthography
    # silently loses the thin lane it exists to find.
    r"|(?:i'?m|im|i|we)\s+(?:am\s+|are\s+)?open[- ]sourc\w*"
    r"|open[- ]sourcing\s+(?:my|our|the|it)"
    r"|spun up"
    r"|how\s+(?:i|we|you)\s+built"
    r"|(?:review|validate|test|debug)\w*\s+(?:llm|ai|agent|generated|model)\w*"
    r")\b", re.IGNORECASE)

# WHERE building happens. Nouns only - never a workflow word (see the disjointness rule).
BUILD_SURFACE = re.compile(
    r"\b(?:"
    r"claude code|cursor|codex|cline|aider|goose|windsurf|replit|zed|devin|copilot"
    r"|jetbrains|vscode|sub-?agents?"
    r"|(?:coding|ai|autonomous) agents?|coding assistants?|agentic coding|pair programm\w*"
    r"|terminal|ide|mcp|cli|sdk|repo|github"
    r")\b", re.IGNORECASE)

# HOW the building goes - workflow, delegation, breakage, cost-of-use. Never a surface noun.
# THE LEADING \b CANNOT WRAP EVERY ALTERNATIVE. `\b(?:...|\$\d+ a month)` never matches:
# in "costs up to $200 a month" the character before "$" is a space, and space-to-"$" is not
# a word boundary because NEITHER side is a word character. Written that way the whole
# cost-of-use frame was dead, and "Claude Code costs up to $200 a month" - the 214-day-old
# story this campaign keeps citing - classified as NEITHER lane. The money alternatives are
# therefore anchored outside the \b group, not inside it.
BUILD_FRAME = re.compile(
    r"(?:\b(?:"
    r"workflow|dev team|in parallel|delegat\w*|babysit\w*"
    r"|(?:writes|reviews|checks|tests|debugs)\s+(?:the\s+|its\s+|your\s+)?code"
    r"|code review"
    # BREAKAGE, added 2026-08-21 to place "Codex on AWS bedrock bug causing 10x charges" —
    # 145 points on HN, the best practice-lane candidate in the live pool, and previously
    # dropped because `bug` was not frame vocabulary.
    #
    # MEASURED BEFORE SHIPPING, and the measurement cut it in half. The obvious version
    # added the COST family too (`charges`, `billing`, `cost me`). Zero posts in his archive
    # moved either way — but a probe of adversarial cases found a real cross-lane error:
    #     "OpenAI charges more for the Codex API from next month"  ->  PRACTICE
    # a pricing story pulled into the practice lane by `charges` (CAPABILITY_CLAIM does not
    # list `charges`, so frontier never even contested it). Per §6.4 a vocabulary addition
    # that moves ONE cross-lane error does not ship, so only the breakage half landed.
    # Breakage alone still places the target story, on `bug`.
    #
    # NOTE HONESTLY: zero archive movement means this addition is UNTESTED on his corpus —
    # its benefit is demonstrated on live records, its zero cost on the archive.
    r"|breaks|broke|broken|fails|stuck|regression|bugs?|buggy|crash\w*"
    r"|context (?:window|rot|limit)"
    r"|sandbox\w*|software creation"
    r"|build (?:your own|software|features|apps)"
    r"|no coding required|free (?:mode|tier)s?|api keys|self[- ]host\w*"
    r"|sessions|talk to each other"
    r")\b"
    r"|costs?\s+(?:up\s+to\s+)?\$\d"
    r"|\$\d+\s*(?:a|per|/)\s*month"
    r")", re.IGNORECASE)


# --------------------------------------------------------------------------------------
# FRONTIER - a NAME plus an EVENT. Two structural floors carry unknown names.
# --------------------------------------------------------------------------------------

LAB = re.compile(
    r"\b(?:anthropic|openai|xai|deepmind|meta superintelligence|moonshot|mistral|alibaba"
    r"|qwen|deepseek|nvidia|together ai|liquid ai|nous research|cohere)\b", re.IGNORECASE)

# Version-glued names must match: "Qwen3.5" never matched `qwen` because the group's trailing
# \b fails between "n" and "3". That bug is already fixed in `subject._BUILDER_THINGS`; the
# fix is inherited here rather than rediscovered.
# `codex` sits here AND in BUILD_SURFACE, deliberately. The disjointness rule governs the two
# halves of the PRACTICE conjunction (SURFACE vs FRAME); it says nothing about a token being
# both a build surface and a model name. That double membership IS the design's observation
# that "Codex appears in all three classes" - "Codex app rollout" is a release, "people would
# rather talk to their agents than babysit a terminal" is practice - and the head tie-break is
# what separates them.
MODEL_NAME = re.compile(
    r"\b(?:opus|sonnet|haiku|claude|fable|gpt|chatgpt|grok|gemini|gemma|llama|mixtral"
    r"|mistral|kimi|qwen|deepseek|nemotron|glm|astra|lfm|nouscoder|codex)[\d.\-]*\b",
    re.IGNORECASE)

# A family IMMEDIATELY followed by a version. Structural, so an unknown lab shipping
# "Foo 3.1" still classifies - the partial answer to a word list going stale.
VERSIONED_MODEL = re.compile(
    r"\b(?:opus|sonnet|haiku|claude|fable|gpt|chatgpt|grok|gemini|gemma|llama|mixtral"
    r"|mistral|kimi|qwen|deepseek|nemotron|glm|astra|lfm|nouscoder)"
    r"[- ]?\d+(?:\.\d+)?\b", re.IGNORECASE)

RELEASE_EVENT = re.compile(
    r"\b(?:launch\w*|releas\w*|ship\w*|announc\w*|introduc\w*|unveil\w*|roll(?:s|ing)?[- ]?out"
    r"|preview|beta|eap|now (?:live|available)|is (?:here|out)|drops"
    r"|delay\w*|rumor\w*|rumour\w*|leak\w*|predict\w*|coming soon|expected on)\b",
    re.IGNORECASE)

CAPABILITY_CLAIM = re.compile(
    r"\b(?:benchmark\w*|evals?|leaderboard|swe-?bench|mmlu|deepswe|pass@\d+"
    r"|outperform\w*|beats|tops|context window|parameters|open[- ]weights?"
    r"|pricing|price|cheaper|per[- ]token|usage (?:limits|credits)|rate limits|quota"
    r"|inference|throughput|latency|reasoning|capabilit\w*)\b", re.IGNORECASE)


_PRACTICE_TESTS = {"BUILD_ACT": BUILD_ACT, "BUILD_SURFACE": BUILD_SURFACE,
                   "BUILD_FRAME": BUILD_FRAME}
_FRONTIER_TESTS = {"LAB": LAB, "MODEL_NAME": MODEL_NAME, "VERSIONED_MODEL": VERSIONED_MODEL,
                   "PARAM_COUNT": _PARAMETER_COUNT_RE, "RELEASE_EVENT": RELEASE_EVENT,
                   "CAPABILITY_CLAIM": CAPABILITY_CLAIM}


@dataclass(frozen=True)
class LaneVerdict:
    """lane: None = off both lanes.

    score  0.0 none · 0.5 both lanes fired and the tie was broken · 0.7 a structural floor
           alone · 1.0 the lane's primary conjunction fired.
    matched: test-name -> the literal text that matched. The names are the module-level
           regex names verbatim, so `--check`, the audition trail and a bug report all say
           the same thing.
    """
    lane: str | None
    score: float
    matched: dict[str, str]
    reason: str


def _hits(tests: dict[str, re.Pattern[str]], text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, pattern in tests.items():
        found = pattern.search(text)
        if found:
            out[name] = found.group(0)
    return out


def _surface_spans(text: str) -> list[tuple[int, int]]:
    """Every span where a BUILD_SURFACE phrase matched. ALL of them, not the first.

    `search` would find one occurrence; a headline can name two tools.
    """
    return [(m.start(), m.end()) for m in BUILD_SURFACE.finditer(text)]


def _frontier_hits_outside_the_tool_name(text: str) -> dict[str, str]:
    """Frontier hits, MINUS any whose every occurrence sits inside a build-surface phrase.

    SPAN CONTAINMENT. `"Claude Code"` contains `"Claude"`, so `MODEL_NAME` fires on a substring
    of the tool's own name and the headline looks like it names a frontier model when it names
    an editor. A frontier name that is PART OF THE TOOL'S NAME is not an independent frontier
    signal - which is a fact about where the match landed, not about more vocabulary.

    A hit survives if ANY of its occurrences falls outside every surface span. That matters:
    "Anthropic is turning Claude Code's auto mode on" names Anthropic OUTSIDE the surface
    phrase, so `LAB` survives and the headline stays unlaned - correctly, because that is a
    genuine independent frontier signal. Only the substring collision is discounted.

    STRICTLY INSIDE A *LONGER* SURFACE PHRASE - AN IDENTICAL SPAN IS NOT CONTAINMENT.
    `"Codex app rollout"` matches BUILD_SURFACE and MODEL_NAME on the SAME five characters,
    because `Codex` is both a tool and a model name in its own right. Treating that as
    containment discarded the frontier signal and turned the design's OWN WORKED EXAMPLE of a
    release into weak practice - caught by `test_documented_cases`. The rule is about a name
    BURIED IN a longer tool name, so the enclosing span must actually be longer.
    """
    spans = _surface_spans(text)
    kept: dict[str, str] = {}
    for name, pattern in _FRONTIER_TESTS.items():
        for found in pattern.finditer(text):
            buried = any(found.start() >= start and found.end() <= end
                         and (end - start) > (found.end() - found.start())
                         for start, end in spans)
            if not buried:
                kept[name] = found.group(0)
                break
    return kept


def _first_at(matched: dict[str, str], text: str) -> int:
    """Earliest character offset of any matched literal - the tie-break's whole basis."""
    low = text.lower()
    spots = [low.find(value.lower()) for value in matched.values()]
    spots = [s for s in spots if s >= 0]
    return min(spots) if spots else len(text) + 1


def _verdict(text: str) -> LaneVerdict:
    body = str(text or "").strip()
    if not body:
        return LaneVerdict(None, 0.0, {}, "empty")

    # A funding, policy or incident-triage story is off both lanes whoever it names. This is
    # `subject`'s calibrated genre kill, reused rather than re-derived - and it is a GENRE
    # test, never a subject test, so it cannot delete his Spider-Man practice posts.
    if _GENRE_KILL.search(body):
        return LaneVerdict(None, 0.0, {}, "off-lane genre (funding / policy / incident)")

    p_hits = _hits(_PRACTICE_TESTS, body)
    f_hits = _frontier_hits_outside_the_tool_name(body)

    act = "BUILD_ACT" in p_hits
    surface_and_frame = "BUILD_SURFACE" in p_hits and "BUILD_FRAME" in p_hits
    practice = act or surface_and_frame

    named = {"LAB", "MODEL_NAME"} & set(f_hits)
    param = "PARAM_COUNT" in f_hits
    evented = {"RELEASE_EVENT", "CAPABILITY_CLAIM"} & set(f_hits)
    versioned = "VERSIONED_MODEL" in f_hits
    frontier = bool(((named or param) and evented) or versioned or (param and named))

    p_only = {k: v for k, v in p_hits.items()
              if k == "BUILD_ACT" or surface_and_frame}
    f_only = {k: v for k, v in f_hits.items()}

    if practice and frontier:
        # WHAT IS THE SENTENCE ABOUT? A crude stand-in - whichever lane's evidence appears
        # earliest in the head - and right on the cases that matter:
        #   "Claude Code costs up to $200 a month. Goose does the same for free" -> practice
        #   "DeepSeek V4 Pro vs Claude Fable 5 on DeepSWE"                       -> frontier
        # A contested verdict is VISIBLY weaker than a clean one, hence score 0.5.
        head = body[:_HEAD_CHARS]
        lane = PRACTICE if _first_at(p_only, head) <= _first_at(f_only, head) else FRONTIER
        both = {**p_only, **f_only}
        return LaneVerdict(lane, 0.5, both, f"both lanes fired; {lane} led in the head")

    if practice:
        return LaneVerdict(
            PRACTICE, 1.0 if act else 0.7, p_only,
            "an act of building" if act else "a build surface in a workflow frame")

    if frontier:
        strong = bool((named or param) and evented)
        return LaneVerdict(
            FRONTIER, 1.0 if strong else 0.7, f_only,
            "a named model with an event" if strong else "a versioned or sized model")

    # SURFACE ALONE IS A WEAK PRACTICE VERDICT, not silence.
    #
    # A bare tool announcement - "Auto mode is now the default in Claude Code" - carries no
    # workflow FRAME, so the practice conjunction cannot fire, and no release EVENT, so
    # frontier cannot either. It fell out of both lanes while holding the evidence.
    #
    # WHY THAT MATTERS MORE THAN A MISSED CLASSIFICATION: `radar._selector_key` term 2 sinks
    # an unlaned candidate below every laned one, and `_lane_rotation_rank` - term 5, built
    # for the thin practice lane - cannot rescue it from four positions later. Worse, the
    # wait card reads `off_lane` and would tell him a CLAUDE CODE story "names nothing this
    # account writes about". Measured against the deployed key, the story ranks top-tier
    # today and is demoted after this change: a net regression on the exact lane this work
    # exists to serve.
    #
    # 0.5, DELIBERATELY - the same score a contested tie-break gets. A surface with nothing
    # said about it is the weakest honest verdict available, and a consumer can tell it from
    # a clean one.
    #
    # THIS RULE AND SPAN CONTAINMENT ARE JOINTLY NECESSARY, and each has a negative control:
    # without containment, `MODEL_NAME` survives on the substring of "Claude Code" and this
    # branch never runs for his most-used tool; without this branch, containment removes a
    # spurious frontier hit and the headline is still unlaned for want of a frame.
    if (set(p_hits) == {"BUILD_SURFACE"}) and not f_hits:
        return LaneVerdict(PRACTICE, 0.5, dict(p_hits),
                           "a build surface with nothing said about it")

    return LaneVerdict(None, 0.0, {}, "neither lane")


def classify(text: str, *, evidence: str = "") -> LaneVerdict:
    """EVIDENCE MAY CONFIRM A LANE, NEVER CREATE ONE.

    Frontier needs an object; practice needs an act. A frontier verdict is reachable from a
    headline alone - the object and the event are both in the noun phrase. A practice verdict
    usually needs a PREDICATE, which headlines bury in the summary. That asymmetry is exactly
    why evidence must not vote: letting it decide took the 128-item pool from 7 practice
    claims, all correct, to 13 with 6 wrong - "A shot-scraper-style JSON API on Bun 1.4"
    (matched `terminal` + `in parallel` from its summary), "Mojo is now open source"
    (`they shipped`), "Gemini Robotics ER 2", and a VentureBeat staff announcement.

    It also means a lane verdict is stable whether or not the article fetch succeeded, so the
    audition can re-audit without the lane changing under it.
    """
    verdict = _verdict(text)
    if verdict.lane and evidence:
        if _verdict(evidence).lane == verdict.lane:
            verdict = replace(verdict, score=min(1.0, verdict.score + 0.2))
    return verdict


def lane_of(candidate: Mapping[str, Any]) -> str | None:
    """The lane of a candidate dict, read from its topic with its evidence as confirmation."""
    if not candidate:
        return None
    stored = candidate.get("lane")
    if stored in LANES:
        return str(stored)
    return classify(str(candidate.get("topic") or ""),
                    evidence=str(candidate.get("evidence") or "")).lane


# --------------------------------------------------------------------------------------
# The privacy-clean fetch surface.
# --------------------------------------------------------------------------------------

# AUTHORED IN-REPO AND ENUMERABLE. All five of `research.AUTHORED_QUERY_TEXTS` are preserved
# here, so this is a widening of an already-shipped list rather than a new vocabulary. The
# declared lane selects a bucket; the account's own words are never sent anywhere.
LANE_QUERIES: dict[str, tuple[str, ...]] = {
    PRACTICE: ("coding agent", "developer tools AI", "agentic coding workflow",
               "LLM code review", "AI pair programming"),
    FRONTIER: ("AI model release", "LLM pricing", "open source model",
               "frontier model benchmark", "AI lab announcement"),
}
ALL_QUERIES: tuple[str, ...] = tuple(chain.from_iterable(LANE_QUERIES.values()))


def queries_for(lane: str | None) -> tuple[str, ...]:
    """Authored strings for a lane; the widest authored union when the lane is unknown.

    UNKNOWN MUST NOT MEAN NOTHING. Sources that send no query at all are lane-independent, so
    an unknown lane still needs the widest net rather than an empty one.
    """
    return LANE_QUERIES.get(str(lane or ""), ALL_QUERIES)


def declared_lanes() -> tuple[str, ...]:
    """Lanes the USER declared, if any. Read lazily so this module imports without a DB."""
    try:
        from .storage import get_account_profile
        profile = get_account_profile() or {}
    except Exception:
        return ()
    declared = profile.get("content_pillars") or profile.get("lanes") or []
    out = [lane for lane in LANES
           if any(lane.lower() == str(item).strip().lower() for item in declared)]
    return tuple(out)


def label_for_texts(texts: Sequence[str]) -> str | None:
    """A lane name for a group of texts, or None to keep the caller's own label.

    Requires >= 3 members that classify AND >= 60% agreement on one lane. The None fallback is
    REQUIRED, not a courtesy: it is what stops a food blogger's dimension being labelled
    "frontier models".
    """
    verdicts = [classify(str(t or "")).lane for t in texts or []]
    landed = [lane for lane in verdicts if lane]
    if len(landed) < 3:
        return None
    for lane in LANES:
        if landed.count(lane) / len(landed) >= 0.6:
            return lane
    return None
