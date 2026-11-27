"""A post describing self-harm is not an engagement opportunity.

FOUND 2026-08-07, while verifying that a DIFFERENT fix executed on a real path.
The standing step is "after fixing, verify the changed line runs" — it was
written to catch inert fixes and it turned up a live safety gap instead. Three
layers, no coverage:

    assess_context("i am going to kill myself")   not blocked
    assess_draft  ("you should end your life")    not blocked
    context.sensitive     suppresses MEDIA REUSE only, never drafting, and is
                          set from X's warning label, absent on plain text

Starling would have drafted an engagement reply to it.

WHY IT WAS MISSING, AND IT IS NOT NEGLIGENCE. The vocabulary is built for the
wrong grammatical person. `TRAGEDY_EXPLICIT` covers third-person news register
("died by suicide", "took their own life", "passed away"); the bare word
"suicide" is DELIBERATELY ambiguous — see the "career suicide" note — so it
needs `HUMAN_HARM_CONTEXT` to corroborate. First-person intent contains none of
that vocabulary, so it never reached the corroboration step at all. The careful
work protected the ambiguous word and left the unambiguous ones unwritten.

WHY THE EXISTING TESTS DID NOT CATCH IT. They measure PRECISION, not recall.
`test_tragedy_register.py` exists because the word list once blocked 20 of 23 of
his ordinary sentences, and `test_problem_registry`'s Q156 uses a BBC Breaking
fixture — third person again. Nothing tested under-blocking. This file is the
recall half, and the precision cases below are here so it cannot be widened
later without the register cost showing up immediately.

SCOPE, AND THE TWO PHRASES DELIBERATELY LEFT OUT. "hurt myself" and "harm
myself" are ambiguous — "I'm going to hurt myself laughing" — and the obvious
remedy, routing them through `HUMAN_HARM_CONTEXT` the way `_is_tragedy` does,
was MEASURED and cannot fire: that context is news vocabulary (victims, death
toll, coroner, paramedics, "police said"), and a person writing about their own
harm uses none of it. A branch that can never match is worse than the gap
because it reports coverage. The three unambiguous phrases carry this on their
own; closing the rest needs a corroboration signal shaped for personal crisis.
"""
from __future__ import annotations

import pytest

from app.safety import assess_context, assess_draft
from app.schemas import PostContext


def _ctx(text: str) -> PostContext:
    return PostContext(author="Someone", author_handle="someone", text=text,
                       visibility="public")


# --------------------------------------------------------------------------- #
# recall — the half nothing tested
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "i am going to kill myself",
    "im killing myself tonight",
    "i want to end my life tonight",
    "ending my life feels like the only option",
    "i might take my own life",
    # SHIPPED MISSING, 2026-08-07. `(?:ing)?` works on kill->killing and
    # end->ending and breaks on take->taking, so the -ing form was dropped from
    # that branch and the pattern looked uniform while covering less. This is
    # the commoner phrasing of the two.
    "thinking about taking my own life",
])
def test_first_person_self_harm_is_blocked(text):
    result = assess_context(_ctx(text))
    assert result.blocked, text
    assert "self_harm" in result.categories
    assert result.severity == "critical"


def test_the_reason_speaks_to_him_and_declines():
    """It renders on HIS card. It must hand the decision back, not compose.

    No crisis-line text anywhere a draft can carry — a stranger in crisis
    receiving a form letter with his name on it is worse than the gap this
    closes. Matches the register of the `human_harm` reason beside it.
    """
    result = assess_context(_ctx("i am going to kill myself"))
    reason = " ".join(result.reasons).lower()
    assert "your own words" in reason
    for forbidden in ("hotline", "helpline", "988", "samaritans", "crisis line"):
        assert forbidden not in reason, (
            f"{forbidden!r} must not appear in a field a draft can carry")


def test_the_outgoing_gate_covers_it_too():
    """Coverage and likelihood are different claims. Starling is unlikely to
    write this; the gate whose job is to stop a harmful draft leaving should
    still cover the category."""
    for text in ("you should end your life", "kill yourself", "kys"):
        result = assess_draft(text)
        assert result.blocked, text
        assert "self_harm" in result.categories


# --------------------------------------------------------------------------- #
# precision — his register must survive, and this is the half that has broken
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "the fatal flaw in this benchmark",
    "this model is a disaster",
    "shipping on a Friday is career suicide",
    "a suicidal pricing strategy",
    "this release is killing me",
    "the deploy killed my afternoon",
    "i want to die of embarrassment after that typo",
    "this bug is going to end my career",
    # `own` is mandatory on the take/taking branch and these are why. Relaxing
    # it to catch "taking my life" would take all three of these with it.
    "taking my life in a new direction",
    "taking my own sweet time on this refactor",
    "he is taking my own advice and shipping it",
])
def test_his_register_is_untouched(text):
    """`[[a-gate-word-list-eats-his-register]]`: "fatal flaw" and "let it rip"
    were once blocked as human tragedy. Every widening of this module has to
    pay this bill up front."""
    assert not assess_context(_ctx(text)).blocked, text


def test_the_second_person_argument_is_not_a_draft_block():
    """"you're only hurting yourself" is ordinary argumentative phrasing in
    exactly the register a reply lands in. It is why "hurt yourself" is not in
    the directed pattern."""
    assert not assess_draft("you are only hurting yourself").blocked


def test_the_deliberate_gap_is_recorded_not_silent():
    """These are NOT blocked, on purpose, and the reason is in the module.

    Pinned so the omission is a decision somebody can revisit rather than an
    oversight rediscovered later.
    """
    for text in ("i am going to hurt myself", "thinking about harming myself tonight"):
        assert not assess_context(_ctx(text)).blocked, (
            f"{text!r} now blocks — if that is intended, the corroboration "
            "signal it needs was built; update this test and the module note")
