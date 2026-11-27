"""The create-post brief lost the one instruction that says "don't sound generated".

`_compose` truncated at 1500 characters and `create_post` is the only brief that
carries the topic list — so it was the only one that went over, and the
truncation took the last thing appended, which is the register instruction:

    "It must be specific enough to react to, sound like the writer, and never
     read as corporate or generated."

Measured in the deployed tree: `create_post` came to 1494 characters with the
clause ABSENT, ending on the fragment "OpenAI, GPT releases, Codex, Grok, xAI,
Elon in" — the identical ending of 1,429 stored briefs. `create_reply` (1491)
and `create_quote` (1478) kept it. Same field, same builder, same budget; the
defect confined to the one action that carries `tiers`. That control is what
makes it a truncation bug rather than a missing string.

Create Post is his most-used door.

WHAT THE FIX COSTS, MEASURED AND NOT GUESSED. Reserving the clause leaves 23
characters where the shortest useful topic list needs 38, so on his pack the
topic block is DROPPED rather than trimmed. That is the right side of the trade
and it was checked rather than assumed: the create-post DIRECTION carries the
actual subject and its evidence ("USE THESE FACTS, and only these: Grok Image
2.0 ..."), so `tiers` is background about the account's domain, which the
direction supersedes on every real post. The register instruction applies to
every post and was missing from all of them.
"""
from __future__ import annotations

import pytest

from app.strategy_v2 import (BRIEF_CHAR_BUDGET, CREATE_CLOSING, _compose,
                             _fit_list, _tidy, brief_for)


CREATE = ("create_post", "create_reply", "create_quote")
ALL_BRIEFS = CREATE + ("enhance_post", "enhance_reply", "enhance_quote",
                       "chat_persona", "decision")


# --------------------------------------------------------------------------- #
# the defect and its control
# --------------------------------------------------------------------------- #

def test_the_post_brief_keeps_its_closing_rule():
    brief = brief_for("create_post") or ""
    assert brief, "no create_post brief at all"
    assert CREATE_CLOSING in brief, (
        "the register instruction was truncated away again — this is the one "
        "that says never read as corporate or generated")


def test_enhance_post_is_a_different_surface_not_a_second_instance():
    """THE DIAGNOSIS THAT SAVED THE FIX.

    A 1,389-character brief — comfortably under the 1,500 cap, not ending
    mid-item — was found without the closing clause, which would have meant
    truncation was not the whole cause. It is an `enhance_post`: generation
    `6e5422cc` carries `enhancement_mode: True`, an `original_draft`, and a
    direction beginning "Enhance this exact user-written post". Its brief ends
    on its OWN closing instruction and never contained the create clause.

    Split across all 1,569 stored post-action briefs, the two surfaces do not
    overlap by a single row:

        create_post    1,429 briefs, EVERY ONE exactly 1494 chars (the cut)
        enhance_post     140 briefs, EVERY ONE exactly 1389 chars (untouched)

    So truncation is the whole cause for `create_post`, and the fourth specimen
    is a different brief. **`action="post"` is not the surface** — it covers
    both doors, and `enhancement_mode` is the discriminator. Any count of
    "create-post briefs" filtered on `action` alone mixes them.

    Whether `enhance_post` SHOULD carry a register instruction is a separate
    decision, not a bug: it has never had one.
    """
    enhance = brief_for("enhance_post") or ""
    assert enhance, "no enhance_post brief"
    assert CREATE_CLOSING not in enhance, (
        "enhance_post has gained the create clause — if that is intended, say "
        "so deliberately; it is a different surface, not a fix for this one")
    assert enhance.rstrip().endswith("return one polished ready-to-post version."), \
        enhance[-70:]
    assert len(enhance) < BRIEF_CHAR_BUDGET, (
        "enhance_post is now at the cap too, which would make it a real "
        "instance of this defect rather than a different surface")


@pytest.mark.parametrize("key", ["create_reply", "create_quote"])
def test_the_controls_are_unchanged(key):
    """These fit under the budget and always kept the clause. They are the
    reason this is a truncation bug and not a missing string, and a fix that
    disturbs them has changed the wrong thing."""
    assert CREATE_CLOSING in (brief_for(key) or "")


