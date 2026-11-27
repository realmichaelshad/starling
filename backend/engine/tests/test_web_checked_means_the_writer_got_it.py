""""Web checked" must mean the writer received the facts, not that a search ran.

`web_checked` is the one field of the five proposed for the draft card that
carries decision information — and it was computed from the wrong thing:

    web_checked = bool(grounding and grounding.get("evidence"))   # the search
    web_checked = EVIDENCE_LEAD in direction                      # the writer

`with_evidence` budgets the facts against whatever room is left in the 1200-char
direction and drops them entirely when there is none; the clamp that follows can
cut them off after that. On a reply both happen. Measured over his 84 real reply
drafts: 5 (6%) carry facts, and 43 (51%) have a direction at or above 1150 of
1200 characters with none. Every starved one reported "web checked".

WHY THIS IS SAFE TO CHANGE. `web_checked is False` also drives the composer's
wait recommendation on posts (`test_wait_on_create`). The change is monotone:
the lead is only ever appended when the evidence is non-empty, so
`EVIDENCE_LEAD in direction` implies the old `bool(evidence)`. It can therefore
only ADD a wait recommendation, never remove one — and only for a draft that
genuinely reached the writer with nothing behind it. Measured over his 1,569
post generations: 76% carry the facts, and of the 382 that do not, none is at
the cap, so on the post path the two definitions agree on the data that exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.direction import (DIRECTION_CAP, EVIDENCE_LEAD, build_post_direction,
                           with_evidence)


MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


# --------------------------------------------------------------------------- #
# the property, through the real builder
# --------------------------------------------------------------------------- #

def test_a_starved_direction_reports_not_checked():
    """THE CASE THAT WAS WRONG. Evidence exists, the search ran, and the
    direction is already full — so the writer gets nothing and the flag must
    say so."""
    full = "x" * (DIRECTION_CAP - 20)
    built, _shape = build_post_direction(full, "OpenAI priced o5 at $12 per "
                                         "million output tokens on 5 August.",
                                         action="reply")
    assert EVIDENCE_LEAD not in built, "control: the facts should not have fit"
    assert len(built) <= DIRECTION_CAP


def test_a_direction_with_room_reports_checked():
    built, _shape = build_post_direction(
        "Write one useful point.",
        "OpenAI priced o5 at $12 per million output tokens on 5 August.",
        action="reply")
    assert EVIDENCE_LEAD in built


def test_no_evidence_reports_not_checked():
    built, _shape = build_post_direction("Write one useful point.", "",
                                         action="post")
    assert EVIDENCE_LEAD not in built


@pytest.mark.parametrize("action", ["post", "reply", "quote"])
def test_the_test_works_for_every_action(action):
    """A reply uses a different trailing instruction from a post, so a flag
    keyed on the instruction would be action-dependent. The lead is not."""
    built = with_evidence("Write one useful point.",
                          "A measured fact with enough length to clear the "
                          "eighty-character floor comfortably.", action=action)
    assert EVIDENCE_LEAD in built


# --------------------------------------------------------------------------- #
# monotonicity — the reason this cannot silently remove a wait recommendation
# --------------------------------------------------------------------------- #

def test_the_lead_never_appears_without_evidence():
    """New-True implies old-True. Since `web_checked is False` triggers the
    composer's wait recommendation, this is what guarantees the change can only
    add one."""
    for direction in ["", "short", "y" * 900, "y" * (DIRECTION_CAP - 1)]:
        for empty in ["", None]:
            assert EVIDENCE_LEAD not in with_evidence(direction, empty)


# --------------------------------------------------------------------------- #
# both sites — a fix applied to one of two doors is not applied
# --------------------------------------------------------------------------- #

def test_both_draft_paths_read_what_the_writer_was_given():
    """The composer path and the per-post ✦ path each compute this. They were
    both wrong in the same way and both had to change.

    W2 (2026-09-21): the facts moved out of the direction into their own field
    and prompt block (`DraftRequest.facts`, rendered whenever non-empty), so
    "the writer received the facts" is read off the built request now, not off
    the direction string they no longer travel in."""
    source = MAIN.read_text(encoding="utf-8")
    # `= None` is the enhance branch and the initialiser — "not applicable",
    # which is a third state and not a claim about a search. `= False` is the
    # placeholder before the request exists.
    reads = [r.strip() for r in re.findall(r"web_checked = (.+)", source)
             if r.strip() not in ("None", "False")]
    assert len(reads) >= 2, f"expected two assignment sites, found {reads}"
    for expression in reads:
        assert expression == "_writer_got_facts(draft_request)", (
            f"a draft path still infers web_checked from the search: {expression}")


def test_the_writer_got_facts_exactly_when_the_facts_block_exists():
    from app.llm import draft_messages
    from app.main import _writer_got_facts
    from app.schemas import DraftRequest, PostContext
    context = PostContext(author="A", text="A post about a launch today.", visibility="public")
    with_facts = DraftRequest(action="reply", context=context, facts="Example Labs shipped v2 today.")
    without = DraftRequest(action="reply", context=context)
    assert _writer_got_facts(with_facts) and not _writer_got_facts(without)
    assert "Example Labs shipped v2 today." in draft_messages(with_facts, [], {})[-1]["content"]
    assert "Facts you may use" not in draft_messages(without, [], {})[-1]["content"]


def test_neither_path_asks_the_search():
    source = MAIN.read_text(encoding="utf-8")
    for dead in ['web_checked = bool(evidence)',
                 'web_checked = bool(grounding and grounding.get("evidence"))']:
        assert dead not in source, f"the old definition is back: {dead}"


def test_the_flag_is_computed_from_the_built_request():
    """A flag read before the request exists describes a request that was never
    sent. (W2: it used to be read after the direction clamp for the same reason.)"""
    source = MAIN.read_text(encoding="utf-8")
    matches = list(re.finditer(r"web_checked = _writer_got_facts\(draft_request\)", source))
    assert len(matches) >= 2
    for match in matches:
        preceding = source[:match.start()]
        assert preceding.rfind("draft_request = DraftRequest(") != -1, (
            "a web_checked assignment is read before the DraftRequest is built")
