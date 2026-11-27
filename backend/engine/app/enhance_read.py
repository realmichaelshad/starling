"""Read the draft before rewriting it.

Enhance was a copy-editor. `ENHANCE_DRAFT_RULES` is typo repair, sentence
length and plain words; the prompt says "Keep their point." and nothing ever
worked out what the point WAS. So the model was asked to preserve something
nobody had identified, on a surface where the whole job is not changing the
meaning.

This is the mirror of `point.decide_point`, which exists for the same complaint
in the other direction — his words, quoted in that module: "as a reader I'm not
getting what the post is trying to say — and prompting isn't what's going to
fix it." Creation solved it by choosing the point in its own call and writing to
it. Enhance cannot choose one: he already did. So it EXTRACTS his, and then two
things become possible that were not before.

  1. The rewrite is told what it is protecting, and where that point currently
     arrives. "Lead with it" is actionable; "keep their point" is not.
  2. `intelligence._planned_point` / `point.serves_the_point` start guarding
     enhance for free. They read the point out of `direction` and reject a
     candidate that no longer carries it — and a rejected candidate on this
     surface means the composer keeps HIS words, which is the correct outcome.
     That guard was already there; enhance simply never put anything in the
     field it reads.

Deliberately one extra small call, and deliberately fail-open: every failure
path returns None and enhance behaves exactly as it did before. `point.py`
records why that matters — "An earlier attempt put a planning call BEFORE
[generation], which forced a cold model load and pushed the whole request past
the timeout — 128s, and the composer got nothing." So this never runs unless
there is real budget left for it, and it is capped by what remains.
"""
from __future__ import annotations

import re

from . import deadline

# Enhance costs ~4.3s today (p50 over 15 real presses, max 9.6s). One planning
# call roughly doubles that, which is the trade `point.py` already accepted for
# creation. It is not worth making on a request that is nearly out of clock, so
# the read is skipped below this much remaining budget rather than eating into
# the rewrite the user actually asked for.
MIN_BUDGET_FOR_READ_S = 25.0

# Short output, low temperature: this is extraction, not writing, and a long
# answer here is a model narrating instead of answering.
_MAX_POINT_WORDS = 22

_PROMPT = """A writer has drafted this. Do not rewrite it. Say what it ARGUES.

DRAFT:
{draft}
{context}
Answer with the single point the draft makes: the one thing a reader could
repeat afterwards.

Rules for your answer:
- ONE sentence, under 20 words.
- Use the draft's own subject. Never introduce a subject the draft does not name.
- State the claim, not the topic.
- No preamble, no quotes, no explanation. The sentence only.

Bad:  "The draft talks about model pricing."           (that is the topic)
Good: "Cheap inference is a margin problem nobody is pricing in yet."

Bad:  "The writer thinks this is important."           (says nothing)
Good: "Groq's price cut only matters if the margins were never real."
"""


def _stem(word: str) -> str:
    """Possessive and plural off, and nothing else.

    `point._words` compares raw tokens, and against a point extracted from a
    DRAFT that is too strict to be usable: "Groq's price cut hides a margin
    problem" shares exactly one exact token with the draft it came from, because
    `groq's` is not `groq` and `price` is not `prices`. A correct read was being
    thrown away, which would have left this feature silently doing nothing most
    of the time — the worst failure mode available, since it looks like it works.

    Deliberately not a real stemmer. Two rules, both reversible by eye, is the
    most that can be added here without the comparison becoming something nobody
    can predict from reading a draft.
    """
    if word.endswith("'s") or word.endswith("’s"):
        word = word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9'’]+", (text or "").lower())
            if len(w) > 3}


