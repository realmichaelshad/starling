"""The post has to deliver a point a reader can carry away.

He flagged this draft as not landing:

    "Opus 5 is cheaper and less restrictive than Fable 5. It launched two
     months after Opus 4.8. Why does this matter? Because Anthropic's
     guardrails are finally MATCHING the speed of their releases. They're NOT
     KEEPING UP with the demand for more open models."

Two separate faults, and neither fires on any of his 34 published posts:

  explainer scaffolding  "Why does this matter? Because..." is blog structure.
                         He writes "Why?" then evidence — never the
                         question-and-answer form, which reads like a model
                         narrating its own reasoning.
  self-contradiction     it affirms an idea and then denies it, so there is no
                         point left to take away.

A first attempt measured whether the closing line shared vocabulary with the
opening. That was WRONG and the archive said so: his own posts score a median
0.00 on it, because a punchy closing line full of new words is exactly his
style. Recorded here so nobody re-derives it.
"""
from __future__ import annotations

import pytest

from app.intelligence import _contradicts_itself, _draft_quality_issues
from app.schemas import DraftRequest, PostContext

COHERENCE = {"explainer_scaffolding", "contradicts_itself"}
FLAGGED = ("Anthropic's Opus 5 is cheaper and less restrictive than Fable 5. It "
           "launched just two months after Opus 4.8. Why does this matter? "
           "Because Anthropic's guardrails are finally matching the speed of "
           "their releases. They're not keeping up with the demand for more "
           "open models.")


def issues(text: str) -> set[str]:
    request = DraftRequest(action="post",
                           context=PostContext(author="me", visibility="public", text=""))
    return set(_draft_quality_issues(text, request, [])) & COHERENCE


def test_the_draft_he_flagged_is_caught():
    assert issues(FLAGGED) == COHERENCE


@pytest.mark.parametrize("text", [
    "Opus 5 is cheaper. Why does this matter? Because the maths changes.",
    "Rate limits are the story. Here's why it matters: throughput is the cost.",
    "Fable 5 shipped early. In other words, the cadence is accelerating.",
])
def test_blog_scaffolding_is_caught(text: str) -> None:
    assert "explainer_scaffolding" in issues(text), text


def test_arguing_both_sides_is_caught():
    assert _contradicts_itself(
        "Their guardrails are finally matching the pace. They are not keeping "
        "up with demand.")
    assert _contradicts_itself(
        "Anthropic is catching up on cadence, but they can't keep up on price.")


def test_a_one_sided_claim_is_not_a_contradiction():
    assert not _contradicts_itself("Anthropic is finally matching OpenAI on cadence.")
    assert not _contradicts_itself("Anthropic is not keeping up with OpenAI.")


def test_none_of_his_published_posts_are_flagged():
    """The measure that matters: it must not reject how he actually writes."""
    from app.storage import performance_records
    rows = [str(r.get("text") or "") for r in performance_records() if r.get("text")]
    if not rows:
        pytest.skip("no archive in this environment")
    flagged = [t for t in rows if issues(t)]
    assert not flagged, f"{len(flagged)} of his own posts flagged: {flagged[:1]}"
