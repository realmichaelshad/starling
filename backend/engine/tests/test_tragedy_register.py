"""The human-harm gate must survive his register.

He showed Starling an ordinary post and was told it was dangerous. The flat
tragedy word list blocked 20 of the 23 sentences below — "the fatal flaw in
this benchmark", "just rip out the old retrieval layer", "print media is
dying", "I'm shooting for sub-100ms p99" — each with *People were hurt here.
This is not a thread to work an angle in.*

The bar, from the same rule every other gate here is held to: ZERO false
positives on the figurative battery, ZERO misses on the real-tragedy battery.
Tightening the trigger must not soften the response — anything that clears
the bar is still a hard block.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.safety import assess_context  # noqa: E402
from app.schemas import PostContext  # noqa: E402


# Ordinary posts in his register that use a tragedy word figuratively.
FIGURATIVE = [
    "The fatal flaw in this benchmark is that it leaks the eval set into pretraining.",
    "Just rip out the old retrieval layer, it's costing you 400ms per call.",
    "Let it rip — ship the beta and let the usage data tell you what's broken.",
    "RIP to my weekend, this migration is going to take all of it.",
    "Print media is dying and everyone in this thread knows it.",
    "The death of the SaaS pricing model has been predicted every year since 2019.",
    "Moore's law died years ago, we just kept renaming what we measure.",
    "Shipping on a Friday is career suicide and yet here we are.",
    "The first casualty of a rushed launch is always the docs.",
    "An overdose of synthetic data is why these models feel same-y now.",
    "This release is an earthquake for anyone building on the old API.",
    "Feast or famine with GPU availability right now.",
    "They're holding the whole roadmap hostage over one dependency.",
    "I'm shooting for sub-100ms p99 on this endpoint.",
    "You're shooting yourself in the foot by caching the embeddings that long.",
    "I completely bombed that live demo, the model timed out on stage.",
    "The hype around agents died down fast once people tried to ship one.",
    "Deaths of small startups in this space are mostly self-inflicted.",
    "Startup death rate in AI infra is wild — most never find a second use case.",
    "That pricing page is a fatality for their enterprise motion.",
    # The original "dead" carve-out — a prediction-market odds post.
    "The launch odds went from 80% to basically dead by lunch.",
    "This model is a scam at that price point.",
    "Attention is dying as a moat; distribution is the real one.",
    # An ordinary-sounding co-signal next to a figurative tragedy word. These
    # are what the ambiguous list costs if the corroborating signal is loose.
    "We had 12 people in the room and the demo was a disaster.",
    "At least 30 people asked me about the pricing change today.",
    "Officials said the merger collapsed after the funding fell through.",
    "Officials said the attack affected 40,000 accounts, no data was taken.",
    "Their family of models is finally competitive on long-context retrieval.",
    "The rescue plan for this migration is just to roll back and try Monday.",
    "Missing docs are the real reason nobody adopts this SDK.",
    "My inbox is under siege since that post went out.",
    "Fire off a request to the batch endpoint and check the latency.",
    "The whole eval harness crashed halfway through the run.",
]

# Every one of these must still be a hard stop.
REAL_TRAGEDY = [
    "At least 40 people have died after a bridge collapse in the capital.",
    "Breaking: 12 dead, dozens injured in this morning's shooting.",
    "A gunman opened fire at the mall; three were found dead at the scene.",
    "Funeral services for the victims will be held Sunday. Condolences to the family.",
    "He was pronounced dead at the hospital following the crash.",
    "The earthquake has killed hundreds and rescue crews are still digging.",
    "Rest in peace to everyone lost in the wildfire this week.",
    "Reports of war crimes are emerging from the occupied region.",
    "She died by suicide last night. Please check on your friends.",
    "The plane crash left no survivors, according to officials.",
    # No tier-one phrase: an ambiguous word plus real human context.
    "Three people died in the apartment fire last night.",
    "The death toll from the flooding has risen to 89.",
    "A stabbing outside the station left two injured and one dead.",
    "Devastating news about the crash. My thoughts are with their family.",
    "He took his own life. If you're struggling, please call a crisis line.",
    "Officials said at least 20 people are missing after the building fire.",
    "Rescue crews are still searching the rubble for survivors.",
    "The overdose crisis killed hundreds in this county alone last year.",
    "Two children were wounded in the shooting near the school.",
    "Hundreds of families were evacuated as the wildfire reached the ridge.",
]


def _assess(text: str):
    return assess_context(PostContext(text=text, author="someone"))


@pytest.mark.parametrize("text", FIGURATIVE)
def test_figurative_tragedy_words_are_not_a_tragedy(text: str) -> None:
    result = _assess(text)
    assert not result.blocked, (
        f"blocked an ordinary post as {result.categories}: {text}")


@pytest.mark.parametrize("text", REAL_TRAGEDY)
def test_real_tragedies_are_still_a_hard_stop(text: str) -> None:
    result = _assess(text)
    assert result.blocked, f"let a real tragedy through: {text}"
    assert "human_harm" in result.categories
    assert result.severity == "critical"