def _clean(raw: str) -> str:
    """Same shape as `point._clean` — a model that explains itself, trimmed."""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    for marker in ("Point:", "point:", "Answer:", "answer:", "It argues that", "It argues"):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
    text = text.split("\n")[0].strip().lstrip(":").strip()
    # QUOTES COME OFF LAST. Stripping before the marker split leaves the quotes
    # that were wrapped around the sentence INSIDE the label ('Point: "..."'),
    # and a point carrying stray quote marks then goes into the rewrite prompt
    # as if he had written them.
    text = text.strip('"“”‘’').strip()
    words = text.split()
    if len(words) > _MAX_POINT_WORDS:
        text = " ".join(words[:_MAX_POINT_WORDS])
    return text


def read_the_point(llm, draft: str, *, context_text: str = "") -> str | None:
    """One sentence: what his draft argues. None when nothing usable comes back.

    Rejected on the same grounds as `point.decide_point`, plus the one that
    matters most here: a point that shares no vocabulary with the draft is the
    model writing its own post instead of reading his, and injecting that into
    the rewrite would change his meaning — the exact harm enhance exists to
    avoid.
    """
    body = str(draft or "").strip()
    if len(body.split()) < 6:
        # Too short to have a buried point, and too short for a rewrite to lose
        # one. Nothing this could tell the writer is worth a model call.
        return None

    left = deadline.remaining()
    if left is not None and left < MIN_BUDGET_FOR_READ_S:
        return None

    context_block = ""
    source = str(context_text or "").strip()
    if source:
        # Only as orientation. It is NOT the thing being summarised, and saying
        # so matters: given a post and a draft with no instruction about which
        # is which, the model reads back the post.
        context_block = (f"\nThe draft is a response to this post, for context only "
                         f"— do not describe the post:\n{source[:400]}\n")

    # NO `timeout` ARGUMENT, and no `lane="fast"`.
    #
    # `ProviderRouter.chat` takes neither: it already refuses to start when
    # `deadline.expired()`, and the transport under it caps every call with
    # `deadline.cap(CHAT_TIMEOUT_S)`. Passing a timeout here would be a second
    # spelling of a rule that is already enforced one layer down.
    #
    # The fast lane is deliberately not used even though this is analysis rather
    # than prose. What comes back decides what the rewrite is told to protect,
    # so a weaker read does not just lose a nicety — it hands the writer the
    # wrong point and gets his meaning changed, which is the one thing enhance
    # must never do. This call earns the quality model.
    try:
        raw = llm.router.chat(
            [{"role": "system",
              "content": "Return only the single sentence. No preamble."},
             {"role": "user", "content": _PROMPT.format(draft=body[:900],
                                                        context=context_block)}],
            options={"temperature": 0.2, "num_predict": 60, "timeout_s": 8.0},
            think=False)
    except Exception:                                    # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        from .llm import _strip_thinking
        raw = _strip_thinking(raw)
    except Exception:                                    # noqa: BLE001
        pass

    point = _clean(raw)
    return point if point_is_his(point, body) else None


def point_is_his(point: str, draft: str) -> bool:
    """Is this a point of HIS draft? The same test whichever call named it.

    S8 (2026-09-22): the enhance draft call now names the point itself, in the same
    object as the rewrite; this is the check the separate read's answer had to pass.
    IT HAS TO BE ABOUT HIS DRAFT: at least four words, and two distinctive words
    shared with his own text, mirroring `serves_the_point`'s own floor so the point is
    one that function can actually verify afterwards.
    """
    cleaned = _clean(point)
    if len(cleaned.split()) < 4:
        return False
    return len(_words(cleaned) & _words(str(draft or ""))) >= 2


_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")


