"""Everything after the hook, and the harness vouching for itself.

Three fabrication holes, all found by execution rather than by reading, all in
the path a from-scratch post takes.

1. PARTS 2..N REACHED NO INTEGRITY GATE. On a thread request `llm.py` sets
   `candidate["text"] = parts[0]` and puts the whole thread in `thread_parts`,
   while `_assemble_candidates` ran its five hard gates — unsupported figures,
   invented experience, leaked instructions, unvouched URLs, injected entities —
   on `text` alone. `thread_parts` was not built until ~170 lines later. A part
   two reading "It cut inference spend by 62% on a 750,000 line refactor" shipped
   with no note, and `main.py` joins the parts into both the chat answer and the
   composer, so it was exactly as public as the hook.

2. PARTS 2..N REACHED NO SCRUBBER either, for the same reason: the whole scrub
   chain runs on `text`, and the parts were rebuilt from raw model output
   afterwards — which also threw away any scrubbing that had been done.

   `tests/test_threads.py::test_thread_candidates_gate_every_part` passed
   throughout and is why this looked handled: engagement_bait is caught by
   `assess_draft` INSIDE the per-part loop, so its premise held for the one gate
   it exercised while the five above stayed blind.

3. THE SHAPE BLOCK WAS EVIDENCE. `shape.py` always appends "no sentence past 20
   words" and "Write it as N blocks..., N sentences total", and the whole
   `direction` string went into the corpus the figure gate checks against — so
   20, the block count and the word target were permanently "supported". A draft
   claiming "20% cheaper", "$20" or "4x" was vouched for by Starling's own
   instruction about sentence length.
"""
from __future__ import annotations

import pytest

from app import intelligence, storage
from app.intelligence import (PersonalModel, _assemble_candidates,
                              _direction_without_scaffolding, _unsupported_claims)
from app.safety import assess_context
from app.schemas import DraftRequest, PostContext


HOOK = "Claude Code runs in the terminal now, and that changes the loop."
SHAPE = ("SHAPE FOR THIS POST: write 3 blocks separated by blank lines, "
         "4 sentences total; no sentence past 20 words.")
DIRECTION = ("TOPIC: Claude Code. USE THESE FACTS, and only these: Anthropic put "
             "Claude Code into the terminal as an agent. " + SHAPE)


def _request():
    request = DraftRequest(action="post",
                           context=PostContext(author="", text="", visibility="public"),
                           research_mode="off", media_policy="no_media",
                           grok_permission=False)
    request.direction = DIRECTION
    return request


def _assemble(parts):
    storage.initialize()
    request = _request()
    raw = [{"text": parts[0], "thread_parts": list(parts)}]
    return _assemble_candidates(raw, [], "not_needed", request, [], PersonalModel(),
                                {}, assess_context(request.context))


# --------------------------------------------------------------------------- #
# 1. the five gates now judge every part
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("part_two, what", [
    ("It cut inference spend by 62% on a 750,000 line refactor.", "an invented figure"),
    ("I ran it on my own repo for a week before this landed.", "invented experience"),
    ("Microsoft ships the same terminal agent next month, apparently.", "an injected entity"),
    ("The full writeup is at https://evil-host.example/claim now.", "an unvouched link"),
])
def test_a_fabrication_in_part_two_sinks_the_whole_thread(part_two, what):
    assert _assemble([HOOK, part_two]) == [], (
        f"{what} in part two shipped — the gates are only seeing the hook again")


def test_a_clean_thread_still_ships_with_every_part():
    """The gates must sink fabrication, not threads."""
    clean = "The terminal is where the work already happens, so the loop shortens."
    out = _assemble([HOOK, clean])
    assert out, "a clean thread was rejected"
    parts = getattr(out[0], "thread_parts", None) or []
    assert len(parts) == 2, parts
    assert parts[0] == HOOK


def test_the_gates_run_over_the_parts_not_just_the_text(monkeypatch):
    """Measured, because the bug was invisible to every assertion about output:
    the gate was CALLED once, with the hook."""
    seen: list[str] = []
    real = intelligence._unsupported_claims

    def spy(text, corpus):
        seen.append(text)
        return real(text, corpus)

    monkeypatch.setattr(intelligence, "_unsupported_claims", spy)
    clean = "The terminal is where the work already happens, so the loop shortens."
    _assemble([HOOK, clean])
    assert len(seen) >= 2, f"the gate only ever saw {seen!r}"
    assert any(clean in text for text in seen), "part two was never passed to the gate"


# --------------------------------------------------------------------------- #
# 2. the parts are the scrubbed ones
# --------------------------------------------------------------------------- #

# RETIRED (API-native pass, 2026-09-22): test_part_two_is_scrubbed_not_raw_model_output: the style scrubbers left the pipeline for every part; the integrity gates still judge each part.


# --------------------------------------------------------------------------- #
# 3. the harness does not vouch for itself
# --------------------------------------------------------------------------- #

def test_the_shape_block_is_not_evidence():
    request = _request()
    stripped = _direction_without_scaffolding(request)
    assert "SHAPE FOR THIS POST" not in stripped
    assert "20 words" not in stripped
    assert "Anthropic put Claude Code into the terminal" in stripped, (
        "the real evidence was cut away with the scaffolding")


@pytest.mark.parametrize("figure", ["20%", "$20", "4x"])
def test_a_figure_matching_the_shape_numbers_is_still_unsupported(figure):
    """Every one of these passed while the shape block was in the corpus."""
    request = _request()
    corpus = _direction_without_scaffolding(request)
    draft = f"Claude Code is {figure} better than the alternative."
    assert _unsupported_claims(draft, corpus), f"{figure} is vouched for by nothing"
    # ...and the raw direction is what made it pass, so this is the regression.
    assert not _unsupported_claims(draft, DIRECTION), (
        "if this now fails the shape block stopped carrying numbers — good, but "
        "this test no longer proves anything and should be re-anchored")


def test_grounded_claims_still_pass():
    request = _request()
    corpus = _direction_without_scaffolding(request)
    assert not _unsupported_claims(
        "Claude Code is a terminal agent from Anthropic.", corpus)
