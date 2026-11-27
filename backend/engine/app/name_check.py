"""A name the draft mangled: unknown everywhere, one slip away from the source.

Live, 2026-07-30: the evidence said "Claude Code costs up to $200 a month.
Goose does the same thing for free." and the draft shipped "Claude Code runs
to two hundred dollars. Goats is free for the same work today." — a false
statement about a product's name, in his voice. Neither existing net could
see it: `_injected_entities` is deliberately alias-only (the wide
any-capitalised-name version was tried and killed 7 of 30 good candidates on
spelling variants), and `_unsupported_claims` watches figures, which "two
hundred" spelled out also slipped. The gap between the two is exactly a
CORRUPTED name: a token that is no entity we know, appears nowhere in the
material the model was handed — and sits one small edit from a name that IS
in that material.

Requiring the mangled TWIN is what makes this safe where the wide net was not:

  * a known entity is never flagged here — anything in the alias list, the
    known-tools list, or his own archive is exempt before the twin test, so
    a spelling variant of a known name cannot fire, and an absent-but-known
    comparison stays `_injected_entities`' jurisdiction;
  * an ordinary capitalised sentence-opener ("Everyone", "Shipping") has no
    twin among the material's names, so it cannot fire;
  * his own writing is exempt STRUCTURALLY, not statistically: every name he
    uses is in `account_core_tokens`, so the archive cannot false-positive
    by construction. A test still proves it against the live DB.

The verdict is a weakness note, not a rejection: a wrong name is a
five-second fix once it is pointed at, and a false positive here must cost a
"check the name" line, never a deleted draft.
"""
from __future__ import annotations

import re

_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9.\-]{2,}\b")

# Twin tolerances, CALIBRATED on all 1,499 historical post/quote candidates
# (2026-07-30). The raw shape — any capitalised token, first letter shared,
# edits <= 3 — flagged 13, of which 12 were false: ALL-CAPS meta words
# ("WRONG", "INT8", "GPT-5") and ordinary sentence-openers twinned against
# the DIRECTION'S OWN PROSE ("Stop"->"shape", "Worth"->"write" — the
# instruction text is full of capitalised verbs). Two tightenings kill all
# twelve and keep the real one:
#   * the draft token must be Titlecase — a garbled product name is written
#     like a name; shouts and acronyms are not this defect;
#   * the twin must share the first TWO letters — "Goats"/"goose" agree on
#     "go", while stop/shape, worth/write and which/write all diverge.
# "Goats"/"Goose" is substitutions a->o, t->s, s->e = 3, so the edit cap
# sits exactly there. Result on the corpus: 1 flag in 1,499, and it is the
# live bug.
_MAX_EDITS = 3
_MAX_LEN_DIFF = 2
_MIN_TOKEN_LEN = 4
_TITLECASE_RE = re.compile(r"^[A-Z][a-z]")
_VERSION_TAIL_RE = re.compile(r"[-.]?\d+(?:\.\d+)*$")


def _edits(a: str, b: str, cap: int = _MAX_EDITS + 1) -> int:
    """Levenshtein with an early exit past `cap` — tokens here are short."""
    if abs(len(a) - len(b)) >= cap:
        return cap
    previous = list(range(len(b) + 1))
    for i, ch_a in enumerate(a, 1):
        current = [i]
        best = i
        for j, ch_b in enumerate(b, 1):
            cost = min(previous[j] + 1, current[j - 1] + 1,
                       previous[j - 1] + (ch_a != ch_b))
            current.append(cost)
            best = min(best, cost)
        if best >= cap:
            return cap
        previous = current
    return previous[-1]


def _known_stems() -> set[str]:
    from .subject import _KNOWN_TOOLS, _SUBJECT_ALIASES, account_core_tokens
    known = set(_SUBJECT_ALIASES) | set(_SUBJECT_ALIASES.values()) | set(_KNOWN_TOOLS)
    try:
        known |= account_core_tokens()
    except Exception:
        pass
    return known


def _stop_stems() -> frozenset[str]:
    from .subject import _DOMAIN_TERMS, _DOMAIN_VERBS, _NOT_A_NAME
    return frozenset(_NOT_A_NAME | _DOMAIN_TERMS | _DOMAIN_VERBS)


def _appears(stem: str, haystack: str) -> bool:
    """Present in the material, tolerant of possessives and bare plurals."""
    forms = {stem}
    if stem.endswith("s") and len(stem) > _MIN_TOKEN_LEN:
        forms.add(stem[:-1])
    return any(re.search(rf"\b{re.escape(form)}", haystack) for form in forms)


def mangled_names(text: str, allowed: str) -> list[tuple[str, str]]:
    """(bad, twin) pairs: names the draft corrupted from the material.

    `allowed` is everything the model was legitimately handed — direction
    (topic + evidence), the visible conversation, the voice examples.
    """
    allowed_l = (allowed or "").lower()
    stops = _stop_stems()
    known = _known_stems()

    twins: list[str] = []
    for token in _NAME_RE.findall(allowed or ""):
        stem = token.lower().strip(".-")
        if len(stem) >= 3 and stem not in stops:
            twins.append(stem)

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in _NAME_RE.findall(text or ""):
        if not _TITLECASE_RE.match(token):
            continue                # shouts and acronyms are not this defect
        stem = token.lower().strip(".-")
        unversioned = _VERSION_TAIL_RE.sub("", stem)
        if len(stem) < _MIN_TOKEN_LEN or stem in seen:
            continue
        if {stem, unversioned} & stops or {stem, unversioned} & known:
            continue
        if _appears(stem, allowed_l) or (
                unversioned != stem and _appears(unversioned, allowed_l)):
            continue
        for twin in twins:
            if twin == stem or twin[:2] != stem[:2]:
                continue
            if abs(len(twin) - len(stem)) > _MAX_LEN_DIFF:
                continue
            if _edits(stem, twin) <= _MAX_EDITS:
                found.append((token, twin))
                seen.add(stem)
                break
    return found
