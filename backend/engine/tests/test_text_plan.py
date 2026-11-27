"""The Writer's plan and editor (§8.3f-§8.3h).

Two behaviours matter most: a plan missing a required field must be returned
upstream rather than papered over with a louder hook, and a draft that fails a
hard check twice must be labelled `below-bar` with the missing ingredient named
rather than presented as optimised.
"""
from __future__ import annotations

import pytest

from app import text_plan as tp


def _plan(**kw) -> tp.TextPlan:
    base = dict(reader="People who run local inference",
                point="Batch economics changed this quarter",
                proof="Measured 43 tok/s on a consumer GPU",
                payoff="They can stop paying per call",
                primary_action="distribution", fmt="post")
    base.update(kw)
    return tp.TextPlan(**base)


# --------------------------------------------------------------------------- #
# §8.3f the plan
# --------------------------------------------------------------------------- #

def test_a_complete_plan_reports_nothing_missing():
    assert _plan().complete and _plan().missing == []


@pytest.mark.parametrize("field", tp.REQUIRED_FIELDS)
def test_each_required_field_is_actually_required(field):
    plan = _plan(**{field: None})
    assert not plan.complete
    assert field in plan.missing


def test_the_plan_renders_every_field_for_the_prompt():
    block = _plan().as_prompt_block()
    for label in ("READER:", "POINT:", "PROOF:", "PAYOFF:", "PRIMARY ACTION:", "FORMAT:"):
        assert label in block


def test_build_plan_invents_nothing_when_evidence_is_absent():
    """A field filled with plausible text is exactly how a writer ends up
    compensating for missing evidence."""
    class BareAnalysis:
        audience_forecast = {}
        angles = []
        penalties = []
        summary = "something"

    class BareDecision:
        primary_objective = None
        action = "post"
        risks = []

    plan = tp.build_plan(BareDecision(), BareAnalysis())
    assert not plan.complete
    assert "point" in plan.missing and "proof" in plan.missing


def test_build_plan_carries_the_prohibited_tactics_into_do_not():
    class Analysis:
        audience_forecast = {"core": "readers", "conversion_bridge": "the archive"}
        angles = [{"why": "a distinction exists", "kind": "clear_distinction"}]
        penalties = []
        summary = "evidence"

    class Decision:
        primary_objective = "distribution"
        action = "post"
        risks = []

    plan = tp.build_plan(Decision(), Analysis())
    assert plan.do_not, "the writer must be told what it may not do"
    assert plan.archetype == "clear_distinction"


# --------------------------------------------------------------------------- #
# §8.3h the sentence editor
# --------------------------------------------------------------------------- #

def test_engagement_begging_is_cut():
    result = tp.edit_sentences(
        "Local inference now costs less than the API for batch work. Like if you agree!")
    assert "Like if you agree" not in result["text"]
    assert any("engagement" in item["reason"] for item in result["removed"])


def test_self_important_filler_is_cut():
    result = tp.edit_sentences(
        "Batch inference costs dropped by half this quarter. This changes everything.")
    assert "changes everything" not in result["text"].lower()


def test_manufactured_urgency_is_cut():
    result = tp.edit_sentences(
        "The pricing shift is real and measurable now. Act now before it's too late.")
    assert "act now" not in result["text"].lower()


def test_a_sentence_that_only_repeats_the_claim_is_cut():
    result = tp.edit_sentences(
        "Local inference is cheaper than cloud for batch work. "
        "Local inference is cheaper than cloud for batch work.")
    assert len(tp._sentences(result["text"])) == 1


def test_sentences_doing_a_real_job_survive():
    text = ("Batch inference dropped below cloud pricing this quarter. "
            "We measured 43 tokens per second on a consumer GPU. "
            "That means small teams can stop paying per call.")
    result = tp.edit_sentences(text)
    assert len(tp._sentences(result["text"])) == 3
    assert result["changed"] is False


def test_an_all_filler_draft_is_returned_unchanged_for_review():
    """Editing a draft down to nothing is worse than returning it."""
    result = tp.edit_sentences("This changes everything. Let that sink in.")
    assert result["text"].strip()
    assert "unchanged" in (result.get("note") or "")


# --------------------------------------------------------------------------- #
# §8.3g format programs
# --------------------------------------------------------------------------- #

def test_a_reply_must_connect_to_the_parent_claim():
    problems = tp.check_format("Completely unrelated musings about sourdough.",
                               "reply", parent_text="Inference pricing changed again")
    assert any("parent claim" in p for p in problems)


def test_a_quote_that_leans_on_the_embed_is_rejected():
    problems = tp.check_format("This is exactly right and worth reading.", "quote")
    assert any("depends on the quoted post" in p for p in problems)


def test_a_post_leading_with_a_link_is_rejected():
    problems = tp.check_format("https://example.com/post and some words after it",
                               "post")
    assert problems


def test_a_conforming_post_has_no_format_problems():
    assert tp.check_format(
        "Batch inference dropped below cloud pricing, which changes the build-versus-buy call.",
        "post") == []


# --------------------------------------------------------------------------- #
# §8.3h hard checks and banding
# --------------------------------------------------------------------------- #

GOOD = ("Batch inference dropped below cloud pricing this quarter. "
        "We measured 43 tokens per second on a consumer GPU, which means "
        "small teams can stop paying per call.")


def test_a_good_draft_is_post_ready():
    result = tp.band_for(GOOD, _plan(), parent_text="inference pricing")
    assert result["failed"] == []
    assert result["band"] == "post-ready"


def test_unsupported_certainty_fails_truth_and_names_the_fix():
    result = tp.band_for("This is guaranteed to work for every single team, always.",
                         _plan())
    assert "truth_and_attribution" in result["failed"]
    assert result["missing_ingredient"]


def test_bait_fails_action_integrity():
    result = tp.band_for("Local models are great. Like if you agree, follow me for more!",
                         _plan())
    assert "action_integrity" in result["failed"]


def test_hashtags_fail_x_presentation():
    result = tp.band_for(GOOD + " #AI #MachineLearning", _plan())
    assert "x_presentation" in result["failed"]


def test_a_payoff_behind_a_link_fails_native_payoff():
    result = tp.band_for("https://example.com/x", _plan())
    assert "native_payoff" in result["failed"]


def test_a_failing_draft_is_below_bar_and_says_what_is_missing():
    """Never dressed up as optimised (§8.3h)."""
    result = tp.band_for("Thoughts?", _plan())
    assert result["band"] == "below-bar"
    assert result["missing_ingredient"], "the specific missing ingredient must be named"


def test_an_incomplete_plan_caps_the_band_below_post_ready():
    result = tp.band_for(GOOD, _plan(proof=None), parent_text="inference pricing")
    assert result["band"] == "workable"
    assert "proof" in result["plan_missing"]


def test_all_seven_hard_checks_run():
    result = tp.band_for(GOOD, _plan())
    assert set(result["checks"]) == set(tp.HARD_CHECKS)
