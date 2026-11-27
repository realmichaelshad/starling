"""Draft cleaning: an unpostable draft must never reach the composer.

Length is scored as a preference in growth_scorer.LENGTH_SWEET_SPOTS. These are
the deterministic gates behind that: X refuses anything over 280 characters
whatever the model felt like writing, and a reasoning scratchpad must not leak
into a post.
"""
from __future__ import annotations

import pytest

from app.llm import POST_LIMIT, _strip_thinking, fit_to_limit


# --------------------------------------------------------------------------- #
# Reasoning scratchpads
# --------------------------------------------------------------------------- #

def test_closed_think_block_is_removed():
    assert _strip_thinking("<think>weighing it</think>The post.") == "The post."


def test_unterminated_think_block_is_removed():
    """A model cut off by the token budget emits `<think>` with no closing tag.
    The old pattern only matched closed blocks and shipped this verbatim."""
    assert _strip_thinking("<think>still reasoning and then the budget ran") == ""


def test_text_before_an_unterminated_block_survives():
    assert _strip_thinking("The post.\n<think>second thoughts") == "The post."


def test_ordinary_text_is_untouched():
    assert _strip_thinking("A perfectly ordinary post.") == "A perfectly ordinary post."


# --------------------------------------------------------------------------- #
# Length
# --------------------------------------------------------------------------- #

def test_short_drafts_are_untouched():
    text = "A perfectly ordinary post."
    assert fit_to_limit(text) == text


def test_a_draft_at_exactly_the_limit_is_untouched():
    text = "x" * POST_LIMIT
    assert fit_to_limit(text) == text


def test_overlong_draft_is_cut_at_a_sentence_boundary():
    """The observed failure: a small model returned 394 characters for a 280 cap."""
    text = ("First sentence that is long enough to matter here. "
            "Second sentence carrying the follow-up thought along. "
            "Third sentence adding yet more words to the pile here. "
            "Fourth sentence that pushes this comfortably past the limit. "
            "Fifth sentence which must not survive the trim at all, not one word.")
    assert len(text) > POST_LIMIT, "the fixture must actually be too long"
    result = fit_to_limit(text)
    assert len(result) <= POST_LIMIT
    assert result.endswith(".") and not result.endswith("…")
    assert "Fifth sentence" not in result
    assert "Fourth sentence" in result, "trim to the LAST sentence that fits"


def test_a_wall_of_text_with_no_sentence_end_is_cut_on_a_word():
    result = fit_to_limit("word " * 200)
    assert len(result) <= POST_LIMIT
    assert result.endswith("…"), "visible truncation, not a silent stop"
    assert not result.rstrip("…").endswith(" ")


def test_a_single_unbroken_token_is_still_capped():
    """No spaces and no sentence ends — the fallbacks must not return the input."""
    assert len(fit_to_limit("x" * 500)) <= POST_LIMIT


def test_empty_and_whitespace_are_safe():
    assert fit_to_limit("") == ""
    assert fit_to_limit("   ") == ""


@pytest.mark.parametrize("limit", [40, 120, 280])
def test_the_cap_is_honoured_at_any_limit(limit):
    text = "A sentence that keeps going and going. " * 20
    assert len(fit_to_limit(text, limit)) <= limit