def test_no_brief_ends_on_the_historical_fragment():
    """1,429 stored briefs ended on "... Grok, xAI, Elon in"."""
    for key in ALL_BRIEFS:
        brief = (brief_for(key) or "").rstrip()
        assert not brief.endswith(" in"), f"{key}: {brief[-60:]!r}"


@pytest.mark.parametrize("key", ALL_BRIEFS)
def test_every_brief_still_fits_the_budget(key):
    """Reserving room must not be a way to exceed it."""
    assert len(brief_for(key) or "") <= BRIEF_CHAR_BUDGET, key


@pytest.mark.parametrize("key", ALL_BRIEFS)
def test_no_brief_ends_mid_word_or_mid_list(key):
    brief = (brief_for(key) or "").rstrip()
    if not brief:
        return
    assert brief[-1] in ".!?", f"{key} ends {brief[-40:]!r}"
    assert "(highest." not in brief, (
        f"{key} carries a list header introducing nothing")


# --------------------------------------------------------------------------- #
# the reservation itself
# --------------------------------------------------------------------------- #

def test_a_reserved_tail_always_survives():
    tail = "KEEP THIS."
    out = _compose("x" * 4000, budget=200, tail=tail)
    assert out.endswith(tail)
    assert len(out) <= 200


def test_a_brief_that_already_fits_is_unchanged_by_the_reservation():
    """The controls must be byte-identical, not merely still passing."""
    parts = ("Alpha sentence.", "Beta sentence.")
    tail = "Closing rule."
    assert _compose(*parts, tail=tail, budget=500) == \
        _compose(*parts, tail, budget=500)


def test_the_body_is_what_yields():
    out = _compose("y" * 300, budget=120, tail="END.")
    assert out.endswith("END.")
    assert len(out) <= 120
    assert out.count("y") < 300


# --------------------------------------------------------------------------- #
# the donor list
# --------------------------------------------------------------------------- #

def test_whole_items_only():
    """Never a partial item: 39 characters of four topics fit in 40, 46 of five
    do not, and the answer is four whole ones rather than four and a stub."""
    out = _fit_list("Topics: ", ["Anthropic", "Claude", "Fable", "Opus", "Sonnet"], 40)
    assert out == "Topics: Anthropic, Claude, Fable, Opus."
    assert len(out) <= 40


def test_a_header_never_appears_without_a_list():
    """The naive character cut produced "Core topics (highest." — a header
    introducing nothing, which is worse than no header."""
    assert _fit_list("Core topics (highest fit): ", ["Anthropic", "Claude"], 30) == ""
    assert _fit_list("Core topics (highest fit): ", ["Anthropic"], 0) == ""
    assert _fit_list("Core topics (highest fit): ", [], 500) == ""


def test_it_keeps_as_many_as_fit():
    items = [f"topic{n}" for n in range(20)]
    out = _fit_list("T: ", items, 60)
    assert out.startswith("T: topic0, topic1")
    assert len(out) <= 60


def test_a_generous_room_keeps_everything():
    items = ["Anthropic", "Claude", "Fable"]
    assert _fit_list("T: ", items, 500) == "T: Anthropic, Claude, Fable."


# --------------------------------------------------------------------------- #
# tidying a cut clause
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw, expected", [
    # WORD-level, not item-level: "Elon" is a whole word, and `_tidy` cannot
    # know it is the head of "Elon in direct relation to AI/model claims".
    # Cutting a LIST at item boundaries is `_fit_list`'s job, which is why the
    # topic list no longer reaches this function at all.
    ("Codex, Grok, xAI, Elon in", "Codex, Grok, xAI, Elon."),
    ("one thesis and", "one thesis."),
    ("a list of items,", "a list of items."),
    ("already closed.", "already closed."),
    ("a question?", "a question?"),
])
def test_tidy_closes_a_severed_clause(raw, expected):
    assert _tidy(raw) == expected
