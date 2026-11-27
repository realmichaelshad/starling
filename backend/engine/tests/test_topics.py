"""Topic selection for standalone posts (spec §4.4a, §6.2b).

These tests exist because of a real, observed failure: four consecutive
"create a post" runs on the live account all produced Anthropic/Fable posts,
because content pillars were being passed in as the SUBJECT.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import topics


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _post(text: str, *, handle: str, minutes_ago: int = 30) -> dict:
    return {"post_id": f"p{abs(hash((text, handle))) % 10**6}", "text": text,
            "author_handle": handle,
            "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def _own(text: str, *, days_ago: int = 2) -> dict:
    return {"text": text, "created_at": (NOW - timedelta(days=days_ago)).isoformat()}


# --------------------------------------------------------------------------- #
# The regression itself
# --------------------------------------------------------------------------- #

def test_pillars_are_never_the_subject():
    """The exact bug: pillars must not become the topic of the post."""
    choice = topics.choose_topic(candidates=[], pillar_tokens={"anthropic", "fable"},
                                 own_posts=[], now=NOW)
    line = topics.grounding_line(choice)
    assert "anthropic" not in line.lower()
    assert "fable" not in line.lower()
    assert "content pillars" not in line.lower()


def test_no_signal_admits_it_rather_than_falling_back():
    choice = topics.choose_topic(candidates=[], pillar_tokens={"anthropic"},
                                 own_posts=[], now=NOW)
    assert choice["topic"] is None
    assert choice["fell_back_to_archive"] is False
    # T3 (2026-09-21): it still says what to do, but only something the product can act
    # on; "open x.com/explore" and "add an X API key" were neither.
    assert "explore" not in choice["reason"].lower() and "api key" not in choice["reason"].lower()
    assert "tell me a subject" in choice["reason"].lower()


def test_saturated_topic_loses_to_a_fresh_one():
    """The term that widens the range: what the account already covers is penalised."""
    # Both subjects sit INSIDE the account's domain. That is the real ask:
    # widen the range across things this account can credibly discuss, not
    # jump to an unrelated field. Varied phrasing on purpose — five identical
    # strings is a genuinely closed angle, and real posts about one event do
    # not repeat each other verbatim.
    anthropic = [
        "anthropic claude opus pricing changed again today",
        "claude opus pricing tiers just moved for anthropic teams",
        "anthropic quietly reworked claude opus pricing overnight",
        "the new claude opus pricing from anthropic favours volume",
        "anthropic claude opus pricing update landed without a changelog",
    ]
    gemini = [
        "gemini shipped a much longer context window today",
        "google gemini context limits jumped in this release",
        "the new gemini build handles far longer context",
        "gemini long context is live for google cloud users",
        "google quietly raised gemini context limits overnight",
    ]
    candidates = ([_post(t, handle=f"a{i}") for i, t in enumerate(anthropic)]
                  + [_post(t, handle=f"b{i}") for i, t in enumerate(gemini)])
    own = [_own("anthropic claude opus pricing is wild", days_ago=d) for d in (1, 3, 5, 7)]

    choice = topics.choose_topic(
        candidates=candidates,
        pillar_tokens={"anthropic", "claude", "opus", "gemini", "google"},
        own_posts=own, now=NOW)
    assert choice["topic"] is not None
    picked = choice["topic"]["topic"].lower()
    # Despite pillars covering Anthropic too, the subject they keep repeating loses.
    assert "gemini" in picked
    assert "anthropic" not in picked


def test_self_repetition_scales_with_how_often_it_is_covered():
    tokens = {"anthropic", "claude", "pricing"}
    none = topics.self_repetition(tokens, [], now=NOW)
    once = topics.self_repetition(tokens, [_own("anthropic claude pricing")], now=NOW)
    lots = topics.self_repetition(
        tokens, [_own("anthropic claude pricing", days_ago=d) for d in (1, 2, 3, 4)],
        now=NOW)
    assert none[0] == 0.0
    assert 0.0 < once[0] < lots[0]
    assert lots[0] == 1.0  # saturated


def test_stale_coverage_does_not_count_as_repetition():
    """A topic covered a year ago is not the same as one covered twice this week."""
    tokens = {"anthropic", "claude"}
    old = topics.self_repetition(
        tokens, [_own("anthropic claude thoughts", days_ago=200)], now=NOW)
    assert old[0] == 0.0


# --------------------------------------------------------------------------- #
# Prediction — the user's "or will trend" case
# --------------------------------------------------------------------------- #

def test_early_topic_is_marked_forward_looking_and_asks_for_falsifiability():
    candidates = [_post("gateway api ga shipping next release", handle=f"h{i}",
                        minutes_ago=5) for i in range(3)]
    choice = topics.choose_topic(candidates=candidates, pillar_tokens=set(),
                                 own_posts=[], now=NOW)
    if choice["topic"] and choice["topic"]["forward_looking"]:
        line = topics.grounding_line(choice)
        assert "falsifiable" in line.lower()


def test_no_uncalibrated_probability_is_ever_emitted():
    """§4.4: a raw score is not a probability. No percentages in user-facing text."""
    candidates = [_post("gateway api ga", handle=f"h{i}") for i in range(4)]
    choice = topics.choose_topic(candidates=candidates, pillar_tokens=set(),
                                 own_posts=[], now=NOW)
    line = topics.grounding_line(choice)
    assert "%" not in line
    assert "likely to trend" not in line.lower()


# --------------------------------------------------------------------------- #
# Variety
# --------------------------------------------------------------------------- #

def test_alternatives_offer_a_different_subject():
    gateway = ["gateway api ga shipped this morning",
               "kubernetes gateway api is stable now",
               "gateway api general availability landed",
               "ingress is done, gateway api went GA"]
    postgres = ["postgres 18 async io benchmarks look strong",
                "async io in postgres 18 changes the write path",
                "benchmarks for postgres 18 async io are out",
                "postgres 18 ships async io, benchmarks surprised me"]
    candidates = ([_post(t, handle=f"a{i}") for i, t in enumerate(gateway)]
                  + [_post(t, handle=f"b{i}") for i, t in enumerate(postgres)])
    choice = topics.choose_topic(candidates=candidates, pillar_tokens=set(),
                                 own_posts=[], now=NOW)
    assert choice["topic"] is not None
    assert choice["alternatives"], "variety requires a second subject to offer"
    assert choice["alternatives"][0]["topic"] != choice["topic"]["topic"]


def test_relevance_still_matters_but_does_not_dictate():
    """Pillars keep their real job: scoring credibility, not supplying subjects."""
    texts = ["claude opus context window doubled overnight",
             "the opus context window is twice what it was",
             "claude opus now takes a much larger context",
             "opus context limits jumped in the latest claude build"]
    candidates = [_post(t, handle=f"a{i}") for i, t in enumerate(texts)]
    with_pillars = topics.choose_topic(
        candidates=candidates, pillar_tokens={"claude", "opus", "anthropic"},
        own_posts=[], now=NOW)
    without = topics.choose_topic(candidates=candidates, pillar_tokens=set(),
                                  own_posts=[], now=NOW)
    assert with_pillars["topic"]["relevance"] > without["topic"]["relevance"]


def test_unrelated_subject_is_ineligible_however_hot_it_is():
    """Widening range means a different subject they CAN speak to.

    Observed on the real archive: an AI-commentary account was handed a
    cyclospora prediction market as its post topic, purely because it was fresh
    and shared no words with anything they had already written.
    """
    hot = ["breaking cyclospora outbreak traced to imported basil",
           "cyclospora case count revised upward again today",
           "another cyclospora recall notice just went out",
           "cyclospora testing backlog is growing at the lab"]
    candidates = [_post(t, handle=f"h{i}", minutes_ago=3) for i, t in enumerate(hot)]
    choice = topics.choose_topic(
        candidates=candidates, pillar_tokens={"anthropic", "claude", "gemini"},
        own_posts=[], now=NOW)
    assert choice["topic"] is None
    assert choice["fell_back_to_archive"] is False


def test_bare_trend_names_are_not_topics():
    """"Smooth" and "#Dbacks" are pointers, not subjects to write about."""
    candidates = [_post(name, handle=f"t{i}")
                  for i, name in enumerate(["Ojay", "#Dbacks", "Smooth", "#FRAENG"])]
    choice = topics.choose_topic(candidates=candidates, pillar_tokens=set(),
                                 own_posts=[], now=NOW)
    assert choice["topic"] is None


def test_topic_label_is_readable_not_a_token_bag():
    texts = ["gemini shipped a much longer context window today",
             "google gemini context limits jumped in this release",
             "the new gemini build handles far longer context"]
    candidates = [_post(t, handle=f"a{i}") for i, t in enumerate(texts)]
    choice = topics.choose_topic(candidates=candidates,
                                 pillar_tokens={"gemini", "google"},
                                 own_posts=[], now=NOW)
    assert choice["topic"] is not None
    # The cluster key is alphabetised tokens; the label must be real prose.
    assert choice["topic"]["topic"] in texts


def test_grounding_line_forbids_the_usual_subjects():
    candidates = [_post("gateway api ga shipped", handle=f"a{i}") for i in range(4)]
    choice = topics.choose_topic(candidates=candidates, pillar_tokens=set(),
                                 own_posts=[], now=NOW)
    assert "usual subjects" in topics.grounding_line(choice).lower()
