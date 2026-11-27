"""A quote that only asks a question has done none of a quote's job.

Measured 2026-07-27: ten quotes of ten DIFFERENT tweets, and the first five
came back as five rhetorical questions —

    "What's the catch with Goose if it's really beating Claude?"
    "What's the real cost of open weights when the models are already
     outpacing closed ones?"
    "Does Cowork mean Claude finally gets to work in your files without
     babysitting a chatbot?"
    "What's the point of running two models side by side if they don't
     actually disagree?"
    "What if the real problem isn't the switch statement but the models
     themselves?"

One template five times. That is repetitive across a batch on its own, and it
misses the point of the surface: the reader is already looking at the post, so
the quote's whole job is the line they would not otherwise have got. A question
supplies nothing. `what_works.py` measures him opening with a question in 0% of
his posts, top or bottom.

Calibration: fires on 1 of his 35 published posts (3%, limit 20% for a revise
trigger), and that one is "@grok i think Anthropic is copying someone don't
you?" — an @-mention, not a post written from scratch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import asks_without_saying           # noqa: E402

LIVE = (
    "What's the catch with Goose if it's really beating Claude?",
    "What's the real cost of open weights when the models are already outpacing closed ones?",
    "Does Cowork mean Claude finally gets to work in your files without babysitting a chatbot?",
    "What's the point of running two models side by side if they don't actually disagree?",
    "What if the real problem isn't the switch statement but the models themselves?",
)


def test_every_live_question_only_quote_is_caught():
    for text in LIVE:
        assert asks_without_saying(text), f"missed: {text!r}"


def test_a_question_that_lands_a_claim_survives():
    """His own shape: ask, then answer. This must never be touched."""
    assert not asks_without_saying(
        "Why is Anthropic so quiet about Opus 5?\n\n"
        "One thing I'm betting on: it'll be significantly better than Fable 5.")
    assert not asks_without_saying(
        "Prediction: Grok 4.5 launches on July 19, 2026.\n\nWhy?\n\n"
        "Grok 4.5 reportedly entered private testing in June.")


def test_a_plain_statement_is_not_a_question():
    assert not asks_without_saying(
        "Claude Opus 5 tops the index at 26% lower cost per task.")
    assert not asks_without_saying("")


def test_it_stays_within_the_revise_bound_on_his_archive():
    fixture = Path(__file__).with_name("fixtures_his_posts.json")
    if not fixture.exists():
        return
    posts = [p for p in json.loads(fixture.read_text(encoding="utf-8")) if p.strip()]
    hits = [p for p in posts if asks_without_saying(p)]
    share = len(hits) / len(posts)
    assert share <= 0.20, (
        f"fires on {len(hits)}/{len(posts)} ({share:.0%}) of his posts, "
        f"e.g. {hits[0][:90]!r}")


def test_it_is_a_revise_trigger_not_a_silent_reject():
    """The rule from the registry: a quality miss ships with the fault named."""
    from app.intelligence import _CLARITY_ISSUES, _INTEGRITY_ISSUES, _WEAKNESS_NOTES
    assert "asks_without_saying" in _CLARITY_ISSUES, "the rewrite must get one chance"
    assert "asks_without_saying" in _WEAKNESS_NOTES, "it must never ship as a bare shrug"
    assert "asks_without_saying" not in _INTEGRITY_ISSUES
    assert _WEAKNESS_NOTES["asks_without_saying"].startswith("Heads up:")
