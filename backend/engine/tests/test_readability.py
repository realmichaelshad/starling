"""Drafts must be understandable in one read.

The rejected examples are real output the user could not parse. The accepted
ones are real drafts that worked — the gate has to keep those.
"""
from __future__ import annotations

from app.intelligence import _readability_issues


TANGLED = [
    "Posting this as a standalone tweet risks wasting your only slot today "
    "because it invites generic chat rather than building authority on AI "
    "models or pricing leaks which are currently trending in the niche.",
    "The best move is to reply directly to Tibo thread with a specific honest "
    "story about how an LLM helped you debug code or avoid a phishing scam "
    "which shows you care about safety rather than generic praise because "
    "that drives engagement.",
]

CLEAR = [
    "Kimi K3 just topped the frontend coding leaderboard.\n\nIf that is real, "
    "Chinese labs are not 8 months behind anymore. They are right here.",
    "@thsottiaux honestly? It is the 'I just fixed my code' moment. "
    "No prompt engineering, no hallucinations.",
    "Claude Fable is hitting Max plans at 50% limits.",
    "I cut my debug time from 3 hours to 20 minutes. That is the whole story.",
]


def test_tangled_drafts_are_rejected():
    for text in TANGLED:
        assert _readability_issues(text), text[:60]


def test_clear_drafts_pass():
    for text in CLEAR:
        assert _readability_issues(text) == [], (text[:60], _readability_issues(text))


def test_one_long_sentence_is_caught():
    text = " ".join(["word"] * 35) + "."
    assert "sentence_too_long" in _readability_issues(text)


def test_stacked_clauses_are_caught():
    text = ("It works because the cache is warm rather than cold, "
            "which means the first call is fast.")
    assert "tangled_clauses" in _readability_issues(text)


def test_jargon_pile_up_is_caught():
    text = "This leverages your authority in the space. It optimizes positioning."
    assert "abstract_jargon" in _readability_issues(text)


def test_a_single_buzzword_is_tolerated():
    """One imperfect word is not worth rejecting an otherwise clear draft."""
    assert _readability_issues("This is good positioning for the launch.") == []


def test_short_punchy_fragments_are_fine():
    assert _readability_issues("Wait. Fable is getting into Max plans.") == []
