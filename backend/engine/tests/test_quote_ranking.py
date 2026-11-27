"""A candidate that commits to something outranks one that only asks.

GrowthScore judges shape. A rhetorical question has excellent shape — a hook, a
format fit, an alignment — and asserts nothing, so the scorer cannot see the
one fault that matters most here. This is the same blindness already recorded
for replies, on the quote surface.

Measured live 2026-07-27, quoting @simonw's Claude Code pricing post:

    [0] "Why's Goose free when it's doing the same thing as Claude?"
    [1] "Goose is free, but it's not like you're getting a free ride."

The ranker chose [0]. Six of ten quotes in that run were questions.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import (_assemble_candidates, asks_without_saying,   # noqa: E402
                              get_personal_model)
from app.safety import assess_context                                      # noqa: E402
from app.schemas import DraftRequest, PostContext                          # noqa: E402

SOURCE = ("Claude Code costs up to $200 a month. Goose does the same thing for "
          "free, and I've been running it against local models all week.")


def _request() -> DraftRequest:
    return DraftRequest(action="quote",
                        context=PostContext(author="@simonw", author_handle="@simonw",
                                            text=SOURCE, visibility="public"))


def test_a_claim_is_ranked_above_a_question():
    request = _request()
    raw = [
        {"text": "Why's Goose free when it's doing the same thing as Claude?"},
        {"text": "Goose runs the same agent loop for nothing while Claude Code "
                 "charges $200 a month."},
    ]
    result = _assemble_candidates(raw, [], "unavailable", request, [],
                                  get_personal_model(), {"objective": "reach"},
                                  assess_context(request.context))
    assert result, "the run produced nothing to rank"
    assert not asks_without_saying(result[0].text), (
        f"a question was ranked first: {result[0].text!r}")


def test_the_order_of_two_claims_is_left_alone():
    """The tier must only bite when the candidates differ on it."""
    request = _request()
    raw = [
        {"text": "Goose runs the same agent loop for nothing while Claude Code "
                 "charges $200 a month."},
        {"text": "Claude Code's $200 tier buys the same loop Goose gives away."},
    ]
    result = _assemble_candidates(raw, [], "unavailable", request, [],
                                  get_personal_model(), {"objective": "reach"},
                                  assess_context(request.context))
    for candidate in result:
        assert not asks_without_saying(candidate.text)
