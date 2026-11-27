"""Topic selection must not be decided by what the account already wrote.

The account produced the same post over and over. The last six from-scratch
topics on this machine were:

    Grok 4.5 Efficiency Leader
    Anthropic's Opus 5 Restrictions Removal
    Opus 5 AI Model
    Anthropic's Long-standing Line of Heavyweight AI Models
    Claude Opus 5 Value Proposition
    Opus 5 Cybersecurity Safeguards

Five of six are Opus 5, and the variety guard was running the whole time.
Nothing here is about Anthropic specifically — any account would collapse onto
whatever its first posts happened to cover. Two mistakes, stacked:

  1. RELEVANCE SCORED ON PAST ENTITIES. "Is this topic his?" was answered by
     overlap with the entity names in his archive, where one company is 55% of
     all mentions. A topic could only look relevant by naming something he had
     already covered, so nothing new could ever be recommended. Circular by
     construction, and it tightens every time it runs.

  2. VARIETY CHECKED ON LABEL WORDING. The old check needed two shared word
     tokens; "Opus 5 AI Model" and "Claude Opus 5 Value Proposition" share
     exactly one. The same subject, rephrased, read as a different subject.

Voice still comes from his archive — that part was always right. Only topic
selection is cut loose from it.
"""
from __future__ import annotations

import pytest

from app.subject import (domain_fit, repeats_recent_subject, subject_pressure,
                         subjects)

RECENT = ["Grok 4.5 Efficiency Leader",
          "Anthropic's Opus 5 Restrictions Removal",
          "Opus 5 AI Model",
          "Claude Opus 5 Value Proposition"]


def test_the_same_subject_rephrased_is_the_same_subject():
    """The exact failure: five Opus posts that each looked new."""
    assert subjects("Opus 5 AI Model") == subjects("Claude Opus 5 Value Proposition")
    assert repeats_recent_subject("Opus 5 Pricing Advantage", RECENT)
    assert repeats_recent_subject("Anthropic guardrails update", RECENT)


def test_a_genuinely_different_subject_passes():
    assert not repeats_recent_subject("Mistral ships a cheaper model", RECENT)
    assert not repeats_recent_subject("Gemini 3 pricing changes", RECENT)


def test_a_lab_he_has_never_written_about_is_still_his_field():
    """The circularity, stated as a test: novelty must not cost relevance."""
    known = domain_fit("Anthropic ships Opus 5 with a bigger context window")
    novel = domain_fit("Mistral ships a new model with a bigger context window")
    assert novel >= known * 0.9, "an unfamiliar lab must not score lower for being unfamiliar"
    assert domain_fit("Cohere cuts inference pricing for enterprise tiers") > 0.3


@pytest.mark.parametrize("text", [
    "Marvel unveils Doctor Doom casting",
    "Black Panther trailer drops",
    "Mead wins the sports award",
])
def test_off_field_topics_stay_low(text: str) -> None:
    assert domain_fit(text) < 0.25, text


def test_entity_names_are_not_in_the_topic_vocabulary():
    """With them in, 'is this his topic' means 'has he said this name before'."""
    from app.growth_scorer import _VOCAB_CACHE, _account_vocabulary
    from app.subject import _SUBJECT_ALIASES
    _VOCAB_CACHE.update(at=0.0, tokens=frozenset())
    vocabulary = _account_vocabulary()
    if not vocabulary:
        pytest.skip("no archive in this environment")
    assert not (vocabulary & set(_SUBJECT_ALIASES)), \
        "an entity name in the topic vocabulary re-creates the circularity"


def test_pressure_measures_how_lopsided_the_run_is():
    pressure = subject_pressure(RECENT)
    assert pressure["anthropic"] > pressure["xai"]
    assert abs(sum(pressure.values()) - 1.0) < 1e-6


def test_a_crowded_subject_is_pushed_down_not_banned():
    """An account has to be about more than one company — but a genuinely big
    story from a crowded subject should still be able to win."""
    from app.trends import rank_for_account
    rows = [{"name": "Anthropic ships Opus 5 with new pricing", "rank": 1,
             "volume_text": "Trending now · News · 90K posts"},
            {"name": "Mistral ships a new model at lower pricing", "rank": 2,
             "volume_text": "Trending now · News · 5K posts"}]
    ranked = rank_for_account(rows, frozenset({"pricing", "model"}))
    assert len(ranked) == 2, "neither is banned outright"


# ---------------------------------------------------------------------------
# The profile itself. `account_profile` held no pillars, no niche and no
# topics — nothing about what he is into — so the system counted the only
# thing it could and decided this account "is" Anthropic, xAI and OpenAI.
# That describes nobody. He is interested in AI models and vibe-coding;
# a company is just whoever shipped this week.
# ---------------------------------------------------------------------------
def test_the_profile_is_themes_not_companies():
    from app.subject import _SUBJECT_ALIASES, interests
    posts = ["Opus 5 is cheaper than Fable 5 on every tier",
             "Claude Code is turning into a dev team in the terminal",
             "GPT-5.6 benchmarks are out and the latency is the story",
             "Running Qwen locally beat our cloud bill"]
    themes = interests(posts)
    assert themes, "an account that writes about anything has themes"
    assert not (set(themes) & set(_SUBJECT_ALIASES)), \
        "a company name is not a theme"
    assert any("pricing" in t or "coding" in t or "release" in t for t in themes)


def test_an_unfamiliar_company_matching_his_themes_beats_a_familiar_one_that_does_not():
    """The whole point: the theme decides, not the name."""
    from app.subject import interest_fit, interests
    themes = interests(["Opus 5 pricing changes the maths",
                        "Claude Code agents in the terminal",
                        "benchmarks are free, throughput is the cost"])
    unfamiliar = interest_fit("Some unknown startup ships a coding agent for the terminal", themes)
    familiar_offtopic = interest_fit("Anthropic executive gives a conference keynote", themes)
    assert unfamiliar > familiar_offtopic


def test_an_account_with_no_history_still_gets_a_usable_read():
    from app.subject import interest_fit
    assert interest_fit("Mistral cuts inference pricing", []) > 0.0
