"""The algorithm knowledge layer, rebuilt on the CURRENT public release.

The previous version encoded weights from the 2023 heavy ranker ("a reply is
worth ~13x a like") and asserted them as fact in prompts and user-visible copy.
The current primary source reads every weight from runtime parameters
(`params.get(FavoriteWeight)`), so those numbers are not public and must never
be claimed. These tests exist mostly to keep them from coming back.
"""
from __future__ import annotations

import re

import pytest

from app import algorithm_model as am
from app.algorithm_model import algorithm_alignment, algorithm_brief, pipeline_brief


# --------------------------------------------------------------------------- #
# Honesty — the reason this module was rewritten
# --------------------------------------------------------------------------- #

def test_no_numeric_action_weight_is_stated_anywhere():
    """The load-bearing regression guard. If someone reintroduces "a reply is
    worth 13x a like", this fails."""
    surfaces = [algorithm_brief(purpose) for purpose in
                ("create_post", "reply", "timing", "evaluate")]
    surfaces.append(pipeline_brief())
    surfaces.extend(am.MECHANISMS.values())
    banned = re.compile(r"\b\d+(?:\.\d+)?\s*x\b|worth ~?\d|weight of \d", re.IGNORECASE)
    for text in surfaces:
        assert not banned.search(text), f"states a weight: {text[:80]}"


def test_no_surface_promises_virality_or_a_score():
    surfaces = [algorithm_brief(p) for p in ("create_post", "reply", "timing", "evaluate")]
    for text in surfaces:
        lowered = text.lower()
        for forbidden in ("guarantee", "will go viral", "x score", "algorithm-approved"):
            assert forbidden not in lowered


def test_the_visibility_boundary_is_explicit():
    boundary = am.VISIBILITY_BOUNDARY.lower()
    assert "no access" in boundary
    for unknowable in ("viewer histories", "runtime ranking weights", "experiments"):
        assert unknowable in boundary


def test_the_public_pipeline_is_modelled_in_order():
    names = [name for name, _ in am.PIPELINE_STAGES]
    assert names[0] == "query_hydration"
    assert names[-1] == "selection_and_filtering"
    assert "phoenix_ranking" in names and "pre_ranking_filters" in names


def test_both_action_sets_are_present():
    """The release predicts positive AND negative actions separately, which is
    why bait is a cost rather than a neutral tactic."""
    assert "favorite" in am.POSITIVE_ACTIONS and "follow_author" in am.POSITIVE_ACTIONS
    assert "report" in am.NEGATIVE_ACTIONS and "not_dwelled" in am.NEGATIVE_ACTIONS
    assert not set(am.POSITIVE_ACTIONS) & set(am.NEGATIVE_ACTIONS)


def test_creator_weights_are_declared_as_our_own_priors():
    total = sum(am.CREATOR_OPPORTUNITY_WEIGHTS.values())
    assert total == pytest.approx(1.0, abs=0.001), "the spec's weights must sum to 1"
    assert "audience_retrieval_fit" in am.CREATOR_OPPORTUNITY_WEIGHTS
    assert len(am.CREATOR_OPPORTUNITY_PENALTIES) == 5


def test_the_four_gates_and_four_audiences_exist():
    assert [name for name, _ in am.GATES] == [
        "eligibility", "retrieval_fit", "action_value", "conversion_value"]
    assert [name for name, _ in am.AUDIENCE_TYPES] == [
        "core", "adjacent", "action", "conversion"]


def test_nine_archetypes_each_carry_a_primary_action():
    assert len(am.ARCHETYPES) == 9
    for _name, description, action in am.ARCHETYPES:
        assert description and action


# --------------------------------------------------------------------------- #
# Briefs
# --------------------------------------------------------------------------- #

def test_briefs_exist_and_respect_the_budget():
    for purpose in ("create_post", "reply", "timing", "evaluate"):
        brief = algorithm_brief(purpose)
        assert brief and len(brief) <= 700


def test_an_unknown_purpose_falls_back_rather_than_returning_nothing():
    """A prompt with an empty ranking brief silently loses the guidance."""
    assert algorithm_brief("nonsense") == algorithm_brief("create_post")


def test_budget_truncates_on_a_word_boundary():
    brief = algorithm_brief("create_post", budget=120)
    assert len(brief) <= 121 and not brief.rstrip("…").endswith(" ")


# --------------------------------------------------------------------------- #
# Creator-side alignment
# --------------------------------------------------------------------------- #

def test_soft_bait_is_penalised():
    clean = ("The gap between benchmark wins and production reliability keeps "
             "widening, and teams are paying for the second one.")
    bait = "Like if you agree — follow me for more AI takes, and tag someone who needs this!"
    assert algorithm_alignment(clean, "post")[0] > algorithm_alignment(bait, "post")[0]
    assert any("bait" in note for note in algorithm_alignment(bait, "post")[1])


def test_a_payoff_hidden_behind_a_link_scores_lower():
    native = ("Local models finally cleared 40 tok/s on consumer GPUs, which "
              "changes what is worth running on your own machine.")
    link_first = "https://example.com/blog-post"
    assert algorithm_alignment(native, "post")[0] > algorithm_alignment(link_first, "post")[0]
    assert any("link" in note for note in algorithm_alignment(link_first, "post")[1])


def test_a_question_without_a_premise_scores_below_one_with_it():
    generic = "New model just dropped. Thoughts?"
    premise = ("The new model tops every benchmark but costs 4x more per call — "
               "does that gap hold up on real workloads?")
    assert algorithm_alignment(premise, "post")[0] > algorithm_alignment(generic, "post")[0]


def test_agreement_filler_is_penalised_for_replies_specifically():
    """Conversation de-duplication keeps one candidate per conversation, so a
    reply that only agrees is the clearest waste of that slot."""
    filler = "This. So true, exactly right."
    score, notes = algorithm_alignment(filler, "reply")
    assert score < 0.8
    assert any("agreement" in note for note in notes)


def test_hashtags_and_all_caps_are_penalised():
    """X's own organic guidance, not a ranking claim."""
    plain = "Local inference got cheap enough to change the default architecture."
    tagged = "Local inference got cheap enough #AI #MachineLearning #Tech"
    shouty = "LOCAL INFERENCE GOT CHEAP ENOUGH TO CHANGE THE DEFAULT ARCHITECTURE"
    assert algorithm_alignment(plain, "post")[0] > algorithm_alignment(tagged, "post")[0]
    assert algorithm_alignment(plain, "post")[0] > algorithm_alignment(shouty, "post")[0]


def test_an_empty_draft_scores_zero():
    score, notes = algorithm_alignment("   ", "post")
    assert score == 0.0 and notes


def test_alignment_is_bounded():
    for text in ("", "a", "word " * 200, "Like if you agree #x " * 20):
        score, _ = algorithm_alignment(text, "post")
        assert 0.0 <= score <= 1.0
