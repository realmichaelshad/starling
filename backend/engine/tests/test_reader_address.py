"""A from-scratch post does not tell the reader about themselves.

Measured across all 35 of his published posts: ZERO make a claim about the
reader's situation, and exactly one contains the word "you" at all — a reply to
@grok. The model closes on one constantly, and it is always the point where an
otherwise-grounded draft stops being checkable:

    "World-model-optimizer cuts costs in half."  | "You're saving thousands on
                                                    model training."
    "Spur solver's Z3 magic solves agent bugs."  | "You're fighting a battle
                                                    that's already lost."

The first block came from the evidence. The second is about a stranger the
writer has never met, and no gate downstream can check it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import drop_reader_address           # noqa: E402


def test_a_trailing_reader_claim_is_removed():
    text = ("World-model-optimizer cuts costs in half.\n\n"
            "You're saving thousands on model training.")
    assert drop_reader_address(text) == "World-model-optimizer cuts costs in half."


def test_the_grounded_half_is_kept_intact():
    text = ("Spur solver's Z3 backend resolves coding agent bugs.\n\n"
            "You're fighting a battle that's already lost.")
    kept = drop_reader_address(text)
    assert kept == "Spur solver's Z3 backend resolves coding agent bugs."


def test_it_never_empties_a_draft():
    """A post that is ONLY a reader claim has a different problem, and the
    quality gates must see it whole rather than be handed a fragment."""
    only = "You're saving thousands on model training."
    assert drop_reader_address(only) == only
    assert drop_reader_address("") == ""


def test_a_closing_block_doing_real_work_survives():
    """Second person is not the fault; emptiness is. A closer that carries a
    figure or a named product is substance, whatever pronoun it uses."""
    text = ("Claude Code costs $200 a month.\n\n"
            "You're paying 4x what Goose charges for the same loop.")
    assert drop_reader_address(text) == text


def test_a_reply_is_untouched_by_this_rule():
    """Not tested here directly — `_assemble_candidates` only calls this for
    non-reply actions — but the intent is recorded: in a reply "you" is the
    person being answered, and removing it would break the conversation."""
    text = ("That matches what I saw.\n\n"
            "You're right that the routing is the bottleneck.")
    # The function itself would cut it; the CALLER is what protects replies.
    assert drop_reader_address(text) != text


def test_it_does_not_rewrite_his_own_posts():
    """The scrubber bar: text surgery is held stricter than rejection."""
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    changed = [p for p in posts if drop_reader_address(p) != p]
    assert not changed, (
        f"rewrote {len(changed)}/{len(posts)} of his own posts, "
        f"e.g. {changed[0][:100]!r}")
