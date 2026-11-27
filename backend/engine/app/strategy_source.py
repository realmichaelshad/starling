"""Structured drafting guidance distilled from the bundled X craft manual.

This compact profile is the durable runtime form of the manual
(`app/data/strategy_v2.txt`): it keeps enhancement prompts small while retaining
the rules that matter for a live composer. When the manual itself is available,
its per-action brief leads and these constants follow as the guaranteed fallback.
"""

from __future__ import annotations

from typing import Literal


SOURCE = {
    "title": "Starling X Craft Manual v2",
    "sha256": "83251A7069545E20054B259633398C9A3687023632986AA8EFACD8B57C42536A",
    "mode": "private_local_strategy",
}

COMMON_RULES = (
    "Preserve the writer's actual point, casual rhythm, and level of confidence. "
    "Correct accidental typos and tighten wording without turning it corporate. "
    "Be specific, useful, and defensible; do not invent facts, fake precision, "
    "engagement bait, forced controversy, harassment, or promises of virality."
)

VOICE_RULES = (
    "Favor immediate, personal, clear reactions over formal mini-essays. "
    "Keep the writer's natural slang, humor, directness, and occasional edge when it is already present; "
    "do not manufacture a persona."
)

CREDIBILITY_RULES = (
    "Keep factual claims proportionate to visible evidence. Do not turn a comparison, prediction, benchmark, "
    "or number into a stronger claim than the writer can support."
)

TIMING_AND_LEARNING_RULES = (
    "For an explicit Enhance request, improve the draft now rather than telling the writer to wait. "
    "Use timing, format, topic, recency, and visible performance only as directional local evidence; update the lesson "
    "after later visible metrics instead of treating a single result as proof."
)

ACTION_RULES: dict[str, tuple[str, ...]] = {
    "reply": (
        "Replies earn borrowed distribution only when they add a clear reaction, comparison, correction, joke, or useful missing point.",
        "Keep it quick to read and strong enough for people other than the parent author to react to.",
    ),
    "quote": (
        "Use the quoted source as essential context, then add an independent point rather than repeating it.",
        "A quote must stand on its own in the feed and should not be a rescue format for a weak idea.",
    ),
    "post": (
        "A standalone post should make one original, recognizable point that gives the right audience a reason to follow.",
        "Lead with the clearest tension, observation, prediction, or useful interpretation, not generic news paraphrase.",
    ),
}


def enhancement_guidance(action: Literal["reply", "quote", "post"]) -> dict[str, object]:
    """Return only the rules relevant to one immediate composer enhancement.

    When the full strategy manual is available (strategy_v2), its per-action
    brief leads; the hand-distilled constants below remain the guaranteed
    fallback so the guidance never goes empty.
    """
    from .strategy_v2 import brief_for  # local import: strategy_v2 reads files lazily

    rules: list[str] = []
    manual_brief = brief_for(f"enhance_{action}")
    if manual_brief:
        rules.append(manual_brief)
    rules.extend([COMMON_RULES, VOICE_RULES, CREDIBILITY_RULES, TIMING_AND_LEARNING_RULES, *ACTION_RULES[action]])
    return {
        "source": SOURCE,
        "task": "Enhance a user-authored draft that is already headed for publication.",
        "rules": rules,
        "output_contract": "Return one polished draft only; never tell the user to wait or change their chosen format.",
    }
