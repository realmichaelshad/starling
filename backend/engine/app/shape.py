"""SHAPE — the silhouette of a post, sampled from the account's own spread.

The register work made drafts sound like the account. This module exists
because they still all LOOKED alike. Measured 2026-07-29 over 663 generated
post drafts against the account's 40 archived texts:

                        archive   drafts
    exactly 2 blocks      27.5%    71.3%   <- the template
    3, 4 or 5 blocks      37.5%     0.3%   <- he does this constantly
    lowercase opener      17.5%     0.2%   <- gone entirely
    NAME opener           57.5%    85.4%
    ALL-CAPS word         22.5%    45.9%
    distinct shapes/text   0.60     0.08   <- sevenfold collapse

The cause is not the model being lazy. `account_model.style_directive` states
the account's MEDIAN shape ("use ~2 short blocks with blank lines between"),
and an instruction to write the median produces exactly zero variance — every
draft becomes the average of the writer. The rate thresholds do the same to
case: lowercase_start_rate 0.175 is below the 0.5 cut, so the directive says
"normal sentence case" and one draft in 663 ever opens lowercase, against
seven of his forty posts.

So this module keeps the DISTRIBUTION, not the average, and hands the writer
one concrete silhouette per draft — sampled from what this account actually
does, excluding the shapes its last few drafts already used. Nothing here is
specific to any account: an archive of food posts yields food-post shapes.

A signature is what a reader takes in before reading a word:

    3B/4S/LOWER/PUNCH   three blocks, four sentences, lowercase opener,
                        short final line

Sampling, not randomness: weights come from the account's own histogram, so a
shape it has never used is never offered, and a shape it uses 15% of the time
is offered about 15% of the time.
"""
from __future__ import annotations

import random
import re
from typing import Any

# Sentence openers that point at something instead of naming it. Kept
# separate from a name so "It's an undo button" and "Snapshield is an undo
# button" are different silhouettes — which they are to a reader.
_POINTING_WORDS = frozenset("""
it its it's this that that's they they're the a an we i you your so and but if
when what why how no not just there here most every all
""".split())

_RECENT_SHAPES_KEY = "recent_post_shapes"
_RECENT_SHAPES_KEEP = 6

# SIZE IS PART OF HIS VOICE — his words, 2026-07-31, holding a 13-word draft:
# "when we say we write in our own voice we mean size too... it should
# calculate what would be the best way and make sure not to be repetitive."
# Measured the same night: his archive runs median 24 words with 30% of posts
# at 31-50, while the last 120 generated drafts had collapsed to median 17.5
# with 40% at or under 15 words — reply-sized posts wearing his account. The
# size classes below cut his archive into thirds (12 short / 17 medium / 14
# long of 43), so preferring a class never asks for a size he does not write.
_RECENT_SIZES_KEY = "recent_post_sizes"
_RECENT_SIZES_KEEP = 6
_SHORT_MAX_WORDS = 15
_MEDIUM_MAX_WORDS = 30
# Material thresholds for the size plan, calibrated against the same corpus
# as the topic audition: the arena's known-good evidence blocks run 177-234
# chars total (medium material — his median-length posts), and enriched
# article evidence runs 536-802 (the long-post case). Thin material must
# never be stretched: a long post about a bare headline is invented, which
# is the starvation defect the evidence work exists to prevent.
_RICH_MATERIAL_CHARS = 380
_SOME_MATERIAL_CHARS = 140


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "").strip())
            if s.strip()]


def block_count(text: str) -> int:
    return len([b for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()])


