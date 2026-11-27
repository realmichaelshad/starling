"""A standalone post was scoring zero on a twelve-point term, every time.

`_topic_fit` measured a draft against the post being replied to, or against
the account's content pillars. A standalone post has no post to reply to, and
`content_pillars` is empty on this account and stays empty until enough sync
has happened — so the term returned 0.0 for every original post. Twelve points
out of a hundred, unavailable for a reason that had nothing to do with the
draft, and the direct consequence was that posts ranked below replies on score
alone.

The fallback asks the only question left: is this even about his subject? The
vocabulary comes from his own archive, minus ordinary English — without that
subtraction "He scored twice in the second half and the crowd went home happy"
matched on and/the/second and took nearly half marks.
"""
from __future__ import annotations

import pytest

from app.growth_scorer import _account_vocabulary, _topic_fit


def fit(text: str, *, context: str = "", pillars=None) -> float:
    """Points out of the 12 this term is worth."""
    return _topic_fit(text, context, pillars or []) * 12.0


@pytest.fixture(autouse=True)
def _beat(monkeypatch):
    """A fixed beat, so the test does not depend on the live archive."""
    import app.growth_scorer as scorer
    monkeypatch.setitem(scorer._VOCAB_CACHE, "at", float("inf"))
    monkeypatch.setitem(scorer._VOCAB_CACHE, "tokens", frozenset({
        "opus", "claude", "anthropic", "gpt", "fable", "grok", "kimi",
        "benchmarks", "pricing", "model", "launch", "inference", "local",
    }))


def test_an_on_topic_post_no_longer_scores_zero():
    assert fit("Opus 5 pricing changes the maths for local inference.") > 0.0


def test_off_topic_text_scores_near_zero():
    for text in ("Preheat the oven to 180C and butter a nine inch cake tin.",
                 "The quarterly synergy alignment framework drives value.",
                 "Rainfall this week will ease the drought in the east."):
        assert fit(text) < 3.0, text


def test_grammar_words_alone_do_not_earn_topical_fit():
    """The football sentence: all common English, none of his subject."""
    assert fit("He scored twice in the second half and the crowd went home "
               "happy after all of that") < 3.0


def test_on_topic_outscores_off_topic_by_a_clear_margin():
    on = fit("Claude Opus 5 and Fable benchmarks moved on pricing.")
    off = fit("He scored twice in the second half and went home.")
    assert on > off * 2


def test_a_reply_still_scores_against_the_post_it_answers():
    """The fallback must not displace the real signal when one exists."""
    scored = fit("The rate limit is the catch, not the price.",
                 context="Opus 5 is here at half the price of Opus 4.5.")
    assert scored > 0.0


def test_the_vocabulary_excludes_ordinary_english():
    import app.growth_scorer as scorer
    scorer._VOCAB_CACHE.update(at=0.0, tokens=frozenset())
    vocabulary = _account_vocabulary()
    if not vocabulary:
        pytest.skip("no archive in this environment")
    assert not vocabulary & {"the", "and", "was", "have", "this", "that"}
