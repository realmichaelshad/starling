"""Turn a standing instruction in chat into a preference he only has to say once.

Two bugs made this dead code, and the table proves it: `preference_memories`
has never held a single row.

  1. Every match returned confidence of exactly 0.78, and the caller saved a
     proposal only when confidence was `< 0.78`. The one value this function
     can produce is precisely the value that excludes it, so nothing was ever
     persisted — at any confidence, for any instruction.
  2. Only "don't / do not / never X" was recognised. The ways he actually
     phrases a standing instruction — "always keep replies short", "stop
     starting posts with…", "no, shorter" — matched nothing at all.

Confidence now means something: a flat imperative ("never use hashtags") is
unambiguous and scores high; a fragment correcting the draft in front of him
("no, shorter") is a real instruction but a weaker guess about how widely it
applies, so it scores lower. Confirmation still gates persistence either way —
this only says how sure the proposal claims to be.
"""
from __future__ import annotations

import re

# (pattern, scope, rule template, confidence)
_PATTERNS: tuple[tuple[str, str, str, float], ...] = (
    (r"\bi(?: would|’d|'d) rather (.+)", "strategy", "Prefer {value}", 0.80),
    (r"\bi prefer (.+)", "voice", "Prefer {value}", 0.82),
    (r"\b(?:don'?t|do not|never|stop) (.+)", "strategy", "Avoid {value}", 0.86),
    (r"\bi want (.+)", "strategy", "Prioritize {value}", 0.80),
    (r"\bi hate (.+)", "voice", "Avoid {value}", 0.84),
    # STANDING POSITIVE INSTRUCTIONS. "always keep replies under 20 words" is
    # as much a rule as any prohibition, and matched nothing before.
    (r"\balways (.+)", "strategy", "Always {value}", 0.86),
    (r"\bkeep (?:it|them|posts?|replies) (.+)", "voice", "Keep {value}", 0.80),
    (r"\bmake (?:it|them|posts?|replies) (.+)", "voice", "Make it {value}", 0.78),
    (r"\bno more (.+)", "strategy", "Avoid {value}", 0.84),
    # BARE "no X" — "no hashtags", "no rhetorical questions", "no emojis". As
    # flat a standing rule as "never use hashtags" and it matched nothing.
    # Anchored and whole-message, with the conversational "no" excluded by its
    # next word, because "no i didnt" and "no thanks" are disagreement rather
    # than instruction and must not become "Avoid i didnt".
    (r"^no\s+(?!more\b|thanks?\b|worries\b|problem\b|idea\b|way\b|one\b|i\b|"
     r"it\b|that\b|this\b|you\b|we\b|im\b|i'?m\b|do\b|don'?t\b|did\b|"
     r"didn'?t\b|need\b|not\b)([a-z][\w' -]{2,60})$",
     "strategy", "Avoid {value}", 0.82),
)

# A correction aimed at the draft in front of him. A real instruction, but a
# weaker claim about how widely it applies — so it proposes rather than asserts.
_CORRECTION = re.compile(
    r"^(?:no,?\s+|nah,?\s+)?(shorter|longer|simpler|punchier|less formal|"
    r"more casual|more specific|less hype|calmer|sharper)\b", re.I)


# A QUESTION IS NOT AN INSTRUCTION. Every pattern below is matched with
# `re.search`, so a fragment buried in a sentence counted: asked "what do you
# think of my account honestly like what should I do to make it better",
# Starling answered "Noted — 'Make it better.' I'll hold every draft to that
# from now on." It had extracted a standing style rule from the middle of a
# strategy question and answered nothing that was asked.
#
# A standing instruction is the WHOLE message ("no hashtags", "always keep
# replies short"), short and declarative. These two guards are deliberately
# separate from the one in `_preference_ack`, because either alone leaves the
# other's holes.
_INTERROGATIVE = re.compile(
    r"^(?:what|how|why|when|where|which|who|should|shall|is|are|was|were|do|"
    r"does|did|can|could|would|will|am|any)\b", re.IGNORECASE)

# Long enough that a match is a fragment rather than the message itself. The
# real instructions are all well under this: "no hashtags" (11), "stop using em
# dashes" (20), "always keep replies under 20 words" (35).
_INSTRUCTION_MAX_CHARS = 60


def _is_question(cleaned: str, lowered: str) -> bool:
    if cleaned.endswith("?"):
        return True
    if _INTERROGATIVE.match(lowered):
        return True
    # "tell me what to do", "i want to know how" — an ask wearing a statement.
    return bool(re.search(r"\b(?:what do you think|what should i|how (?:do|can) i|"
                          r"any (?:ideas|advice|thoughts))\b", lowered))


def propose_preference(message: str) -> dict | None:
    """An explicit standing choice, or None. Confirmation gates persistence."""
    cleaned = " ".join(str(message or "").strip().split())
    lowered = cleaned.lower()
    if len(cleaned) < 4:
        return None
    if _is_question(cleaned, lowered):
        return None
    if len(cleaned) > _INSTRUCTION_MAX_CHARS:
        return None

    correction = _CORRECTION.match(lowered)
    if correction and len(cleaned) <= 40:
        return {"rule": f"Prefer drafts that are {correction.group(1)}.",
                "scope": "voice", "confidence": 0.62}

    for pattern, scope, template, confidence in _PATTERNS:
        match = re.search(pattern, lowered)
        if not match:
            continue
        value = match.group(1).strip(" .!?")
        if len(value) < 3 or len(value) > 180:
            return None
        if "thread" in value:
            return {"rule": "Never suggest or generate a thread.",
                    "scope": "format", "confidence": 0.99}
        if template.startswith("Avoid"):
            value = _gerund(value)
        return {"rule": template.format(value=value), "scope": scope,
                "confidence": confidence}
    return None


# "don't SAY delve" and "never USE hot take" leave a bare infinitive after the
# prohibition, and "Avoid {value}" then produced "Avoid say delve" / "Avoid use
# hot take". These are not scratch strings: they are echoed back to him for
# confirmation, stored, and read by the gates. A rule that reads like a
# transcription error is one he will not trust and will not confirm.
_BARE_VERBS = {
    "say": "saying", "use": "using", "using": "using", "write": "writing",
    "start": "starting", "ask": "asking", "add": "adding", "put": "putting",
    "make": "making", "do": "doing", "post": "posting", "mention": "mentioning",
    "open": "opening", "end": "ending", "call": "calling", "sound": "sounding",
    "be": "being", "talk": "talking", "go": "going", "include": "including",
    "reply": "replying", "quote": "quoting", "suggest": "suggesting",
}


def _gerund(value: str) -> str:
    """'say delve' -> 'saying delve'. Only the leading bare verb is touched."""
    head, _, rest = value.partition(" ")
    fixed = _BARE_VERBS.get(head.lower())
    if not fixed:
        return value
    return f"{fixed} {rest}".strip()
