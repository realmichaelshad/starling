"""The quote rules must not offer "the question it raises" as a way to open.

Six of ten quotes came back as bare rhetorical questions on 2026-07-27 — ten
different source tweets, one template:

    "What's the catch with Goose if it's really beating Claude?"
    "What's the real cost of open weights when the models are already
     outpacing closed ones?"
    "Does Cowork mean Claude finally gets to work in your files ...?"

The cause was in the prompt, not the model. `SOURCE_ADDITION_RULE` said to open
with "your own judgement, the consequence, or the question it raises" — and
offered three options, the model reliably took the one that commits to nothing,
because a question is the cheapest of the three to produce.

A gate, a selection tier and revise guidance were all built downstream before
anyone read the instruction that was causing it. This test exists so the clause
cannot come back.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm import SOURCE_ADDITION_RULE, _draft_rules   # noqa: E402
from app.schemas import DraftRequest, PostContext           # noqa: E402


def test_the_rules_never_offer_a_question_as_an_opening():
    assert "question it raises" not in SOURCE_ADDITION_RULE.lower(), (
        "the clause that produced six of ten question-only quotes is back")


# RETIRED (API-native pass, 2026-09-22): test_the_rules_ask_for_a_claim_instead: whether a quote asks or claims is the model's call in the writer's voice; no rule.


# RETIRED (API-native pass, 2026-09-22): test_the_rule_still_reaches_a_quote_request: same: the quote task says what the surface is, not which move to make.


def test_a_from_scratch_post_does_not_get_the_source_rule():
    """It is about the post being responded to; a post has none."""
    request = DraftRequest(action="post",
                           context=PostContext(author="a", text="", visibility="public"))
    assert "About the post you are responding to" not in _draft_rules(request)
