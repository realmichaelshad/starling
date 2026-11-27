"""The mangled-name check: unknown everywhere, one slip from the source.

The live bug (2026-07-30): evidence said "Goose does the same thing for
free", the shipped draft said "Goats is free for the same work today".
Calibrated before wiring: on all 1,499 historical post/quote candidates the
check flags exactly 1 — that draft — and 0 of his 43 archived posts even
against an empty allowed-corpus (structural exemption: every name he uses is
in account_core_tokens).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.name_check import mangled_names                       # noqa: E402

EVIDENCE = ("Claude Code costs up to $200 a month. Goose does the same "
            "thing for free. Published today on VentureBeat AI.")


def test_the_live_goats_draft_is_caught():
    draft = "Claude Code runs to two hundred dollars. Goats is free for the same work today."
    found = mangled_names(draft, EVIDENCE)
    assert found and found[0][0] == "Goats" and found[0][1] == "goose"


def test_known_entities_are_never_this_defect():
    """An absent-but-known name (Fable under a Midjourney story) is the
    injected-entity defect, not a garble — flagging it here would rebuild
    the wide net that killed 7 of 30 good candidates."""
    assert mangled_names("Fable can't match this image quality.",
                         "Midjourney Releasing v8.2 today.") == []
    # A spelling variant of a known name can't fire either.
    assert mangled_names("Midjourney v8.2 focuses on aesthetics.",
                         "Midjourney Releasing v8.2.") == []


def test_versioned_and_possessive_forms_are_allowed():
    assert mangled_names("GPT-5 gets an 80 percent cut.",
                         "OpenAI cuts GPT-5.6 pricing.") == []
    assert mangled_names("NotebookLM's folders arrive.",
                         "NotebookLM adds folders.") == []


def test_instruction_prose_never_supplies_a_twin():
    """The direction's own capitalised verbs ("Write one original standalone
    post...") twinned ordinary sentence-openers in calibration — Stop/shape,
    Worth/write, Which/write. All killed by the first-two-letters rule."""
    direction = ("Write one original standalone post for this account today. "
                 "Ground it in what is rising. Shape it as one point.")
    for opener in ("Stop paying twice for the same tool.",
                   "Worth watching where this lands.",
                   "Which is the whole story."):
        assert mangled_names(opener, direction) == [], opener


def test_shouts_and_acronyms_are_not_this_defect():
    assert mangled_names("WRONG: this echoes the headline.",
                         "Write one post about pricing.") == []
    assert mangled_names("INT8 quantization lands.",
                         "Nunchaku brings INT4 inference to Diffusers.") == []


def test_the_issue_ships_as_a_note_never_a_rejection():
    """NEVER1's law applied to this gate: the issue has a weakness note, is
    not integrity, and _draft_quality_issues raises it end to end."""
    from app.intelligence import (_INTEGRITY_ISSUES, _WEAKNESS_NOTES,
                                  _draft_quality_issues)
    from app.schemas import DraftRequest, PostContext
    assert "mangled_name" in _WEAKNESS_NOTES
    assert _WEAKNESS_NOTES["mangled_name"].startswith("Heads up:")
    assert "mangled_name" not in _INTEGRITY_ISSUES
    request = DraftRequest(
        action="post",
        context=PostContext(author="a", text="", visibility="public"))
    request.direction = (
        "Write one original standalone post for this account today. "
        "Ground it in what is rising RIGHT NOW: topic \"Claude Code costs "
        "up to $200 a month. Goose does the same thing for free.\" "
        "The evidence is background: Goose is an open-source agent that "
        "does the same work at no cost.")
    issues = _draft_quality_issues(
        "Claude Code runs to two hundred dollars. Goats is free for the "
        "same work today.", request, [])
    assert "mangled_name" in issues
