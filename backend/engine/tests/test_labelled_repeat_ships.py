"""A repeat at the API boundary ships with the weakness named — never as "".

Two mechanisms both guard against repeats, and they were fighting:

  1. `_assemble_candidates` sees the repeat, keeps it aside, and ships it with
     "You have written this before ... it is not a new idea" attached — the
     path that exists so he is never left with nothing.
  2. `_remember` at the API boundary sees the same repeat and returned "".

First round (2026-07-27 morning): the boundary blanked drafts the pipeline had
already labelled — fixed with `already_labelled`. Second round (same day,
evening): a repeat the pipeline did NOT label still came back as an empty
composer — two of ten measured posts, both on topics drafted earlier the same
day. The registry's rule is that a quality miss ships with the fault named,
and a silent empty is the most expensive failure this house recognises. So
`_remember` now returns (text, note): the text always survives, and the note
carries the standard repeat warning for the decision card when the boundary is
the one that caught it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main                                       # noqa: E402
from app.intelligence import remember_draft                # noqa: E402

DRAFT = ("Otlet lets you run Qwen35_4b inside Postgres, so the data never "
         "leaves the database to get model work done.")


def test_an_unlabelled_near_duplicate_ships_with_the_note():
    """The boundary catch names the fault instead of deleting the draft."""
    remember_draft(DRAFT)
    again = DRAFT.replace("so the data never leaves", "meaning data never leaves")
    out, note = main._remember(again)
    assert out == again, "the repeat's text must survive the boundary"
    assert note == main._BOUNDARY_REPEAT_NOTE, \
        "the boundary catch must hand the caller the standard repeat warning"


def test_a_labelled_repeat_reaches_the_composer_unannotated():
    """The pipeline already decided to ship it WITH its own warning attached —
    the boundary adds nothing on top."""
    remember_draft(DRAFT)
    again = DRAFT.replace("so the data never leaves", "meaning data never leaves")
    out, note = main._remember(again, already_labelled=True)
    assert out == again, "the labelled repeat was blanked after being labelled"
    assert note is None


def test_a_fresh_draft_always_passes_through():
    fresh = ("Belay is a local firewall for coding agents, AGPL-3.0, with no "
             "model in the decision path at all.")
    assert main._remember(fresh) == (fresh, None)
    assert main._remember(fresh, already_labelled=True) == (fresh, None)
