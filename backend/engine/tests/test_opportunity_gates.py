"""The second wait mechanism: opportunities.assess_opportunity.

Feeds /strategy/next and the format hint on the create path, with its own
low-value / promotional / current-claim rules — separate from the ones the ✦
decide button uses. Measured at 4/11 on realistic posts. All three rules failed
the same way: they matched on a SUBSTRING or a PREFIX and could not tell a
post's opening word from its substance.

  prefix    "Nice writeup — but the batching numbers only hold if you keep the
             KV cache resident" -> "mostly a low-substance greeting"
  substring "The airdrop meta is dead because the incentive design rewards
             sybils" -> "primarily promotional"
  substring "OpenAI shipped structured outputs today" and "new evals show a 34%
             drop" -> a hard research gate, score 0.0, drafting refused,
             because "today" and "%" were current-claim signals
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.opportunities import assess_opportunity
from app.schemas import PostContext

NOW = datetime.now(timezone.utc)


def _action(text: str) -> str:
    context = PostContext(
        text=text, author="someone", author_handle="someone",
        post_created_at=(NOW - timedelta(minutes=12)).isoformat(),
        visible_metrics={"views": 20000, "replies": 18, "quotes": 4})
    return assess_opportunity(context, NOW)["action"]


SUBSTANTIVE = [
    pytest.param(
        "Nice writeup — but the batching numbers only hold if you keep the KV "
        "cache resident. On a shared GPU you lose that to eviction.", id="nice"),
    pytest.param(
        "First principles on agent memory: you do not need a vector DB until "
        "your context no longer fits. Most teams add one a year early.", id="first"),
    pytest.param(
        "Agree with the framing but the numbers here are from a single run — "
        "variance on this eval is about 8 points between seeds.", id="agree"),
    pytest.param(
        "The airdrop meta is dead because the incentive design rewards sybils, "
        "not users. Every protocol that tried it got mercenary liquidity.", id="airdrop"),
    pytest.param(
        "The giveaway mechanic is why these launch numbers mean nothing — you "
        "are measuring bot registrations, not demand.", id="giveaway"),
    pytest.param(
        "OpenAI shipped structured outputs for the realtime API today. That "
        "kills half the glue code in every voice agent I have seen.", id="today"),
    pytest.param(
        "New evals show a 34% drop in tool-calling accuracy once you go past "
        "12 tools in the schema. This breaks most agent stacks.", id="percent"),
]


@pytest.mark.parametrize("text", SUBSTANTIVE)
def test_an_opening_word_is_not_the_whole_post(text: str) -> None:
    assert _action(text) in ("reply", "quote"), (
        f"skipped a substantive post: {text[:60]}…")


@pytest.mark.parametrize("text", [
    "gm",
    "so true",
    "Nice one, congrats on the launch!",
    "Agree 100%, this is the way.",
])
def test_a_post_that_is_only_a_reaction_is_still_skipped(text: str) -> None:
    assert _action(text) == "wait"


@pytest.mark.parametrize("text", [
    "My course on shipping AI products is live. Link in bio. First 50 people "
    "get 40% off before Friday.",
    "Claim your airdrop now — link in bio, first 5000 wallets only.",
])
def test_actual_ad_copy_is_still_skipped(text: str) -> None:
    assert _action(text) == "wait"


@pytest.mark.parametrize("text", [
    "Breaking: the new model beats o5 on every benchmark, according to "
    "internal data seen by two people familiar with the results.",
    "A new study finds that 61% of enterprise agent deployments were rolled "
    "back within six months.",
])
def test_second_hand_reporting_still_needs_verifying(text: str) -> None:
    """Narrowing the trigger must not remove the gate it was carved out of."""
    assert _action(text) == "research"
