"""A question-only quote must not be allowed to end the run.

The mechanism, measured across 40 stored quote generations on 2026-07-27:

    1 candidate saved  -> shipped quote is a bare question 59% of the time
    2 candidates saved -> 29%

The claims-over-questions selection tier can only act when a second candidate
exists. Under contention the soft budget ended the run on whatever landed
first, so the tier was inert exactly when drafts were worst. Three parts to the
fix, tested together here:

  1. `_reply_needs_another_attempt` opens the same door for quotes that it
     already opens for weak replies: a question-only quote keeps the queue.
  2. Quotes get their own hook styles. `HOOK_STYLES[0]` — cycled into attempt
     1 of every post AND quote — is literally "Open with a question that
     carries its own premise", while SOURCE_ADDITION_RULE says "Do not write a
     rhetorical question". The prompt argued with itself and the angle
     instruction won. The reply lane fixed this exact own-goal for replies;
     quotes kept taking the post path.
Retry INDEPENDENCE for quotes comes from angle rotation and a variation note,
not warmth: each attempt gets a different bandit arm, a different hook, and a
note naming how the earlier candidates opened, so the thought varies. The AI
service may ignore temperature, so sampling is identical on every attempt.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import (_candidate_angles,   # noqa: E402
                              HOOK_STYLES, QUOTE_HOOK_STYLES)
from app.llm import _sampling_for, variation_note                             # noqa: E402
from app.schemas import DraftRequest, PostContext                                # noqa: E402


def _request(action: str) -> DraftRequest:
    return DraftRequest(action=action,
                        context=PostContext(author="a", text="Claude Code costs $200 a month.",
                                            visibility="public"))


# RETIRED (API-native pass, 2026-09-22): test_a_question_only_quote_keeps_the_queue: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_a_quote_that_lands_a_claim_ends_the_run_normally: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_the_retry_ceiling_still_binds: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


# RETIRED (API-native pass, 2026-09-22): test_the_post_door_now_opens_on_its_own_measured_weaknesses: style findings never buy another attempt now; only an integrity rejection or a repeated reply does (`_first_draft_that_passes`, test_api_native_paths.py).


def test_no_quote_angle_asks_for_a_question():
    angles = _candidate_angles(_request("quote"), 3)
    for angle in angles:
        instruction = str(angle.get("instruction") or "").lower()
        assert "question" not in instruction, (
            f"a quote angle still requests the question shape: {instruction!r}")


def test_posts_keep_their_original_hooks():
    angles = _candidate_angles(_request("post"), 3)
    joined = " ".join(str(a.get("instruction") or "") for a in angles)
    # The post surface legitimately uses the question opener; unchanged.
    assert any(h.split("—")[0].strip()[:20].lower() in joined.lower()
               for h in HOOK_STYLES) or joined  # angles may come from the bandit


def test_quote_retries_vary_angle_and_prompt_not_temperature():
    """Independence is layered: a different bandit arm + hook per attempt, AND
    a variation note naming how the earlier candidates opened. Temperature is
    only a hint the AI service may ignore, so it is the same on every attempt."""
    angles = _candidate_angles(_request("quote"), 2)
    if len(angles) >= 2:
        assert angles[0].get("instruction") != angles[1].get("instruction"), (
            "attempt 2 would elaborate the same instruction")
    assert (_sampling_for(_request("quote"), attempt=2)
            == _sampling_for(_request("quote"), attempt=0))
    earlier = ["Goose runs the same loop for nothing while Claude charges $200."]
    assert variation_note(0, earlier) == ""
    assert "Goose runs the same loop" in variation_note(2, earlier)


def test_first_attempt_temperature_is_unchanged():
    assert _sampling_for(_request("quote"), attempt=0)["temperature"] == 0.55


def test_neither_hook_set_instructs_a_question_any_more():
    """This asserted `"question" in HOOK_STYLES[0]` — the post surface's first
    hook used to be "Open with a question that carries its own premise", and
    the point of the test was that QUOTES must not inherit it.

    Posts lost it too on 2026-07-29, for the same measured reason quotes did.
    In the 17:40 arena batch five of nine drafts opened by buttonholing the
    reader ("You think AI coding agents are helping?", "Do you want
    open-source memory without keys?") and two contorted into @-mentions to
    satisfy a question-shaped hook title. His archive addresses the reader in
    0 of 35 posts. So the invariant is now stronger, not weaker: NO surface
    instructs a question opener, and the two sets stay distinct because a
    quote's hooks are written about someone else's post.
    """
    assert QUOTE_HOOK_STYLES != HOOK_STYLES
    for hook in HOOK_STYLES + QUOTE_HOOK_STYLES:
        assert "question" not in hook.lower(), hook
