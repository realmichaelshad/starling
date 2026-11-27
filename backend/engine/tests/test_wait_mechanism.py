"""The ✦ decide verdict on realistic timeline posts.

Measured before any of this session's changes, this battery scored 8/15. The
seven wrong verdicts were two distinct failures:

  FALSE WAIT — four substantive posts came back "I could not find a point worth
  adding to this one", terminal, because the angle search knew how to recognise
  a prediction, a comparison, a digit or a long post and nothing else. None of
  the four contains a digit or any of its fifteen keywords.

  FALSE ACT — an ad drew a reply (the promo detector counted alternations
  instead of signals, so three ad markers scored one hit), and engagement bait
  drew "post your own instead" off POST's constant floor.

These are end-to-end through analyze() -> decide(), both deterministic, so the
verdict here is the verdict on the card.
"""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.analyzer import analyze  # noqa: E402
from app.decider import decide  # noqa: E402
from app.schemas import PostContext  # noqa: E402


def _verdict(text: str, *, age_minutes: float = 10, views: int = 20000,
             replies: int = 18, quotes: int = 4):
    created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    context = PostContext(
        text=text, author="someone", author_handle="someone",
        post_created_at=created.isoformat(),
        visible_metrics={"views": views, "replies": replies, "quotes": quotes,
                         "likes": views // 50})
    analysis = analyze(context, profile=None, recent_user_texts=[])
    return decide(analysis, handle="someone", cadence_pressure=0.0), analysis


# Substantive posts that produced a TERMINAL wait. Each is an argument with an
# exposed joint — a mechanism, a boundary, a contested norm, a sweep — and each
# carries no digit and none of the original keywords.
ARGUMENT_SHAPES = [
    pytest.param(
        "Nice writeup — but the batching numbers only hold if you keep the KV "
        "cache resident. On a shared GPU you lose most of that gain to eviction.",
        id="boundary_condition"),
    pytest.param(
        "First principles on agent memory: you do not need a vector DB until "
        "your context no longer fits. Most teams add one about a year early.",
        id="contested_norm"),
    pytest.param(
        "The airdrop meta is dead because the incentive design rewards sybils, "
        "not users. Every protocol that tried it got mercenary liquidity.",
        id="stated_mechanism"),
    pytest.param(
        "OpenAI shipped structured outputs for the realtime API today. That "
        "kills about half the glue code in every voice agent I have seen.",
        id="sweeping_claim"),
]


@pytest.mark.parametrize("text", ARGUMENT_SHAPES)
def test_an_argument_with_a_joint_is_repliable(text: str) -> None:
    decision, analysis = _verdict(text)
    assert analysis.angles, "no angle found in a post that plainly makes a claim"
    assert decision.action != "wait", (
        f"terminal wait on a substantive post: {decision.why_now}")


@pytest.mark.parametrize("text", ARGUMENT_SHAPES)
def test_and_is_never_a_terminal_wait(text: str) -> None:
    """Terminal means he cannot override it, so a false one is unrecoverable."""
    decision, _ = _verdict(text)
    assert not decision.terminal


def test_an_ad_is_not_a_reply_target_and_not_a_reason_to_post() -> None:
    """Three ad markers used to count as one hit, so this drew a real reply.

    POST must not win either: "This belongs in a post of your own" asserts he
    has something to write, off a score the ad never influenced.
    """
    decision, analysis = _verdict(
        "My course on shipping AI products is live. Link in bio. "
        "First 50 people get 40% off.")
    assert analysis.target_quality == "ad"
    assert decision.action == "wait"
    assert any("ad" in line.lower() for line in decision.why_now)


def test_engagement_bait_is_not_a_reply_target() -> None:
    """The year in it counted as a number worth questioning."""
    decision, analysis = _verdict(
        "Like if you think AGI arrives before 2030. Repost if you disagree.")
    assert analysis.target_quality == "bait"
    assert decision.action == "wait"


def test_a_thin_post_still_waits() -> None:
    decision, _ = _verdict("gm", views=200, replies=1, quotes=0)
    assert decision.action == "wait"


def test_a_tragedy_is_still_a_hard_block() -> None:
    decision, _ = _verdict(
        "At least 40 people have died after a bridge collapse in the capital.")
    assert decision.action == "wait"
    assert decision.blocked and decision.terminal


def test_a_closed_reply_window_is_named_even_when_the_verdict_is_act() -> None:
    """The analyzer knew the conversation had moved on and the card dropped it.

    POST winning here is defensible. Not telling him the reply window shut a day
    ago, while recommending he write something, is not.
    """
    decision, analysis = _verdict(
        "Shipped a small refactor of our embedding cache today. "
        "Down to 40ms p99 from 120ms.",
        age_minutes=60 * 26, views=9000, replies=6, quotes=0)
    assert analysis.reply_surface == "weak"
    assert decision.action != "reply"
    # THE PROPERTY, NOT THE OLD WORDING. This asserted `"moved on" in line`
    # until 2026-08-09; that sentence was reached from age alone and claimed
    # the thread had died on posts whose last reply was 45 minutes old, so it
    # is gone for good. What the test is FOR survives unchanged: a card that
    # recommends acting elsewhere must still tell him the reply window shut.
    #
    # The failure this test produced was itself misleading — it reported the
    # reason "computed and discarded" when the reason was present and its
    # CONTENT had changed. Assert the fact, and print the lines either way.
    # PLUMBING: the docstring's actual claim — computed, then carried. Immune
    # to rewording, and it is what the original defect broke. Verified stable
    # here: on the act path `decider.py:507` PREPENDS the reason at index 0
    # after the :493 assignment, and nothing truncates after it. The `[:2]` cut
    # at :459 is the WAIT path, which this case never takes.
    assert analysis.reply_surface_reason, "the reply-surface reason was never computed"
    assert analysis.reply_surface_reason in decision.why_now, (
        f"the reason was computed and then dropped: {decision.why_now}")
    # CONTENT: and it must still say the window shut. A plumbing-only assertion
    # passes a reason that is present and says nothing — which is exactly the
    # regression this test caught on 2026-08-09.
    why = " ".join(decision.why_now).lower()
    assert "reply window" in why and "closed" in why, (
        f"the closed reply window was not named on an act verdict: {decision.why_now}")
    assert "almost certainly" not in why, (
        f"no confidence adverb about a thread nobody saw: {decision.why_now}")


@pytest.mark.parametrize("text", [
    "Everyone benchmarking agents on SWE-bench is measuring the harness, not "
    "the model. Swap the scaffold and the same model moves 15 points.",
    "Genuine question for people running local models: what are you getting "
    "for tokens/sec on a 4090 with a 32B at 8-bit?",
    "Context windows are basically solved. 1M tokens means you never need RAG.",
])
def test_clearly_repliable_posts_are_unaffected(text: str) -> None:
    decision, _ = _verdict(text)
    assert decision.action == "reply"
