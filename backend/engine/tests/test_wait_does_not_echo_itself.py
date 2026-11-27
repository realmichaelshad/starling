"""A WAIT card does not print its own verdict twice.

FOUND BY DRIVING, 2026-08-07. Five posts through `mode=context`; four came back
WAIT and every one of them looked like this on the wire:

    rationale : 'Nothing here is worth acting on right now.'
    reasons   : ['Nothing here is worth acting on right now.']

`content.ts:2688` joins them — `[decision.rationale, ...reasons]` — so the card
reads the sentence, then reads it again as a bullet.

IT IS STRUCTURAL, NOT A COINCIDENCE, which is why 4 of 4 duplicated while the
one REPLY in the same run carried three distinct and useful reasons:

    reasons                = list(decision_card.why_now)
    _decision_rationale()  = (card.why_now or [...])[0]   when action == "wait"

The rationale IS the first reason on a wait. Any wait therefore echoed, and the
verdict that most needs justifying was the one saying nothing twice — while the
product's own "what does wait mean" answer promises "every verdict comes with
the reason it lost".

WHAT THIS FIX DOES NOT DO. It removes the echo; it does not make the wait reason
better. `why_now` can still be the bare "Nothing here is worth acting on right
now." and that is thin. Enriching it is a separate question — the decider holds
`outweighed` and `risks`, but `decider.py:436` says the term names there are for
logs, so promoting them is a decision to take deliberately and not a cleanup.
"""
from __future__ import annotations

import pytest


def _filter(reasons: list[str], rationale: str) -> list[str]:
    """The shipped rule, applied the way `/browser/assist` applies it."""
    return [item for item in reasons
            if str(item).strip() != str(rationale).strip()]


def _row(action: str, rationale: str, reasons: list[str]) -> dict:
    """A `list_generations` row as `_why_decision_answer` actually reads it:
    the decision lives inside `request_json`, as JSON text."""
    import json

    return {"request_json": json.dumps(
        {"decision": {"action": action, "rationale": rationale, "reasons": reasons}})}


WAIT = "Nothing here is worth acting on right now."
HINT = "You've hit today's posting budget — hold this for the next window."
SOURCE = "Primary source: @AnthropicAI is the verified account this is about."


def test_the_measured_wait_stops_repeating_itself():
    assert _filter([WAIT], WAIT) == []


def test_a_real_second_reason_survives():
    """`why_now` carries up to two, and the unscored-term hint is often the
    useful one. Dropping the whole list would have traded one defect for a
    worse one."""
    assert _filter([WAIT, HINT], WAIT) == [HINT]


def test_the_source_line_survives():
    """The source note is prepended ahead of `why_now` and is the single most
    useful line on a first-party announcement."""
    assert _filter([SOURCE, WAIT], WAIT) == [SOURCE]


def test_whitespace_does_not_smuggle_the_echo_through():
    assert _filter([f"  {WAIT} "], WAIT) == []


def test_a_reply_keeps_all_three_reasons():
    """The non-wait case measured in the same run — nothing here may touch it."""
    reply_rationale = "The post makes a forward claim that can be given a falsifiable condition."
    reasons = ["The post is clear enough to reply to without guessing.",
               "You have a specific point to add that others have not.",
               "The thread is live and worth entering now."]
    assert _filter(reasons, reply_rationale) == reasons


# --------------------------------------------------------------------------- #
# the rule is where the card is built
# --------------------------------------------------------------------------- #

def test_the_filter_sits_in_the_opportunity_builder():
    """Pinned structurally. An absence check would trip on this file's prose
    and on the comment that explains the fix — the trap that has caught three
    source scans here already."""
    import inspect
    import re

    from app import main

    source = re.sub(r'"""(?:.|\n)*?"""', "", inspect.getsource(main))
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    idx = body.index('"reasons": reasons,\n            "source_line"')
    window = body[max(0, idx - 700):idx]
    assert "str(item).strip() != str(rationale).strip()" in window, (
        "the echo filter must stay immediately above the opportunity dict; "
        "without it every wait prints its verdict twice")


def test_the_safety_block_does_not_echo():
    """The same echo on the path that matters most.

    `main.py` built the skip card with `rationale = assessment.reasons[0]` and
    `reasons = assessment.reasons`, so the sentence telling him not to engage
    with harmful or private material printed twice. `strategy.py:183` still has
    the identical pair — another lane's file, reported not edited.
    """
    reasons = ["The post contains a private phone number.",
               "Amplifying it would spread personal data."]
    blocked = reasons[0]
    assert _filter(reasons, blocked) == [reasons[1]], (
        "the blocking reason must not repeat as its own first bullet, and the "
        "SECOND reason must survive")


def test_a_single_reason_block_leaves_no_bullets():
    reasons = ["The post contains a private phone number."]
    assert _filter(reasons, reasons[0]) == []


def test_the_recorded_decision_is_not_read_back_twice(monkeypatch):
    """"Why did you say that" reads a STORED decision, and every decision
    already on disk still pairs `rationale` with `reasons[0]`.

    So the echo could not be fixed only at the writing end — this answer reads
    history, and history is full of the duplicate.
    """
    from app import main

    monkeypatch.setattr("app.storage.list_generations",
                        lambda **kw: [_row("wait", WAIT, [WAIT])])
    answer = main._why_decision_answer()
    assert answer.count(WAIT) == 1, f"the stored reason was read back twice:\n{answer}"


def test_a_genuinely_different_stored_reason_is_still_shown(monkeypatch):
    """The filter must not silence a real second sentence."""
    from app import main

    monkeypatch.setattr("app.storage.list_generations", lambda **kw: [
        _row("reply", "There is a distinct point here.",
             ["The thread is live and worth entering now."])])
    answer = main._why_decision_answer()
    assert "The thread is live and worth entering now." in answer
    assert "There is a distinct point here." in answer


def test_the_wait_rationale_is_still_the_first_why_now():
    """The fact the filter depends on. If `_decision_rationale` stops deriving
    the wait line from `why_now`, this filter silently stops matching and the
    echo returns — so the dependency is pinned rather than assumed."""
    import inspect

    from app import main

    source = inspect.getsource(main._decision_rationale)
    assert 'card.action == "wait"' in source
    assert "card.why_now" in source
