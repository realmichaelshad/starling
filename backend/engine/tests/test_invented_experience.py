"""Two failures that no number-checking gate can see.

Both were read off live output on 2026-07-26, after the topic supply was fixed
and the drafts finally had real subjects to write about:

    "Salesforce's Slackbot AI is in beta. It's meant to schedule meetings, but
     I saw it book one with no attendees."

The model has never opened Slack. That anecdote is invented whole, contains no
figure to check, and is the most damaging kind of error available — a reader
who tries to verify it finds a person who made something up.

Partly self-inflicted: reader_value scores firsthand signal UP, so a model with
nothing real to report manufactures some. An incentive to sound experienced is
an incentive to lie unless the lie is blocked.

    "Ask HN: Which is the least sloppy and claudeism free model you have used?
     the question hits right."
    "Why my open-source project hasn't done better people build with it..."

And this is the "delivers nothing, no reason to read" complaint in its purest
form: the draft opens by repeating the headline it was handed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import (            # noqa: E402
    _invented_experience,
    _parrots_the_headline,
)

EVIDENCE = ("Salesforce rolls out new Slackbot AI agent as it battles "
            "Microsoft Copilot for the enterprise.")


@pytest.mark.parametrize("draft", [
    "It's meant to schedule meetings, but I saw it book one with no attendees.",
    "I tested it for a week and the latency was awful.",
    "We ran it against our own suite and it fell over.",
    "I switched to it yesterday and haven't looked back.",
])
def test_a_fabricated_anecdote_is_blocked(draft: str) -> None:
    assert _invented_experience(draft, EVIDENCE), f"invented experience shipped: {draft}"


@pytest.mark.parametrize("draft", [
    "Scheduling is the wrong first feature for an agent nobody trusts yet.",
    "If you're building with it, the bottleneck is going to be permissions.",
    "This is the third assistant this quarter promising the same thing.",
])
def test_an_opinion_is_not_an_anecdote(draft: str) -> None:
    """Opinion, prediction and second-person framing are all still allowed.

    The gate blocks CLAIMED OBSERVATION, not conviction — his real voice is
    full of conviction and would be ruined by a rule that flinched at it.
    """
    assert not _invented_experience(draft, EVIDENCE), draft


def test_a_stranger_in_the_evidence_is_not_a_licence():
    """Two looser versions of this rule shipped and both failed.

    "allow it if the evidence contains a first-person phrase" — a Hacker News
    thread is full of strangers writing "I tried it", and on an Ask HN topic
    that let a draft through reading "I tried it. It still stutters on code."

    "allow it if HIS archive contains one" — his archive is his own writing, so
    that licensed everything.

    Neither escape survives. The model has not used the thing, and his
    experience of one product cannot vouch for a claim about another. The real
    exemption is enhancement mode, where the words are already his and this
    gate never runs at all.
    """
    hn = "Ask HN: I tried Opus 5 for a week and the latency was awful."
    assert _invented_experience("I tested it for a week. Still slow.", hn)


@pytest.mark.parametrize("draft", [
    "Salesforce rolls out new Slackbot AI agent as it battles rivals.",
    "Salesforce rolls out new Slackbot AI agent. Interesting move.",
])
def test_opening_with_the_headline_is_blocked(draft: str) -> None:
    assert _parrots_the_headline(draft, EVIDENCE), f"headline parroted: {draft}"


@pytest.mark.parametrize("draft", [
    "Scheduling is the wrong first feature for an agent nobody trusts yet.",
    "Copilot lost this category two years ago and nobody noticed.",
    "Another assistant, same demo, same three tasks.",
])
def test_a_real_take_is_not_parroting(draft: str) -> None:
    assert not _parrots_the_headline(draft, EVIDENCE), draft


def test_a_short_draft_is_never_judged_as_parroting():
    """Too few words to tell a repeat from a coincidence — say nothing."""
    assert not _parrots_the_headline("Salesforce again.", EVIDENCE)
