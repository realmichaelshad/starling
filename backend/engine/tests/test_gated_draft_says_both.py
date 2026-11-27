"""A gated draft names its weakness AND says what the draft did.

REPORTED 2026-08-07: the enhance flow needed to be better, because it told the
user to review the draft themselves.

Reproduced by driving /browser/assist three times with fresh drafts (fresh
because the repeat gate is fed by every draft ever made, so re-sending the same
text manufactures a duplicate and measures nothing):

  passed its gates -> "Preserved the user's exact point and fact while fixing
                       flow, removing unnecessary words..."
  tripped a gate   -> "Heads up: this says very little. Worth a rewrite."
  tripped a gate   -> "Heads up: this doesn't name anything specific..."

The second one sat above "Everyone says this changes everything. Nobody
actually explains what." — the rewrite Starling had just produced. The product
did the work, then told him the draft was worth a rewrite.

THE MESSAGE WAS NEVER MISSING. `_apply_quality_gates` writes
`rationale=[note, *existing]` at three sites (intelligence.py:1060, :1069,
:1091), deliberately keeping the account of the work behind the weakness. Two
lines in main.py threw the second element away: `[0]` when the decision was
built, and a plain reassignment to the note a few lines later. Sixth instance of
the same shape — the reason is computed, stored, and stops at a fence — and the
first where the upstream author had already built the carrier for it.

WHY NOT ADD A SENTENCE TO THE NOTE INSTEAD. The note is generic by design; the
description is specific to the draft in the composer. Only the pair tells him
what he is looking at, and the pair already existed.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.main import _rationale_text


NOTE = "Heads up: this says very little. Worth a rewrite."
DID = "Preserved the exact point while fixing flow and removing passive voice."


def _cand(rationale, **kw):
    return SimpleNamespace(rationale=rationale, **kw)


# --------------------------------------------------------------------------- #
# the shape the gates actually produce
# --------------------------------------------------------------------------- #

def test_the_gated_pair_says_both():
    """`[note, *existing]` is what the three gate sites write. Both survive."""
    got = _rationale_text(_cand([NOTE, DID]), "unused")
    assert NOTE in got, "the weakness must still be named — silence is the older bug"
    assert DID in got, "the account of the work was discarded; this is the defect"
    assert got.index(NOTE) < got.index(DID), "the weakness leads, as the gates ordered it"


def test_a_clean_draft_is_unchanged():
    """The passing case already worked and must keep working."""
    assert _rationale_text(_cand([DID]), "unused") == DID


def test_the_near_miss_path_still_speaks():
    """`_assemble_candidates` builds `rationale=[note]` alone for a near miss.
    There is no description to add, and the note must survive by itself."""
    assert _rationale_text(_cand([NOTE]), "unused") == NOTE


def test_the_note_is_not_said_twice():
    """`review_note` repeats `rationale[0]`. Joining blind printed it twice."""
    got = _rationale_text(_cand([NOTE, NOTE, DID]), "unused")
    assert got.count(NOTE) == 1, got


# --------------------------------------------------------------------------- #
# the empty and overlong shapes
# --------------------------------------------------------------------------- #

def test_an_empty_rationale_falls_back():
    for empty in ([], None, [""], ["   "], [None]):
        assert _rationale_text(_cand(empty), "fallback") == "fallback", empty


def test_a_missing_attribute_falls_back():
    assert _rationale_text(SimpleNamespace(), "fallback") == "fallback"


def test_a_bare_string_is_not_shredded_into_characters():
    """`DraftCandidate.rationale` is `list[str]`, so pydantic makes this
    unreachable today. It is pinned because the failure is silent: iterating a
    string yields characters, `parts[:2]` takes two of them, and the card reads
    "H e" — which would be blamed on the model, not on this line."""
    assert _rationale_text(_cand(DID), "unused") == DID


def test_a_third_entry_is_dropped_not_run_off_the_card():
    """`_assemble_candidates` appends a grok advisory, which can make three.
    The card shows one line; two is the readable limit."""
    grok = "@grok is suggested only because this draft asks for public analysis."
    got = _rationale_text(_cand([NOTE, DID, grok]), "unused")
    assert grok not in got
    assert NOTE in got and DID in got


# --------------------------------------------------------------------------- #
# the fence cannot come back
# --------------------------------------------------------------------------- #

def test_neither_decision_builder_indexes_the_first_entry():
    """Pinned structurally, not by absence of a string.

    An absence check would trip on this module's own prose and on the comment
    that explains the fix — the same trap that caught two earlier source scans
    here. So assert the helper is CALLED at both builders instead.
    """
    import inspect
    import re

    from app import main

    source = inspect.getsource(main)
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    # Drop the `def` line too — its signature reads exactly like a call and
    # counting it made this test fail on its own first run.
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith(("#", "def ")))
    assert body.count("_rationale_text(candidate,") == 2, (
        "both decision builders must compose the rationale through the helper; "
        "a new one indexing [0] reintroduces the discard")


def test_the_review_note_assignment_is_guarded():
    """The second discard: `decision["rationale"] = note` ran unconditionally
    after the helper had already composed the pair. It may only speak into an
    empty rationale now."""
    import inspect
    import re

    from app import main

    source = re.sub(r'"""(?:.|\n)*?"""', "", inspect.getsource(main))
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    idx = body.index('decision["review_note"] = note')
    window = body[idx:idx + 400]
    assert 'if not str(decision.get("rationale")' in window, (
        "the note assignment must stay behind an emptiness guard, or it "
        "overwrites the composed pair exactly as before")
