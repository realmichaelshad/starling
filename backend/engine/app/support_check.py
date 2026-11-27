"""The judge that fits on any user machine: the model fetches, the harness judges.

The residual post defect — roughly the whole gap between here and the reply
surface's numbers — is a fluent sentence the evidence does not support: "It
replaces Claude Code", "Anthropic's backend is now local models". Every gate
walks past these because every entity in them is real; only the RELATIONSHIP
is invented, and a regex cannot see a relationship.

A second judge model does not fit beside the writer on a 16GB box, and the
writer asked "is this draft OK?" rubber-stamps (measured: it approved a plain
self-contradiction at temp 0). So the writer is never asked to judge. It is
asked to FETCH:

    model:    copy the one sentence from the evidence that supports this
              claim, word for word — or reply NONE
    harness:  the returned quote must be a verbatim (normalised) substring of
              the evidence, or it counts as NONE

Bluffing is structurally defeated — a fabricated quote fails the substring
check. Probed live against qwen3:8b on eleven labelled claims from real
drafts (`tools/support_check_probe.py`): 10/11 correct, zero lazy NONEs on
supported claims, ~10s per claim warm. The one miss was a sweeping claim
quoting a true-but-weaker sentence — the class `sweeping_claim` already gates
upstream, so the mechanisms compose.

What happens on an unsupported claim follows the registry's NEVER1 rule and
his measured style, never silence:

  - an unsupported TRAILING sentence is CUT. The dominant residual shape is a
    grounded first sentence with an invented second; cutting the second turns
    it into the one-sentence post that is also his best-measured shape (4.45x
    engagement by his own numbers).
  - an unsupported LEAD (or only) sentence ships FLAGGED, with the claim named,
    so he can judge it in five seconds.

Bounded and loud: a hard time budget, and a skipped check writes a safety
event — a silent gap in verification is how "some posts got checked and some
did not, with no way to tell which" happens, and that was the reason the 12B
judge was left unwired.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .text_normalizer import split_sentences

# The whole check may not cost more than this on top of a draft. On the API
# model a claim check is ~0.6 s, so two claims fit easily (S5, 2026-09-21; this
# was 30 s at ~10 s per claim on a local model) — and on a slow provider the
# right behaviour is to skip loudly, not to stall the composer.
_BUDGET_S = 5.0
_MIN_EVIDENCE_CHARS = 60
# TWO CLAIMS AGAIN — the reason for cutting it to one is gone.
#
# It was reduced to 1 on 2026-07-28 because a single create was hitting
# 250-300s on a loaded machine. That was never the check's fault: the engine
# serializes requests and CHAT_TIMEOUT_S was 120s, so calls were dying in the
# queue. That root cause was fixed at the transport layer the same day
# (providers.CHAT_TIMEOUT_S 120 → 300), and this mitigation stayed behind and
# quietly halved the verification.
#
# What it cost, live on 2026-07-30: "Together AI says they serve Kimi
# natively but Moonshot just released it. It's a partnership that smells
# like copying instead of building something new together." The evidence says
# the two companies PARTNERED; the draft accuses them of copying. That
# inversion sits in the MIDDLE sentence, and with one claim checked only the
# last sentence was ever looked at.
_MAX_CLAIMS = 2

# A sentence is CHECKABLE when it asserts something about a named thing and is
# not marked as opinion. Opinion is his voice ("I'd take Fable 5 over every
# GPT model") and must never be sent to a support check that would call it
# unsupported — it is not a claim about the evidence.
_OPINION_RE = re.compile(
    r"\b(?:i think|i'?d|i bet|i hope|i want|my prediction|maybe|probably|"
    r"perhaps|feels? like|seems? like|might|could be|honestly|imo|"
    r"let'?s hope|no need to)\b", re.IGNORECASE)
_QUESTION_RE = re.compile(r"\?\s*$")


def _checkable(sentence: str) -> bool:
    body = (sentence or "").strip()
    if len(body.split()) < 4 or _QUESTION_RE.search(body):
        return False
    if _OPINION_RE.search(body):
        return False
    from .subject import subjects
    if subjects(body):
        return True
    return bool(re.search(r"\d", body))


def checkable(sentence: str) -> bool:
    """The same test, for the claim check that decides a web search (W4, radar)."""
    return _checkable(sentence)


def _normalise(text: str) -> str:
    text = (text or "").lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^a-z0-9$%.' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_PROMPT = (
    "Evidence:\n{evidence}\n\n"
    'Claim: "{claim}"\n\n'
    "Copy the ONE sentence from the evidence above that supports the claim, "
    "word for word. If no sentence in the evidence supports the claim, reply "
    "with exactly: NONE"
)


def _quote_supports(reply: str, evidence: str) -> bool:
    body = (reply or "").strip().strip('"')
    if not body or body.upper().startswith("NONE"):
        return False
    quote = _normalise(body)
    return len(quote) >= 20 and quote in _normalise(evidence)


# Singular assertive verbs only, mapped to their attribution form. Plural
# subjects ("open models outperform...") keep the note-only path — rewriting
# them needs verb agreement this deliberately does not attempt.
_HEDGE_MAP = {
    "outperforms": "claims to outperform",
    "beats": "claims to beat",
    "leads": "claims to lead",
    "tops": "claims to top",
}
_HEDGE_RE = re.compile(r"\b(outperforms|beats|leads|tops)\b")


def _hedge_assertion(claim: str) -> str | None:
    """The claim with its first assertive verb turned into an attribution,
    or None when there is nothing to hedge or it is already hedged."""
    if re.search(r"\bclaims?\s+to\b|\bsays\b|\breportedly\b", claim,
                 re.IGNORECASE):
        return None
    match = _HEDGE_RE.search(claim)
    if not match:
        return None
    return claim[:match.start()] + _HEDGE_MAP[match.group(1)] + claim[match.end():]


class _CheckUnavailable(Exception):
    """The verifier could not run. NOT the same as 'the claim is unsupported'."""


def _find_support(claim: str, evidence: str, router: Any) -> bool:
    """True = supported. The HARNESS decides; the model only fetches.

    A CALL THAT NEVER HAPPENED IS NOT A VERDICT. `ProviderRouter.chat`
    returns None — without raising — when the deadline has expired, when no
    model is configured for the lane, and when the transport fails. That None
    used to flow into `_quote_supports(str(raw or ""))`, which is False,
    which this module reads as "the evidence does not support this claim".
    So a model that was merely UNREACHABLE cut the last sentence off every
    post and quote, or shipped them carrying `Heads up: I can't back "…"` —
    and the user reads that as Starling judging the draft rather than
    Starling failing to reach a model. Same silent-empty family as the rest:
    an absent answer must never be read as a negative one.
    """
    raw = router.chat(
        [{"role": "system",
          "content": "You verify quotes. Reply with a verbatim sentence from "
                     "the evidence, or NONE. Nothing else."},
         {"role": "user",
          "content": _PROMPT.format(evidence=evidence[:2000], claim=claim[:300])}],
        # S9 (2026-09-22): copying a sentence out of the evidence is extraction, not
        # writing, so it runs on the fast lane; the harness judges the quote anyway.
        options={"temperature": 0.0, "num_predict": 80, "timeout_s": 8.0}, think=False,
        lane="fast")
    if raw is None:
        raise _CheckUnavailable("the verifier returned no answer at all")
    return _quote_supports(str(raw), evidence)


_MANY_PROMPT = (
    "Evidence:\n{evidence}\n\n"
    "Claims:\n{claims}\n\n"
    "For each claim, copy the ONE sentence from the evidence above that supports it, "
    "word for word, on its own line as \"<number>: <sentence>\". If no sentence in the "
    "evidence supports a claim, write \"<number>: NONE\" for it. Nothing else."
)
_NUMBERED_RE = re.compile(r"^\s*(?:claim\s*)?(\d{1,2})\s*[:.)\-]\s*(.*)$", re.IGNORECASE)


def _find_support_many(claims: list[str], evidence: str, router: Any) -> list[bool | None]:
    """One verdict per claim from ONE call (S6, 2026-09-22): True, False, or None.

    Two claims used to cost two calls in sequence. Now they are asked together and
    the harness still judges each quote against the evidence on its own. None means
    the answer named no line for that claim: a check that did not happen, never a
    negative verdict (the same rule as `_find_support`).
    """
    if len(claims) == 1:
        return [_find_support(claims[0], evidence, router)]
    numbered = "\n".join(f'{index}. "{claim[:300]}"' for index, claim in enumerate(claims, start=1))
    raw = router.chat(
        [{"role": "system",
          "content": "You verify quotes. For each numbered claim reply with a verbatim "
                     "sentence from the evidence, or NONE. Nothing else."},
         {"role": "user", "content": _MANY_PROMPT.format(evidence=evidence[:2000], claims=numbered)}],
        options={"temperature": 0.0, "num_predict": 80 * len(claims), "timeout_s": 8.0}, think=False,
        lane="fast")
    if raw is None:
        raise _CheckUnavailable("the verifier returned no answer at all")
    answer = str(raw).strip()
    if answer.strip(' ."').upper() == "NONE":
        return [False] * len(claims)          # one explicit NONE for everything
    lines: dict[int, str] = {}
    for line in answer.splitlines():
        match = _NUMBERED_RE.match(line)
        if match and int(match.group(1)) not in lines:
            lines[int(match.group(1))] = match.group(2).strip()
    verdicts: list[bool | None] = []
    for index, claim in enumerate(claims, start=1):
        if index in lines:
            verdicts.append(_quote_supports(lines[index], evidence))
            continue
        # The model skipped the format for this claim. Ask about it alone, the old
        # way, rather than let a claim through unchecked (a bluffing unnumbered
        # quote must still fail the substring check).
        try:
            verdicts.append(_find_support(claim, evidence, router))
        except _CheckUnavailable:
            verdicts.append(None)
    return verdicts


def apply_support_check(text: str, evidence: str, llm: Any) -> tuple[str, str | None]:
    """(possibly-shortened text, note-or-None). Never raises, never silent.

    Checks up to the last two checkable sentences of the draft against the
    evidence. An unsupported trailing sentence is cut; an unsupported lead is
    named in the returned note. On any failure or budget overrun the draft is
    returned untouched and a safety event records that the check was skipped.
    """
    body = (text or "").strip()
    evidence = (evidence or "").strip()
    # THESE TWO RETURNED IN COMPLETE SILENCE, which contradicts the rule
    # `_record_skip` exists to enforce one screen below: "a verification that
    # silently does not run is worse than none at all." Only the budget and
    # verifier-down paths were recorded, so the two commonest reasons for this
    # check never running were the two nobody could see. On his machine the
    # safety log held six support_check rows, all budget — and no way to tell
    # whether that was six presses or six hundred.
    if not body:
        return text, None
    if len(evidence) < _MIN_EVIDENCE_CHARS:
        _record_skip(body, "thin_evidence")
        return text, None
    router = getattr(llm, "router", None)
    if router is None or not hasattr(router, "chat"):
        _record_skip(body, "no_router")
        return text, None

    started = time.monotonic()
    try:
        sentences = [s.strip() for s in split_sentences(body) if s.strip()]
        targets = [s for s in sentences if _checkable(s)][-_MAX_CLAIMS:]
        ordered = list(reversed(targets))            # the last claim is judged first
        if not ordered:
            return text, None
        if time.monotonic() - started > _BUDGET_S:
            _record_skip(body, "budget")
            return text, None
        try:
            verdicts = _find_support_many(ordered, evidence, router)
        except _CheckUnavailable:
            # The verifier is down. Return the draft untouched and say so
            # in the safety log — never cut a sentence or accuse a claim
            # on the strength of a call that did not happen.
            _record_skip(body, "verifier_unreachable")
            return text, None
        for claim, supported in zip(ordered, verdicts):
            if supported is None:
                # The answer named no line for this claim: unchecked, not unsupported.
                _record_skip(body, "claim_unanswered")
                continue
            if supported:
                continue
            if len(sentences) >= 2 and claim == sentences[-1]:
                # The grounded-lead + invented-tail shape: cut the tail.
                cut_at = body.rfind(claim)
                if cut_at > 0:
                    _record_caught(claim, "cut_tail")
                    return body[:cut_at].rstrip(), None
            hedged = _hedge_assertion(claim)
            if hedged:
                # "MAI-Cyber-1-Flash outperforms competitors" shipped flagged
                # on 2026-07-27 — the note named the problem and the text
                # still asserted it. The writer's own good form from the same
                # evening ("claims to outperform") is a deterministic edit,
                # so make it: the assertion becomes an attribution and the
                # note tells him which claim to check.
                _record_caught(claim, "hedged")
                return body.replace(claim, hedged, 1), (
                    f'Heads up: the evidence I have doesn\'t verify '
                    f'"{claim[:90]}", so I wrote it as a claim. '
                    f"Check it before posting.")
            # AN UNBACKABLE LEAD WITH A REAL BODY LOSES THE LEAD — the mirror
            # of the tail cut above, added 2026-07-28 after two drafts shipped
            # asserting flagged leads the hedge map has no verb for ("SOTA
            # BEAM ... MEANS small models finally matter"). What remains was
            # always the supported part; deleting the detected sentence is
            # this codebase's founding rule, and it beats a note beside an
            # assertion every time.
            if len(sentences) >= 2 and claim != sentences[-1]:
                remainder = (body[:body.find(claim)] +
                             body[body.find(claim) + len(claim):]).strip()
                # ...unless the lead was carrying all the names. The first
                # arena run (2026-07-28) cut "Mistral OCR4 trails DharmaOCR
                # by thirteen points" and shipped the naked tail "The gap is
                # unchanged across three test sets." — nameless, pointless.
                # A cut that beheads the draft keeps the note path instead.
                from .intelligence import names_nothing
                if len(remainder) >= 40 and not names_nothing(remainder):
                    _record_caught(claim, "cut_lead")
                    return remainder, None
            _record_caught(claim, "flagged")
            return text, (f'Heads up: I can\'t back "{claim[:120]}" from the '
                          f"evidence I have. Check it before posting.")
        return text, None
    except Exception:                                    # noqa: BLE001
        _record_skip(body, "error")
        return text, None


def _record_skip(text: str, reason: str) -> None:
    """A verification that silently does not run is worse than none at all."""
    try:
        from .storage import save_safety_event
        save_safety_event("support_check", "low", False,
                          [f"support_check_skipped:{reason}"],
                          {"text": text[:160]})
    except Exception:                                    # noqa: BLE001
        pass


def _record_caught(claim: str, outcome: str) -> None:
    """And a verification that RUNS also has to be visible.

    Skips were recorded and catches were not, so the safety log could answer
    "did this check decline to run" and could not answer "is this check doing
    anything" — which is the question that decides whether the whole mechanism
    earns its ~10s per claim. On his machine the only support_check rows were
    six skips; a fabrication caught and cut left no trace at all.

    Recorded as a real event rather than a metric because the claim itself is
    the useful part: what the model asserted, what the harness could not back,
    and which repair was applied.
    """
    try:
        from .storage import save_safety_event
        save_safety_event("support_check", "medium", False,
                          [f"support_check_caught:{outcome}"],
                          {"claim": claim[:200]})
    except Exception:                                    # noqa: BLE001
        pass
