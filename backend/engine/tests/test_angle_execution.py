"""Does the draft actually DO the thing its angle says it does?

angle_execution is worth 11 points — joint-largest in the rubric — and it was
measuring vocabulary overlap against six literal phrases per angle. Across the
157 drafts stored on this machine, 94 scored the exact floor (3.85/11), and
sampling them showed why: "GPT-5.6 Sol outperforms Fable 5 in coding, but costs
less" is a direct comparison by any reading and contained none of "same level /
better than / worse than / vs / compared to / not even close".

The floor still matters and must keep working. A draft that claims
`direct_comparison` while comparing nothing has not executed its angle, and
scoring it low is the point — that is the term telling us the model labelled
its own output wrongly.
"""
from __future__ import annotations

import pytest

from app.growth_scorer import _angle_execution

FLOOR = 0.35


def earned(text: str, angle: str) -> float:
    """Points out of the 11 this term is worth."""
    return _angle_execution(text, angle) * 11.0


@pytest.mark.parametrize("text", [
    "GPT-5.6 Sol outperforms Fable 5 in coding, but costs less.",
    "I still prefer GPT-5.6's real-time feedback. It's faster.",
    "Opus 5 undercuts GPT-5 on price and matches it on coding.",
    "Fable 5 vs Opus 5 is the only benchmark that matters.",
    "Claude is cheaper than GPT-5 for the same workload.",
])
def test_a_real_comparison_scores_above_the_floor(text: str) -> None:
    assert earned(text, "direct_comparison") > FLOOR * 11.0 + 0.01, text


@pytest.mark.parametrize("text", [
    "Exciting times ahead in AI!",
    "This is huge for everyone.",
    "Claude Code's security scan is a big deal.",
])
def test_claiming_a_comparison_without_making_one_stays_at_the_floor(text: str) -> None:
    """The term's real job: catching a draft that did not do what it claimed."""
    assert earned(text, "direct_comparison") == pytest.approx(FLOOR * 11.0), text


def test_a_dated_call_executes_a_prediction():
    assert earned("Opus 5 lands before October. Calling it now.",
                  "exact_prediction") > FLOOR * 11.0
    assert earned("My Opus 5 prediction: July 30.",
                  "exact_prediction") > FLOOR * 11.0


def test_cost_language_executes_the_money_angle():
    assert earned("At $20 a month the maths stops working for small teams.",
                  "money_compute") > FLOOR * 11.0


def test_an_unknown_angle_is_neutral_not_punished():
    """Enhance mode assigns no angle; that must not read as a failed one."""
    assert _angle_execution("anything at all", None) == 0.6