def point_arrives_at(draft: str, point: str) -> int:
    """1-based index of the sentence where the point actually lands, or 0.

    Mechanical on purpose. The question "where does the point land" needs no
    model, and answering it in code means the instruction handed to the rewrite
    is a fact about his text rather than another opinion.

    STRONGEST OVERLAP, NOT FIRST MATCH. The first version returned the first
    sentence to share two words, which measured where the SUBJECT is rather than
    where the CLAIM is — and those are usually different sentences, which is the
    whole reason a point gets buried. Measured on the live case: the point
    "Nobody is pricing in the margin risk from Groq's cuts" shares {groq, cut}
    with the opening line "Groq cut inference prices again this morning", so it
    reported sentence 1, called the draft well-led, and added no instruction —
    while the actual claim sat alone in sentence 3 sharing four words. Taking the
    maximum finds the claim; ties go to the earliest, so a point genuinely spread
    across the opening still reads as led-with.
    """
    if not point:
        return 0
    subject = _words(point)
    if len(subject) < 2:
        return 0
    sentences = [s.strip() for s in _SENTENCE_RE.findall(str(draft or "")) if s.strip()]
    best_index, best_overlap = 0, 0
    for index, sentence in enumerate(sentences, start=1):
        overlap = len(subject & _words(sentence))
        if overlap > best_overlap:
            best_index, best_overlap = index, overlap
    return best_index if best_overlap >= 2 else 0


# Below this a "buried" point is not buried, it is just the second half of a
# two-sentence thought. Leading with it would flatten his shape for nothing.
_MIN_SENTENCES_TO_BURY = 3


def buried_note(draft: str, point: str) -> str:
    """NOT WIRED. Measured against real model output and it does not work —
    recorded here so it is not rebuilt from scratch.

    The idea was sound: tell the rewrite "your point does not arrive until
    sentence 3, open with it", which is actionable where "keep the point" is not.
    Locating the point by word overlap is what fails, and it failed twice.

    First version took the FIRST sentence sharing two words, which finds the
    SUBJECT rather than the claim — and a buried point is exactly the case where
    those differ. Fixed to take the strongest overlap. Then measured against what
    the model actually returns, and the real point was:

        "Nobody prices in Groq's margin risk after its inference price cuts."

    That is a summary of the WHOLE draft, so its words load the opening line —
    {groq, cut, inference, price} = 4 against sentence 1, versus {nobody, margin,
    risk} = 3 against the sentence carrying the actual claim. Sentence 1 wins,
    the draft reads as well-led, and no note is added. Phrase the point as a pure
    claim and it works; nothing makes the model phrase it that way.

    So this is a heuristic that fires on the shape of the point rather than on
    the shape of the draft, which is the definition of unpredictable. Locating a
    claim needs the model to say which sentence carries it — another call, on a
    surface already paying for one — or it needs to not be done. It is not done.

    `point_arrives_at` is kept and tested alongside it for the same reason: the
    next person to try this should be able to see how far it gets before it stops
    being trustworthy.
    """
    at = point_arrives_at(draft, point)
    sentences = [s for s in _SENTENCE_RE.findall(str(draft or "")) if s.strip()]
    if at <= 1 or len(sentences) < _MIN_SENTENCES_TO_BURY:
        return ""
    return (f"That point does not arrive until sentence {at} of {len(sentences)}. "
            f"Open with it, then keep their supporting detail behind it. "
            f"Do not add anything new to make room.")


# The exact string `intelligence._POINT_IN_DIRECTION_RE` matches. Kept as a
# format here rather than a second regex: two places spelling the same contract
# differently is how `_NAME_TOKEN_RE` ended up defined twice, silently.
POINT_DIRECTION_TEMPLATE = 'THE POINT OF THIS POST, decided already: "{point}"'


def direction_for_enhance(draft: str, point: str) -> str:
    """The `direction` text an enhance request should carry, or "".

    Written in the format the existing point guard already parses, so
    `_planned_point` finds it and `serves_the_point` protects it without a
    single new check being added anywhere.
    """
    if not point:
        return ""
    # `buried_note` is deliberately NOT appended — see its docstring. It fires on
    # the phrasing of the point rather than on the shape of the draft, so it is
    # measured, kept for reference, and not shipped. `draft` stays in the
    # signature because that is the argument a working position check would need.
    del draft
    return "\n".join([
        POINT_DIRECTION_TEMPLATE.format(point=point),
        "This is the writer's OWN point, taken from their own draft. "
        "Keep it exactly — you are changing how it reads, never what it says."])
