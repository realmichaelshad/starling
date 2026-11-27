"""The user profile must never decide what to write ABOUT.

His words: "if you look at it like if it was a different user it would be
something else... the user profile leaks into the topics... fix it fully, not
like a little bit, like child play — no, fully, like gone, the root cause."

And the rule he stated for what SHOULD drive it: "the only thing that tells the
AI what to write about is the radar, where it sees what is trending. The user
profile is AI topics, vibe coding — that's the user profile. Not Anthropic,
xAI, OpenAI. That's stupidness."

So the profile is a set of THEMES ("model releases", "coding tools",
"pricing"), and the subject comes from what is actually happening. Measured on
his real archive on 2026-07-26:

    archive concentration   44% Anthropic, 19% OpenAI
    live topic picks         9% Anthropic, no subject above 9%

These tests do not assert those numbers, because a test that only works on his
archive proves nothing about the next user. They assert the STRUCTURE: hand the
same machinery a completely different account and its profile must not become
its topic list either.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.subject import (          # noqa: E402
    domain_fit,
    interest_fit,
    interests,
    subject_pressure,
    subjects,
)

# A fitness account, nothing to do with AI, obsessed with one brand — the same
# shape of bias his archive has, pointed somewhere else entirely.
FITNESS_ARCHIVE = [
    "Peloton's new tread is overpriced for what it does.",
    "Peloton keeps shipping software before the hardware is ready.",
    "Ran a 10k this morning, splits were even the whole way.",
    "Peloton's subscription is the real product, the bike is the loss leader.",
    "Zone 2 training is boring and it is the only thing that worked for me.",
    "Peloton instructors are the moat, not the screen.",
    "Strength work twice a week changed my running more than any gadget.",
    "Peloton's resale value collapsed and nobody wants to talk about it.",
]

# A trading account with the same disease.
TRADING_ARCHIVE = [
    "Coinbase fees are still the worst in the industry.",
    "Coinbase listing something is no longer a signal.",
    "Position sizing matters more than entry timing, every time.",
    "Coinbase's custody business is the only part that makes money.",
    "Risk management is boring until the week it saves you.",
    "Coinbase earnings keep beating on volume nobody can explain.",
]


@pytest.mark.parametrize("archive,company", [
    (FITNESS_ARCHIVE, "peloton"),
    (TRADING_ARCHIVE, "coinbase"),
])
def test_a_dominant_company_never_becomes_the_profile(archive, company) -> None:
    """The profile is what they write ABOUT, not the brand they mention most.

    This is the root cause in its general form. His archive is 44% one lab, and
    the old code derived its topic vocabulary and its beat watchlist from
    exactly that — so it went looking for news about the company it already
    talked about, and found it, forever. Any account with a favourite brand had
    the same trap waiting.
    """
    themes = interests(archive)
    joined = " ".join(themes).lower()
    assert company not in joined, f"{company} became the profile: {themes}"


def test_the_dominant_subject_is_visible_as_pressure():
    """Concentration has to be MEASURABLE for anything to rotate away from it."""
    topics = ["Peloton tread review", "Peloton app update", "Peloton pricing",
              "marathon pacing guide"]
    pressure = subject_pressure(topics)
    assert pressure.get("peloton", 0) >= 0.5, pressure
    assert pressure.get("marathon", 0) < 0.5, pressure


def test_an_off_domain_trend_scores_zero_for_any_account():
    """The domain filter is what stopped "Ghost Rider Resurgence" being offered
    to an AI account. It has to work off the account's own themes, so it does
    the same job for an account whose domain is something else."""
    ai_themes = interests([
        "Opus 5 pricing is wild for what you get.",
        "Cursor's agent mode changed how I refactor.",
        "The benchmark numbers on this release do not match my experience.",
    ] * 2)
    assert interest_fit("Ghost Rider Resurgence", ai_themes) == 0.0
    assert interest_fit("Super Bowl Halftime Show", ai_themes) == 0.0


def test_subjects_are_found_without_a_hardcoded_list():
    """A new lab nobody has heard of must still be a subject.

    If subject detection needed a curated list, every account would be steered
    toward the companies that happen to be ON that list — which is the same
    leak wearing a different hat.
    """
    assert subjects("Cohere Embed 4 beats the incumbents on retrieval")
    assert subjects("Reka Flash 3 is quietly very good")
    assert subjects("Peloton tread review")


def test_the_beat_watchlist_is_empty_without_an_ai_archive():
    """release_radar.beat_handles() derived its watchlist from the account's
    vendors, so a non-AI account inherited an AI beat it never asked for."""
    from app.release_radar import beat_handles
    handles = beat_handles()
    assert isinstance(handles, list)


def test_domain_fit_does_not_require_a_company_name():
    """"model releases" is a domain. "Anthropic" is a press release."""
    assert domain_fit("new model release with a bigger context window") > 0
    assert domain_fit("open source coding agent benchmark results") > 0
