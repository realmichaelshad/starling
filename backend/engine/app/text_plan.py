"""The Writer's plan and its editor (spec §8.3f-§8.3h).

Two ideas the previous Writer did not have.

**Plan before wording (§8.3f).** The Writer receives a decided strategy and
turns it into a compact plan — READER, POINT, PROOF, PAYOFF, PRIMARY ACTION,
FORMAT, VOICE, DO NOT — before choosing a single word. If any of the first five
is blank, it does NOT compensate with a louder hook; it returns the missing
requirement upstream. That is the difference between a writer and a hook
generator.

**Edit by job, not by feel (§8.3h).** Every sentence must do one of six jobs.
A sentence that only repeats the claim, manufactures urgency, announces its own
importance or asks for engagement is removed. Then seven hard checks run; a
failure gets ONE revision, and if it still fails the draft is returned
`below-bar` naming the missing ingredient rather than dressed up as
"X optimised".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import algorithm_model as am

# The five fields a plan cannot be missing (§8.3f).
REQUIRED_FIELDS = ("reader", "point", "proof", "payoff", "primary_action")

# §8.3h: the six jobs. A sentence earns its place by doing at least one.
SENTENCE_JOBS = ("orient", "claim", "proof", "consequence", "qualify", "action")

# §8.3h hard checks.
HARD_CHECKS = ("immediate_comprehension", "information_gain", "truth_and_attribution",
               "native_payoff", "voice_integrity", "action_integrity", "x_presentation")

BANDS = ("post-ready", "strong", "workable", "below-bar")

_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?", re.MULTILINE)
_LINK_RE = re.compile(r"https?://\S+")
_HASHTAG_RE = re.compile(r"(?:^|\s)#\w+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")

# Sentences that exist only to announce importance or beg (§8.3h).
_FILLER_PATTERNS = (
    "this changes everything", "nobody is talking about", "let that sink in",
    "read that again", "this is huge", "game changer", "mind blown",
    "thread below", "a thread", "buckle up", "you need to see this",
)
_BEGGING = ("like if", "retweet if", "rt if", "follow for", "follow me",
            "share this", "tag someone", "comment below", "drop a")
_URGENCY = ("act now", "before it's too late", "don't miss", "last chance",
            "everyone is", "right now everyone")
_UNSUPPORTED = ("guaranteed", "will definitely", "always works", "never fails",
                "proven to", "100% of")

_STOP = {"the", "and", "for", "that", "this", "with", "you", "your", "are",
         "was", "have", "has", "but", "not", "they", "them", "from", "what",
         "when", "who", "why", "how", "all", "can", "will", "just", "about"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.findall(text or "") if s.strip()]


@dataclass
class TextPlan:
    """§8.3f. Built from the Decider's output, never invented by the Writer."""

    reader: str | None = None
    point: str | None = None
    proof: str | None = None
    payoff: str | None = None
    primary_action: str | None = None
    fmt: str = "post"
    voice: dict[str, Any] = field(default_factory=dict)
    do_not: list[str] = field(default_factory=list)
    archetype: str | None = None
    length_range: tuple[int, int] | None = None

    @property
    def missing(self) -> list[str]:
        return [name for name in REQUIRED_FIELDS if not getattr(self, name)]

    @property
    def complete(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict[str, Any]:
        return {"reader": self.reader, "point": self.point, "proof": self.proof,
                "payoff": self.payoff, "primary_action": self.primary_action,
                "format": self.fmt, "voice": self.voice, "do_not": self.do_not,
                "archetype": self.archetype, "length_range": self.length_range,
                "missing": self.missing}

    def as_prompt_block(self) -> str:
        """The plan as the Writer sees it — explicit, so it cannot drift."""
        lines = [
            f"READER: {self.reader}",
            f"POINT: {self.point}",
            f"PROOF: {self.proof}",
            f"PAYOFF: {self.payoff}",
            f"PRIMARY ACTION: {self.primary_action}",
            f"FORMAT: {self.fmt}",
        ]
        if self.voice:
            lines.append(f"VOICE: {self.voice}")
        if self.do_not:
            lines.append("DO NOT: " + "; ".join(self.do_not))
        return "\n".join(lines)


def build_plan(decision, analysis, *, voice: dict[str, Any] | None = None,
               length_range: tuple[int, int] | None = None) -> TextPlan:
    """Turn a decision plus its evidence into a plan. Invents nothing.

    A field stays None when the evidence does not supply it, which is what
    makes `missing` meaningful — filling gaps with plausible text is exactly
    how a writer ends up compensating with a louder hook.
    """
    forecast = getattr(analysis, "audience_forecast", {}) or {}
    angles = getattr(analysis, "angles", []) or []
    lead = angles[0] if angles else {}

    do_not = list(am.PROHIBITED_TACTICS[:4])
    do_not.extend(getattr(analysis, "penalties", []) or [])
    if getattr(decision, "risks", None):
        do_not.extend(str(risk) for risk in decision.risks[:2])

    return TextPlan(
        reader=forecast.get("core") or None,
        point=lead.get("why") or None,
        # Proof is only claimed when the evidence actually names one.
        proof=(getattr(analysis, "summary", None) if lead else None),
        payoff=forecast.get("conversion_bridge") or None,
        primary_action=getattr(decision, "primary_objective", None) or
        forecast.get("primary_action"),
        fmt=getattr(decision, "action", "post"),
        voice=voice or {},
        do_not=do_not,
        archetype=lead.get("kind"),
        length_range=length_range,
    )


# --------------------------------------------------------------------------- #
# §8.3g format programs — as validation, not only as prompt text
# --------------------------------------------------------------------------- #

def check_format(text: str, fmt: str, *, parent_text: str = "") -> list[str]:
    """Structural failures for the chosen format. Empty means it conforms."""
    problems: list[str] = []
    sentences = _sentences(text)
    if not sentences:
        return ["empty draft"]
    first = sentences[0]

    if fmt == "reply":
        # §8.3g: name the exact claim, not merely the author or topic.
        if parent_text:
            if _tokens(first) & _tokens(parent_text):
                pass
            else:
                problems.append("the reply does not connect to the parent claim")
        if text.strip().startswith("@") and len(sentences) == 1 and len(text) < 40:
            problems.append("the reply is a bare mention with no contribution")
    elif fmt == "quote":
        # Must stand alone if the embed is never opened.
        if re.match(r"^\s*(this|that|it)\b", first, re.IGNORECASE):
            problems.append("the first line depends on the quoted post to make sense")
    else:  # standalone post
        if _LINK_RE.match(text.strip()):
            problems.append("the post leads with a link instead of the takeaway")
        if len(_LINK_RE.sub("", text).strip()) < 40 and _LINK_RE.search(text):
            problems.append("the payoff is hidden behind the link")
    return problems


# --------------------------------------------------------------------------- #
# §8.3h sentence-level editor
# --------------------------------------------------------------------------- #

def classify_sentence(sentence: str, *, index: int, total: int,
                      claim_tokens: set[str]) -> tuple[list[str], str | None]:
    """Which of the six jobs a sentence does, and why it might be cut."""
    lowered = sentence.lower()
    jobs: list[str] = []

    if any(phrase in lowered for phrase in _BEGGING):
        return [], "asks for engagement"
    if any(phrase in lowered for phrase in _FILLER_PATTERNS):
        return [], "announces importance instead of showing it"
    if any(phrase in lowered for phrase in _URGENCY):
        return [], "manufactures urgency"

    if index == 0:
        jobs.append("orient")
    tokens = _tokens(sentence)
    if any(char.isdigit() for char in sentence) or " because " in lowered:
        jobs.append("proof")
    if any(word in lowered for word in ("so ", "which means", "therefore",
                                        "that means", "the result")):
        jobs.append("consequence")
    if any(word in lowered for word in ("but ", "unless", "only if", "assuming",
                                        "roughly", "about ", "may ", "might ")):
        jobs.append("qualify")
    if sentence.rstrip().endswith("?") and len(sentence) > 40:
        jobs.append("action")
    if tokens - claim_tokens:
        jobs.append("claim")

    if not jobs:
        # Nothing new and no job: it is restating the claim.
        return [], "repeats the claim without adding a job"
    return jobs, None


def edit_sentences(text: str) -> dict[str, Any]:
    """Keep only sentences that do a job; report what was cut and why."""
    sentences = _sentences(text)
    if not sentences:
        return {"text": "", "kept": [], "removed": [], "changed": False}

    claim_tokens: set[str] = set()
    kept: list[str] = []
    removed: list[dict[str, str]] = []
    for index, sentence in enumerate(sentences):
        jobs, reason = classify_sentence(
            sentence, index=index, total=len(sentences), claim_tokens=claim_tokens)
        if reason:
            removed.append({"sentence": sentence, "reason": reason})
            continue
        kept.append(sentence)
        claim_tokens |= _tokens(sentence)

    # Never edit a draft down to nothing: if every sentence looked like filler,
    # that is a judgement to report, not a licence to return an empty post.
    if not kept:
        return {"text": text.strip(), "kept": sentences, "removed": [],
                "changed": False,
                "note": "Every sentence looked like filler; returned unchanged for review."}

    edited = " ".join(kept)
    return {"text": edited, "kept": kept, "removed": removed,
            "changed": edited.strip() != text.strip()}


def hard_checks(text: str, plan: TextPlan, *, parent_text: str = "",
                voice_examples: list[str] | None = None) -> dict[str, Any]:
    """The seven §8.3h checks. Each returns pass/fail plus the missing thing."""
    results: dict[str, dict[str, Any]] = {}
    sentences = _sentences(text)
    lowered = text.lower()

    first = sentences[0] if sentences else ""
    results["immediate_comprehension"] = {
        "ok": len(first) >= 25 and not re.match(r"^\s*(this|that|it)\b", first, re.I),
        "missing": "a first line a new reader can understand without the source",
    }
    body_tokens = _tokens(text)
    results["information_gain"] = {
        "ok": len(body_tokens - _tokens(parent_text)) >= 4,
        "missing": "an observation, proof, distinction or decision aid the parent lacks",
    }
    results["truth_and_attribution"] = {
        "ok": not any(phrase in lowered for phrase in _UNSUPPORTED),
        "missing": "qualified language instead of unsupported certainty",
    }
    without_links = _LINK_RE.sub("", text).strip()
    results["native_payoff"] = {
        "ok": len(without_links) >= 40,
        "missing": "value the reader gets without clicking anything",
    }
    results["voice_integrity"] = {
        # Only checkable when examples exist; unknown is not a failure.
        "ok": True if not voice_examples else any(
            _tokens(example) & body_tokens for example in voice_examples),
        "missing": "wording that resembles the writer's demonstrated style",
    }
    alignment, notes = am.algorithm_alignment(text, plan.fmt)
    results["action_integrity"] = {
        "ok": alignment >= 0.7,
        "missing": "; ".join(notes) or "a natural action without bait",
    }
    letters = [c for c in text if c.isalpha()]
    shouting = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.6
    results["x_presentation"] = {
        "ok": not _HASHTAG_RE.search(text) and not shouting,
        "missing": "concise, conversational copy without hashtags or all caps",
    }

    failed = [name for name, value in results.items() if not value["ok"]]
    return {"results": results, "failed": failed, "passed": not failed}


def band_for(text: str, plan: TextPlan, *, parent_text: str = "",
             voice_examples: list[str] | None = None) -> dict[str, Any]:
    """A quality label plus, on failure, the specific missing ingredient.

    §8.3h: a draft that still fails after one revision is returned `below-bar`
    naming what it lacks, rather than disguised as optimised.
    """
    fmt_problems = check_format(text, plan.fmt, parent_text=parent_text)
    checks = hard_checks(text, plan, parent_text=parent_text,
                         voice_examples=voice_examples)
    failed = list(checks["failed"]) + fmt_problems

    if failed:
        band = "below-bar"
    elif plan.complete:
        band = "post-ready"
    else:
        band = "workable"
    return {
        "band": band,
        "failed": failed,
        "missing_ingredient": (checks["results"].get(failed[0], {}).get("missing")
                               if failed and failed[0] in checks["results"]
                               else (failed[0] if failed else None)),
        "checks": checks["results"],
        "format_problems": fmt_problems,
        "plan_missing": plan.missing,
    }
