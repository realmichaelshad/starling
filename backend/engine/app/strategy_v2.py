"""Runtime ingestion of the bundled X craft manual (v2).

The manual ships with the engine (`app/data/strategy_v2.txt`). The parser splits
the numbered `======`-fenced sections, extracts the structures the runtime needs
(voice rules, banned phrases, angle library, length targets, hook families, tier
topics, scoring rubric), and precomputes one compact brief per purpose so prompts
stay small.

Everything degrades gracefully: a missing or reorganized file falls back to the
hand-distilled constants in `strategy_source.py`.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# SHA-256 of app/data/strategy_v2.txt as last reviewed. A mismatch changes only
# the status message; the manual is still parsed and used.
EXPECTED_SHA256 = "83251a7069545e20054b259633398c9a3687023632986aa8efacd8b57c42536a"
BRIEF_CHAR_BUDGET = 1500

_FENCE_RE = re.compile(r"^={10,}\s*$")
_TITLE_RE = re.compile(r"^(\d{1,2})\.\s+(.{3,80})$")
_SUBSECTION_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\s+(.{3,90})$")
_QUOTED_PHRASE_RE = re.compile(r'"([^"\n]{3,60})"')

# The nine §6 angle families double as the bandit's arm library. Slugs are
# stable identifiers; titles are matched loosely so renumbering survives.
ANGLE_LIBRARY = (
    ("direct_comparison", "Direct comparison"),
    ("contradiction", "Contradiction or hypocrisy"),
    ("real_user_pain", "Real user pain"),
    ("exact_prediction", "Exact prediction"),
    ("open_vs_closed", "Open versus closed"),
    ("benchmarks_vs_use", "Benchmarks versus actual use"),
    ("release_cadence", "Release cadence"),
    ("money_compute", "Money, compute, and incentives"),
    ("question_with_premise", "Question with a premise"),
)

LENGTH_TARGET_FALLBACK = {
    "reply": "about 40-120 visible characters when the idea allows it; every sentence must earn its place",
    "quote": "commentary shorter than a standalone; the first line must work without the embed",
    "post": "roughly 100-230 visible characters in one to four short blocks",
}


def _bundled_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "strategy_v2.txt"


@dataclass
class StrategyV2:
    loaded: bool = False
    path: str = ""
    sha256: str = ""
    sha_matches_distillation: bool = False
    # The account this manual and its verbatim replies belong to, or ""
    # if it does not say. A STATIC fact of the file, so it is parsed here;
    # whether THIS install is that account is resolved per call below,
    # never cached — see `exemplars_for_this_install`.
    owner_handle: str = ""
    sections: dict[int, dict[str, str]] = field(default_factory=dict)
    banned_phrases: list[str] = field(default_factory=list)
    voice_traits: list[str] = field(default_factory=list)
    openers: list[str] = field(default_factory=list)
    weak_endings: list[str] = field(default_factory=list)
    tier1_topics: list[str] = field(default_factory=list)
    tier2_topics: list[str] = field(default_factory=list)
    hook_families: list[str] = field(default_factory=list)
    scoring_components: list[str] = field(default_factory=list)
    exemplars: list[dict[str, Any]] = field(default_factory=list)
    length_targets: dict[str, str] = field(default_factory=lambda: dict(LENGTH_TARGET_FALLBACK))
    briefs: dict[str, str] = field(default_factory=dict)

    def section_by_title(self, fragment: str) -> str:
        fragment_l = fragment.lower()
        for section in self.sections.values():
            if fragment_l in section["title"].lower():
                return section["text"]
        return ""

    def brief_for(self, purpose: str) -> str:
        return self.briefs.get(purpose, "")


def _split_sections(raw: str) -> dict[int, dict[str, str]]:
    lines = raw.splitlines()
    sections: dict[int, dict[str, str]] = {}
    current: dict[str, Any] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if _FENCE_RE.match(line) and index + 2 < len(lines) and _FENCE_RE.match(lines[index + 2]):
            match = _TITLE_RE.match(lines[index + 1].strip())
            if match:
                if current is not None:
                    sections[current["number"]] = {"title": current["title"], "text": "\n".join(current["body"]).strip()}
                current = {"number": int(match.group(1)), "title": match.group(2).strip(), "body": []}
                index += 3
                continue
        if current is not None:
            current["body"].append(line)
        index += 1
    if current is not None:
        sections[current["number"]] = {"title": current["title"], "text": "\n".join(current["body"]).strip()}
    return sections


def _subsection(text: str, fragment: str) -> str:
    """Return the body of one `N.M TITLE` subsection whose title contains fragment."""
    lines = text.splitlines()
    collecting = False
    body: list[str] = []
    for line in lines:
        match = _SUBSECTION_RE.match(line.strip())
        if match:
            if collecting:
                break
            if fragment.lower() in match.group(3).lower():
                collecting = True
            continue
        if collecting:
            body.append(line)
    return "\n".join(body).strip()


def _bullets(text: str, limit: int = 40) -> list[str]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip().rstrip(";"))
        if len(items) >= limit:
            break
    return items


def _extract_banned_phrases(voice_section: str) -> list[str]:
    avoid = _subsection(voice_section, "WHAT TO AVOID")
    phrases = _QUOTED_PHRASE_RE.findall(avoid)
    return [phrase.strip() for phrase in phrases if phrase.strip()]


def _extract_exemplars(format_section: str) -> list[dict[str, Any]]:
    """Verbatim reply exemplars plus the WHY bullets that follow them."""
    exemplars: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", format_section)
    for position, block in enumerate(blocks):
        stripped = block.strip()
        if not (stripped.startswith('"') and stripped.endswith('"') and len(stripped) < 400):
            continue
        why: list[str] = []
        following_blocks = blocks[position + 1:position + 5]
        for block_index, following in enumerate(following_blocks):
            if following.strip().lower().startswith("why it worked"):
                # Bullets may share the header's block or sit in the next one.
                why = _bullets(following, limit=8)
                if not why and block_index + 1 < len(following_blocks):
                    why = _bullets(following_blocks[block_index + 1], limit=8)
                break
        exemplars.append({"text": stripped.strip('"'), "why": why, "format": "reply"})
    return exemplars


def _extract_length_targets(sections: dict[int, dict[str, str]]) -> dict[str, str]:
    targets = dict(LENGTH_TARGET_FALLBACK)
    for section in sections.values():
        if "voice model" not in section["title"].lower():
            continue
        guidance = _subsection(section["text"], "LENGTH GUIDANCE")
        reply_match = re.search(r"(?:about\s+)?(\d{2,3})[–-](\d{2,3}) visible characters", guidance)
        if reply_match:
            targets["reply"] = f"about {reply_match.group(1)}-{reply_match.group(2)} visible characters when the idea allows it"
        post_match = re.search(r"target:\s*(\d{2,3})[–-](\d{2,3}) visible characters", guidance)
        if post_match:
            targets["post"] = f"roughly {post_match.group(1)}-{post_match.group(2)} visible characters for a sharp take, one to four short blocks"
    return targets


# Words a truncated list must not end on. Cutting at a word boundary is not the
# same as cutting at a clean one: a topic list cut to end on "..., Codex, Grok,
# xAI in" was the real ending of 1,429 stored briefs.
_DANGLING = {"in", "on", "at", "to", "of", "or", "and", "for", "with", "by",
             "the", "a", "an", "from", "as", "into", "about", "like"}


def _tidy(text: str) -> str:
    """Close a truncated clause instead of leaving it mid-list."""
    text = text.rstrip()
    while True:
        stripped = text.rstrip(" ,;:-")
        head, _, last = stripped.rpartition(" ")
        if head and last.lower() in _DANGLING:
            text = head
            continue
        text = stripped
        break
    return text + "." if text and text[-1] not in ".!?" else text


def _fit_list(header: str, items: list[str], room: int, *, minimum: int = 3) -> str:
    """As many WHOLE items as fit, or nothing at all.

    Reserving the closing clause has to take its room from somewhere, and the
    topic list is the right donor — but a naive character cut produced
    "Core topics (highest." on the shipped pack: a header introducing no list, which is
    worse than no header. Items are dropped whole, and the block disappears
    entirely rather than shrink below `minimum`.
    """
    if room <= 0 or not items:
        return ""
    kept: list[str] = []
    for item in items:
        candidate = f"{header}{', '.join([*kept, item])}."
        if len(candidate) > room:
            break
        kept.append(item)
    return f"{header}{', '.join(kept)}." if len(kept) >= minimum else ""


def _compose(*parts: str, budget: int = BRIEF_CHAR_BUDGET, tail: str = "") -> str:
    """Assemble a brief, RESERVING the closing instruction rather than appending it.

    THE CUT LANDED ON THE ONE CLAUSE THAT MATTERS. `create_post` is the only
    brief that carries the topic list, which pushed it past the 1500 budget —
    and the truncation took the last thing added, which is the register
    instruction: *"It must be specific enough to react to, sound like the
    writer, and never read as corporate or generated."*

    Measured in the deployed tree: `create_post` came to 1494 characters with
    the clause ABSENT, ending on a dangling topic-list fragment — the identical
    ending of 1,429 stored briefs.
    `create_reply` (1491) and `create_quote` (1478) kept it, which is the
    control: same field, same builder, the defect confined to the one action
    that carries `tiers`.

    So the most-used door in the product — Create Post — has been asking for a
    post with no instruction not to sound generated, while the two rarer doors
    had it. `tail` is the `append_before_shape` shape: reserve the room, cut the
    BODY, then append. A brief that already fits is byte-identical either way.
    """
    text = " ".join(part.strip() for part in parts if part and part.strip())
    text = re.sub(r"\s+", " ", text)
    closing = re.sub(r"\s+", " ", tail.strip())
    # `_tidy` CLOSES the clause, which can ADD a character. Reserving only the
    # joining space put the first version of this one byte over the budget —
    # a fix for a truncation bug that overran the cap it was respecting.
    if not closing:
        return _tidy(text[:budget - 1].rsplit(" ", 1)[0]) if len(text) > budget else text
    room = budget - len(closing) - 2          # the joining space, and that period
    if len(text) > room:
        text = _tidy(text[:room].rsplit(" ", 1)[0])
    return f"{text} {closing}".strip()


# The register instruction, named because it is the thing that must survive.
CREATE_CLOSING = ("It must be specific enough to react to, sound like the writer, "
                  "and never read as corporate or generated.")


def _build_briefs(strategy: StrategyV2) -> dict[str, str]:
    voice = "Voice: " + "; ".join(strategy.voice_traits[:10]) + "." if strategy.voice_traits else ""
    banned = ("Never use these phrases (they sound generated): "
              + "; ".join(strategy.banned_phrases[:12]) + ".") if strategy.banned_phrases else ""
    goal = ("Goal: grow this account's views and followers legitimately - earn attention with a "
            "specific, useful, recognizable point. Never ask for follows, never bait engagement, never fabricate.")
    action_rules = {
        "reply": ("A reply earns borrowed reach when people other than the parent author want to react: "
                  "a clear opinion, comparison, correction, joke, or real user pain in one quick read. "
                  "No generic questions, no 'great point', no tagging bots for reach."),
        "quote": ("A quote is a restricted tool: the embedded source must be essential proof or context, and the "
                  "first line must work without the embed. Never quote just to repeat a headline or rescue a missed reply."),
        "post": ("A standalone post builds identity and converts profile visitors: one original thesis with a "
                 "recognizable company, a concrete fact, a contradiction or prediction, and a short conclusion with attitude."),
    }
    length = {action: f"Length target: {strategy.length_targets.get(action, '')}." for action in ("reply", "quote", "post")}
    hooks = ("Hook families that fit: " + "; ".join(strategy.hook_families[:7]) + ".") if strategy.hook_families else ""
    tier_header = "Core topics (highest fit): "
    typo_policy = ("Correct accidental typos; target natural informality, never artificial illiteracy; "
                   "short paragraphs and line breaks are in-voice.")

    briefs: dict[str, str] = {}
    for action in ("reply", "quote", "post"):
        briefs[f"enhance_{action}"] = _compose(
            goal, "Enhance the user's exact draft: preserve its real point, chosen format, and confidence level.",
            action_rules[action], voice, typo_policy, banned, length[action],
            tail="Never tell the user to wait; return one polished ready-to-post version.")
        # THE TOPIC LIST IS THE DONOR, and it gives whole topics rather than
        # characters. Composed twice on purpose: once to learn what the fixed
        # parts plus the reserved clause actually cost, then once for real with
        # the list sized to what is left, so the register instruction always
        # survives and the topic list gives up whole topics instead.
        fixed = (goal, f"Write one {action} from scratch in the writer's voice.",
                 action_rules[action], voice, typo_policy, banned, length[action], hooks)
        topics = ""
        if action == "post" and strategy.tier1_topics:
            spent = len(_compose(*fixed, tail=CREATE_CLOSING))
            topics = _fit_list(tier_header, strategy.tier1_topics[:14],
                               BRIEF_CHAR_BUDGET - spent - 1)
        briefs[f"create_{action}"] = _compose(*fixed, topics, tail=CREATE_CLOSING)
    briefs["chat_persona"] = _compose(
        "You speak like the account's candid strategist: direct, short, plain sentences, evidence first, no corporate tone.",
        voice, banned,
        "Replies acquire attention; standalone posts build the reason to follow; quotes need proof; WAIT is a valid call.",
        "Never promise virality; views without engagement or follows are incomplete success.")
    briefs["decision"] = _compose(
        "Decide exactly one action: reply, quote, post, or wait.",
        "Replies are the current acquisition engine (short, opinionated, under major relevant accounts, fresh or accelerating parents).",
        "Standalone posts are for original theses and follower conversion; quotes require the source as essential proof;",
        "wait beats a weak post - relevance alone is not enough, tagging bots is not a distribution strategy,",
        "generic questions are not growth posts.",
        "Freshness: under 30 minutes is the strongest window; 30-90 minutes viable; 90 minutes-3 hours only if the parent "
        "is still moving; past 4 hours normally skip - but VELOCITY MATTERS MORE THAN AGE: an old post still getting heavy "
        "engagement per minute can stay useful, and a 40-minute-old post with no movement may not be. Memes and live events "
        "decay in minutes; analysis and predictions decay slowly.",
        ("Scoring rubric: " + "; ".join(strategy.scoring_components[:8]) + ".") if strategy.scoring_components else "")
    return briefs


def _parse(raw: str, path: str) -> StrategyV2:
    strategy = StrategyV2(loaded=True, path=path)
    strategy.sha256 = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    strategy.sha_matches_distillation = strategy.sha256.lower() == EXPECTED_SHA256
    from .pack_ownership import declared_owner
    strategy.owner_handle = declared_owner(raw)
    strategy.sections = _split_sections(raw)

    voice_section = strategy.section_by_title("voice model")
    strategy.banned_phrases = _extract_banned_phrases(voice_section)
    core = _subsection(voice_section, "CORE VOICE CHARACTERISTICS")
    strategy.voice_traits = [item for item in _bullets(core, limit=14) if 2 < len(item) < 120]
    strategy.openers = _QUOTED_PHRASE_RE.findall(_subsection(voice_section, "OPENERS"))
    strategy.weak_endings = _QUOTED_PHRASE_RE.findall(_subsection(voice_section, "ENDINGS"))

    topic_section = strategy.section_by_title("topic selection")
    strategy.tier1_topics = _bullets(_subsection(topic_section, "TIER 1"), limit=20)
    strategy.tier2_topics = _bullets(_subsection(topic_section, "TIER 2"), limit=20)

    hook_section = strategy.section_by_title("hook engineering")
    strategy.hook_families = [line.strip().title() for line in hook_section.splitlines()
                              if line.strip() in ("CONSEQUENCE", "CONTRADICTION", "RESULT", "PREDICTION",
                                                  "ERROR", "PROOF", "TRADE-OFF")]

    scoring_section = strategy.section_by_title("scoring model")
    strategy.scoring_components = re.findall(r"^[A-H]\.\s+(.{5,70}?)\s+—\s+0 to \d+", scoring_section, flags=re.MULTILINE)

    strategy.exemplars = _extract_exemplars(strategy.section_by_title("format decision system"))
    strategy.length_targets = _extract_length_targets(strategy.sections)
    strategy.briefs = _build_briefs(strategy)
    return strategy


_LOCK = threading.Lock()
_CACHED: StrategyV2 | None = None


def exemplars_for_this_install() -> list[dict[str, Any]]:
    """The manual's verbatim reply exemplars, only for the account they belong to.

    Exemplars are one account's own words. A pack may carry them only with an
    `owner-handle:` declaration, and they reach drafts only on that account;
    every other install gets the craft and none of the person. The bundled
    manual declares no owner and carries no exemplars, so this is [] for every
    customer unless a pack is deliberately built for one account.

    RESOLVED HERE, NOT IN `_load`. The obvious seam is where `exemplars` is
    assigned — and it is the wrong one. `get_strategy()` caches the parsed
    manual in `_CACHED` for the life of the process, so filtering at parse time
    would bake the verdict in at first load: a fresh install with no handle
    recorded yet would lock in "no exemplars" and never re-evaluate, even after
    the archive import made them the owner. Same shape as every cached-pool bug
    in this codebase. The handle is read on every call instead; it is one
    indexed row and the pool it guards is three items.
    """
    strategy = get_strategy()
    if not strategy.exemplars:
        return []
    from .pack_ownership import normalize_handle
    try:
        from .storage import get_account_profile
        handle = normalize_handle((get_account_profile() or {}).get("handle"))
    except Exception:                                        # noqa: BLE001
        # An unreadable profile is not evidence of ownership. Failing closed
        # costs the owner a few examples for one call; failing open puts one
        # account's voice in another customer's draft.
        return []
    owner = normalize_handle(strategy.owner_handle)
    return list(strategy.exemplars) if owner and owner == handle else []


def get_strategy() -> StrategyV2:
    global _CACHED
    with _LOCK:
        if _CACHED is not None:
            return _CACHED
    strategy = _load()
    with _LOCK:
        _CACHED = strategy
    return strategy


def reload_strategy() -> StrategyV2:
    global _CACHED
    with _LOCK:
        _CACHED = None
    return get_strategy()


def _load() -> StrategyV2:
    # One manual for every customer: there is no per-user override and no
    # packed copy in the hosted engine.
    try:
        bundled = _bundled_path()
        if bundled.is_file():
            raw = bundled.read_text(encoding="utf-8", errors="replace")
            if raw.strip():
                return _parse(raw, str(bundled))
    except OSError:
        pass

    return StrategyV2(loaded=False)


def source_status() -> dict[str, Any]:
    strategy = get_strategy()
    return {
        "loaded": strategy.loaded,
        "path": strategy.path,
        "sections": len(strategy.sections),
        "sha_matches_distillation": strategy.sha_matches_distillation,
        "message": ("Strategy manual loaded." if strategy.loaded and strategy.sha_matches_distillation
                    else "Strategy manual loaded (updated since last distillation)." if strategy.loaded
                    else "Strategy manual missing; using built-in distilled rules."),
    }


def brief_for(purpose: str) -> str:
    return get_strategy().brief_for(purpose)


def angle_ids() -> list[str]:
    return [slug for slug, _ in ANGLE_LIBRARY]


def angle_title(slug: str) -> str:
    for angle_slug, title in ANGLE_LIBRARY:
        if angle_slug == slug:
            return title
    return slug.replace("_", " ")
