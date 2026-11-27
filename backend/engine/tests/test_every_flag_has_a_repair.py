"""A gate that fires and cannot be repaired is a complaint, not a check.

Both defects in the 2026-07-30 arena run were `names_nothing`, and the product
was NOT blind to either — the gate fired on both. It had been deliberately
moved out of the hard-reject path so a vague draft ships with its fault named
instead of as silence, and then nobody added it to the revisable set. So it
flagged, and shipped, and never got a second attempt at the one repair that
would have worked: put the name from the evidence into the sentence.

Two invariants keep that from happening again:

  * every revisable issue reaches the reviser with an INSTRUCTION, not a bare
    token — their own note says "a bare issue name is not an instruction";
  * every issue the gates can raise has human-readable text for the card.
"""
import re

import app.intelligence as I
from app.llm import ModelClient


def test_every_revisable_issue_tells_the_model_what_to_do():
    missing = sorted(I._CLARITY_ISSUES - set(ModelClient.CLARITY_GUIDANCE))
    assert not missing, (
        f"these issues trigger a revise pass with no instruction attached, "
        f"so the rewrite is a coin flip: {missing}")


def test_naming_nothing_is_repairable():
    """The specific hole that shipped both arena defects."""
    assert "names_nothing" in I._CLARITY_ISSUES, (
        "names_nothing flags the draft and offers no way to fix it")
    assert "names_nothing" in ModelClient.CLARITY_GUIDANCE


def test_guidance_is_an_instruction_not_a_label():
    for key, text in ModelClient.CLARITY_GUIDANCE.items():
        assert len(text.split()) >= 8, f"{key}: too short to act on — {text!r}"
        assert text.strip().lstrip('"')[0].isupper(), (
            f"{key}: not written as a sentence")


def test_every_shippable_issue_names_its_own_fault_on_the_card():
    """No draft ships under the generic shrug when the gate knew the reason.

    The near-miss path does `_WEAKNESS_NOTES.get(issues[0], "It is a bit
    weak…")`, so a missing entry is not a crash — it is a draft that reaches
    him with the fault hidden behind a shrug, which is what this registry
    exists to prevent. Integrity issues are excluded: those are hard rejects
    and never become `issues[0]` on a shipped card.
    """
    source = (I.__file__ and open(I.__file__, encoding="utf-8").read()) or ""
    raised = set(re.findall(r'issues\.append\("([a-z_]+)"\)', source))
    unknown = sorted(raised - set(I._INTEGRITY_ISSUES) - set(I._WEAKNESS_NOTES))
    assert not unknown, (
        f"these ship under the generic shrug instead of their real "
        f"reason: {unknown}")