def opener_class(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return "EMPTY"
    if body[0] in '"“':
        return "QUOTE"
    parts = sentences(body)
    first = parts[0] if parts else body
    head = (first.split() or [""])[0].strip('"“‘\'.,:;')
    if not head:
        return "OTHER"
    if re.match(r"^[\$\d]", head):
        return "NUMBER"
    if head[:1].islower():
        return "LOWER"
    if head.lower() in _POINTING_WORDS:
        return "PRONOUN"
    if head[:1].isupper():
        return "NAME"
    return "OTHER"


def closer_class(text: str) -> str:
    parts = sentences(text)
    if not parts:
        return "EMPTY"
    last = parts[-1]
    if last.rstrip().endswith("?"):
        return "QUESTION"
    if len(last.split()) <= 5:
        return "PUNCH"
    return "STATEMENT"


def signature(text: str) -> str:
    """The reader's-eye silhouette, e.g. "2B/3S/NAME/PUNCH"."""
    return (f"{block_count(text)}B/{len(sentences(text))}S/"
            f"{opener_class(text)}/{closer_class(text)}")


def coarse_signature(text: str) -> str:
    """Blocks and opener only — the axes a reader notices while scrolling.

    Anti-repeat runs on THIS, not the full signature: two drafts at
    2B/2S/NAME and 2B/3S/NAME are the same post to a reader even though
    their sentence counts differ, and requiring the full signature to
    change would let that pair through.
    """
    return f"{block_count(text)}B/{opener_class(text)}"


def _archive_texts() -> list[str]:
    try:
        from .storage import connect
        with connect() as conn:
            # A deleted post is not his shape any more than it is his
            # performance; filter mirrors performance_records' stance.
            return [r[0] for r in conn.execute(
                "select text from posts where text is not null and deleted_at is null") if r[0]]
    except Exception:                                    # noqa: BLE001
        return []


def _corpus_key(texts: list[str]) -> tuple[int, int]:
    # Keyed on corpus_version AND count: version catches same-count edits and
    # deletions; count keeps tests that write posts without touching the
    # version counter (direct sqlite seeding) honest.
    try:
        from .storage import corpus_version
        return (corpus_version(), len(texts))
    except Exception:                                    # noqa: BLE001
        return (-1, len(texts))


_DISTRIBUTION_CACHE: tuple[tuple[int, int], dict[str, Any]] | None = None


def size_class(word_count: int) -> str:
    """short / medium / long, cut at his archive's natural thirds."""
    if word_count <= _SHORT_MAX_WORDS:
        return "short"
    if word_count <= _MEDIUM_MAX_WORDS:
        return "medium"
    return "long"


def account_shape_distribution() -> dict[str, Any]:
    """What silhouettes THIS account actually uses, with weights.

    Returns {"shapes": {signature: count}, "sample": int, "by_class":
    {size_class: {signature: count}}}. Cached against the archive size so an
    import or a new post refreshes it.
    """
    global _DISTRIBUTION_CACHE
    texts = _archive_texts()
    cache_key = _corpus_key(texts)
    if _DISTRIBUTION_CACHE is not None and _DISTRIBUTION_CACHE[0] == cache_key:
        return _DISTRIBUTION_CACHE[1]
    shapes: dict[str, int] = {}
    by_class: dict[str, dict[str, int]] = {}
    for text in texts:
        # A post whose sentences are too many to be a single tweet-shaped
        # thought is still his shape; nothing is filtered here except empties.
        if not text.strip():
            continue
        sig = signature(text)
        shapes[sig] = shapes.get(sig, 0) + 1
        bucket = by_class.setdefault(size_class(len(text.split())), {})
        bucket[sig] = bucket.get(sig, 0) + 1
    result = {"shapes": shapes, "sample": len([t for t in texts if t.strip()]),
              "by_class": by_class}
    _DISTRIBUTION_CACHE = (cache_key, result)
    return result


def reset_shape_cache() -> None:
    global _DISTRIBUTION_CACHE
    _DISTRIBUTION_CACHE = None


_ONE_BLOCK_ASKED = re.compile(r"Write it as ONE block", re.IGNORECASE)
_N_BLOCKS_ASKED = re.compile(r"Write it as (\d+) blocks")


_SENTENCES_ASKED = re.compile(r"(\d+)\s+sentences?\s+total|,\s*(\d+)\s+sentence")


def asked_sentences(direction: str) -> int | None:
    """The sentence count this request's shape block asked for, or None.

    THE OTHER HALF OF THE SHAPE CONTRACT, AND NOTHING READ IT. Layout is
    enforced from `asked_blocks`, but the enforcement is capped at the
    sentence count the model actually produced — this function never splits a
    sentence, so four blocks out of two sentences is two blocks. The block
    count therefore reads as "enforced" while the silhouette still collapses
    toward the median whenever the model under-produces sentences, which is
    the majority case for the 3-, 4- and 5-block shapes that make up 37.5% of
    his archive. Reading the number back is what lets the retry door see a
    shape that was asked for and not delivered.
    """
    match = _SENTENCES_ASKED.search(direction or "")
    if not match:
        return None
    value = match.group(1) or match.group(2)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_WORDS_ASKED = re.compile(r"about (\d+) words")


def asked_words(direction: str) -> int | None:
    """The word target this request's shape block asked for, or None.

    Read back for the same reason `asked_sentences` is: the model follows
    rhetoric and ignores arithmetic. His press on 2026-07-31 asked "2 blocks,
    2 sentences, about 24 words" and shipped seven words in one sentence —
    `shape_was_delivered`'s sentence check passes at wanted-1 (2 -> 1), so a
    word floor is the only door that can see this collapse.
    """
    match = _WORDS_ASKED.search(direction or "")
    try:
        return int(match.group(1)) if match else None
    except (TypeError, ValueError):
        return None


def size_was_delivered(text: str, direction: str) -> bool:
    """Did the draft come back anywhere near the size that was asked?

    Fires only when the plan asked for his medium or long sizes (>= 16
    words) and the draft delivered less than half — a planned SHORT post at
    seven words is his own voice (his archive starts at 4), and a modest
    miss on a big shape is normal model wobble the shaper absorbs.
    """
    wanted = asked_words(direction)
    if not wanted or wanted < 16:
        return True
    return len((text or "").split()) >= wanted * 0.5


def shape_was_delivered(text: str, direction: str) -> bool:
    """Did the draft actually come back in the silhouette that was asked for?

    Deliberately lenient on blocks (the shaper enforces those deterministically
    after the fact) and strict on sentence supply, because that is the input
    the shaper cannot manufacture.
    """
    wanted = asked_sentences(direction)
    if not wanted or wanted < 2:
        return True
    return len(sentences(text)) >= wanted - 1


def asked_blocks(direction: str) -> int | None:
    """The block count this request's shape block asked for, or None.

    The contract travels IN the direction rather than through a parameter, so
    every path that composes a direction — the composer, the arena, any future
    caller — gets layout enforcement for free, and the stored request_json
    keeps a record of what was asked. That record is what proved layout needed
    enforcing: measured 2026-07-29 across 32 drafts carrying a shape block,
    opener and closer instructions were obeyed 96.9% of the time and block
    count only 43.8%, with every 4- and 5-block request collapsing to one or
    two. The model follows rhetoric and ignores layout, so rhetoric stays a
    request and layout becomes a mechanism.
    """
    body = direction or ""
    if _ONE_BLOCK_ASKED.search(body):
        return 1
    match = _N_BLOCKS_ASKED.search(body)
    return int(match.group(1)) if match else None


def account_words_by_blocks() -> dict[int, int]:
    """Median word count per block count, from this account's own posts.

    Length has to be conditioned on shape or the two instructions fight: a
    global median of 25 words plus a one-sentence shape produced a 38-word
    run-on that failed the register gate (live, 2026-07-29). His one-block
    posts are short and his four-block posts are not, and only the archive
    knows by how much.
    """
    buckets: dict[int, list[int]] = {}
    for text in _archive_texts():
        if not text.strip():
            continue
        buckets.setdefault(block_count(text), []).append(len(text.split()))
    out: dict[int, int] = {}
    for blocks, lengths in buckets.items():
        lengths.sort()
        out[blocks] = lengths[len(lengths) // 2]
    return out


def account_block_counts() -> set[int]:
    """Every block count this account actually publishes.

    The post shaper uses this to know what NOT to touch. Measured on this
    archive: {1, 2, 3, 4, 5} — and the shaper was rewriting all of 3, 4 and 5
    down to the median 2, which is why multi-block drafts appeared in 0.3% of
    663 generated posts against 37.5% of his own.
    """
    counts: set[int] = set()
    for sig, weight in account_shape_distribution()["shapes"].items():
        parsed = parse_signature(sig)
        if parsed and weight:
            counts.add(parsed["blocks"])
    return counts


def recent_shapes() -> list[str]:
    """Coarse signatures of the last few from-scratch posts drafted."""
    try:
        from .storage import get_job_state
        state = get_job_state(_RECENT_SHAPES_KEY) or {}
        return [str(s) for s in (state.get("shapes") or [])][:_RECENT_SHAPES_KEEP]
    except Exception:                                    # noqa: BLE001
        return []


def record_shape(text: str) -> None:
    """Remember what the draft that just shipped looked like."""
    if not (text or "").strip():
        return
    try:
        from .storage import set_job_state
        shapes = [coarse_signature(text)] + recent_shapes()
        set_job_state(_RECENT_SHAPES_KEY,
                      {"shapes": shapes[:_RECENT_SHAPES_KEEP]})
    except Exception:                                    # noqa: BLE001
        pass


def recent_sizes() -> list[str]:
    """Size classes of the last few drafts, as DELIVERED — the model's real
    output, not what was asked, because delivery is what the reader sees and
    what a run of sameness is made of."""
    try:
        from .storage import get_job_state
        state = get_job_state(_RECENT_SIZES_KEY) or {}
        return [str(s) for s in (state.get("sizes") or [])][:_RECENT_SIZES_KEEP]
    except Exception:                                    # noqa: BLE001
        return []


def record_size(text: str) -> None:
    if not (text or "").strip():
        return
    try:
        from .storage import set_job_state
        sizes = [size_class(len(text.split()))] + recent_sizes()
        set_job_state(_RECENT_SIZES_KEY, {"sizes": sizes[:_RECENT_SIZES_KEEP]})
    except Exception:                                    # noqa: BLE001
        pass


def plan_size_classes(material_chars: int,
                      recent: list[str] | None = None) -> list[str]:
    """Ranked size classes for THIS draft — calculated, not drawn blind.

    The MATERIAL decides the base order: rich evidence carries his 31-50
    word shape, moderate evidence his median, and thin material must stay
    short — stretching a bare headline is how invented filler gets written.
    The recent RUN then breaks monotony: two same-class drafts in a row send
    that class to the back of the line, so three reply-sized posts running
    (the live complaint, 2026-07-31) cannot happen while richer shapes are
    available. The sampler still draws inside whichever class wins, so every
    silhouette offered remains one he actually publishes.
    """
    material = max(0, int(material_chars or 0))
    if material >= _RICH_MATERIAL_CHARS:
        ranked = ["long", "medium", "short"]
    elif material >= _SOME_MATERIAL_CHARS:
        ranked = ["medium", "long", "short"]
    else:
        ranked = ["short", "medium"]
    run = list(recent or [])
    if len(run) >= 2 and run[0] == run[1] and run[0] in ranked:
        ranked = [c for c in ranked if c != run[0]] + [run[0]]
    return ranked


def parse_signature(sig: str) -> dict[str, Any]:
    match = re.match(r"^(\d+)B/(\d+)S/([A-Z]+)/([A-Z]+)$", sig or "")
    if not match:
        return {}
    return {"blocks": int(match.group(1)), "sentences": int(match.group(2)),
            "opener": match.group(3), "closer": match.group(4)}


def sample_shape(*, exclude: list[str] | None = None,
                 rng: random.Random | None = None,
                 size_classes: list[str] | None = None) -> dict[str, Any] | None:
    """One silhouette for this draft, drawn from the account's own spread.

    `exclude` holds COARSE signatures to avoid (the recent drafts). When every
    weighted shape is excluded the exclusion is dropped rather than returning
    nothing — a repeated shape beats no shape guidance at all, which is how
    the drafts ended up at the median in the first place.

    `size_classes` is the plan from `plan_size_classes`, tried in order: the
    draw is restricted to silhouettes of archive posts in that size class, so
    the size is CHOSEN from the material while the silhouette stays sampled
    from what he publishes at that size. Classes his archive lacks fall
    through to the next; an empty plan falls back to the whole spread.

    None when the archive is too small to have a distribution worth sampling
    (under 8 posts): the existing median directive is then the honest answer.
    """
    distribution = account_shape_distribution()
    shapes = distribution["shapes"]
    if distribution["sample"] < 8 or not shapes:
        return None
    blocked = set(exclude or [])
    by_class = distribution.get("by_class") or {}
    candidate_pools: list[dict[str, int]] = [
        by_class[wanted] for wanted in (size_classes or [])
        if by_class.get(wanted)]
    candidate_pools.append(shapes)
    pool: list[tuple[str, int]] = []
    for source in candidate_pools:
        pool = [(sig, weight) for sig, weight in source.items()
                if coarse_signature_of_sig(sig) not in blocked]
        if pool:
            break
    if not pool:
        pool = list(shapes.items())
    picker = rng or random.Random()
    total = sum(weight for _, weight in pool)
    threshold = picker.uniform(0, total)
    running = 0.0
    chosen = pool[-1][0]
    for sig, weight in pool:
        running += weight
        if running >= threshold:
            chosen = sig
            break
    parsed = parse_signature(chosen)
    if not parsed:
        return None
    # NEVER ASK FOR A DRAFT THE GATES REJECT. A one-sentence QUESTION shape is
    # a post whose only sentence is a question, which is exactly what
    # `asks_without_saying` refuses — the archive's single instance is an
    # @-addressed reply to another account, a surface this sampler does not
    # serve. The closer degrades to a statement and the rest of the silhouette
    # stands. (A gate and a sampler that disagree is the prompt arguing with
    # itself one layer down; this module must not add a new one.)
    if parsed.get("closer") == "QUESTION" and int(parsed.get("sentences") or 1) <= 1:
        parsed["closer"] = "STATEMENT"
    parsed["signature"] = chosen
    return parsed


def coarse_signature_of_sig(sig: str) -> str:
    parsed = parse_signature(sig)
    if not parsed:
        return sig
    return f"{parsed['blocks']}B/{parsed['opener']}"


# TERSE ON PURPOSE. `DraftRequest.direction` is capped at 1200 characters and
# the evidence is budgeted into whatever is left, so every character this
# block spends is a character of FACTS the writer does not get. The first
# version ran ~300 characters of prose and starved the evidence outright
# (caught by its own regression test, 2026-07-29). Facts outrank silhouette:
# same instructions, a third of the room.
_OPENER_INSTRUCTION = {
    "NAME": "open with the product or model name",
    "NUMBER": "open with a figure",
    # "start the first word lowercase" alone lowercased the PRODUCT NAMES
    # too: measured 2026-07-29, "vibeue edits animsequences... it drives
    # metasond" (also a typo, since a mangled name invites one) and
    # "nouscoder-14B ... on swe-bench". A post whose product name is
    # lowercased reads as a mistake rather than a habit, and `names_nothing`
    # flags it because the subject extractor needs the capital.
    "LOWER": ("start the FIRST WORD lowercase, as they sometimes do, but keep "
              "every product, model and company name capitalised normally"),
    "PRONOUN": "open by pointing at it, then name it",
    "QUOTE": "open with a short quoted phrase",
    "OTHER": "",
}

_CLOSER_INSTRUCTION = {
    "STATEMENT": "end on a full statement",
    "PUNCH": "end on a line of five words or fewer",
    "QUESTION": "end on a real question",
    "EMPTY": "",
}


def shape_directive(target: dict[str, Any] | None) -> str:
    """The sampled silhouette as instructions the writer can follow."""
    if not target:
        return ""
    blocks = int(target.get("blocks") or 1)
    sentence_count = int(target.get("sentences") or 1)
    plural = "s" if sentence_count != 1 else ""
    if blocks <= 1:
        layout = f"Write it as ONE block, {sentence_count} sentence{plural}"
    else:
        layout = (f"Write it as {blocks} blocks separated by blank lines, "
                  f"{sentence_count} sentence{plural} total")
    # LENGTH BELONGS TO THE SHAPE. A global median of 25 words beside a
    # one-sentence shape produced a 38-word run-on that tripped the register
    # gate. One-block posts here run 14 words, four-block posts 47. Measured
    # per block count, per account.
    try:
        words = account_words_by_blocks().get(blocks)
    except Exception:                                    # noqa: BLE001
        words = None
    if words:
        layout += f", about {words} words"
    clauses = [layout]
    opener_class_name = str(target.get("opener") or "")
    opener = _OPENER_INSTRUCTION.get(opener_class_name, "")
    if opener_class_name == "PRONOUN" and sentence_count <= 1:
        # A one-sentence post has no "next sentence" to name the thing in, and
        # a pronoun opener whose subject is never named is exactly what
        # `dangles_its_subject` rejects — the sampler must not ask for a draft
        # the gates will flag.
        opener = "open by pointing at it, and name it in that same sentence"
    if opener:
        clauses.append(opener)
    closer = _CLOSER_INSTRUCTION.get(str(target.get("closer") or ""), "")
    if closer:
        clauses.append(closer)
    clauses.append("no sentence past 20 words")
    return "SHAPE FOR THIS POST: " + "; ".join(clauses) + "."
