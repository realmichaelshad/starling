"""The 2026-07-28 morning pair, both calibrated 0/71 on his writing.

1. "is a big deal" — the exact hype sentence the prompt bans, shipped twice
   as essentially the whole draft. Quality issue (ships flagged) + revisable.
2. A verbless trailing question ("Frontier-Bench SOTA 43.3%?") — a noun pile
   with a question mark. Cut by repair_stub_tail; his real closing questions
   have verbs and stay.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.intelligence import _BIG_DEAL_RE, repair_stub_tail   # noqa: E402


def test_big_deal_is_flagged_and_revisable():
    from app.intelligence import _CLARITY_ISSUES, _WEAKNESS_NOTES
    assert _BIG_DEAL_RE.search("Ami's graph memory is a big deal.")
    assert not _BIG_DEAL_RE.search("Ami closed a big deal with AWS.")
    assert "empty_big_deal" in _WEAKNESS_NOTES
    assert "empty_big_deal" in _CLARITY_ISSUES


def test_verbless_question_tail_is_cut():
    text = ("Fable 5 blocked biology queries now route to Opus 5.\n\n"
            "Frontier-Bench SOTA 43.3%?")
    assert repair_stub_tail(text) == \
        "Fable 5 blocked biology queries now route to Opus 5."


def test_a_closing_question_with_a_verb_survives():
    text = ("The benchmark gap is 13 points.\n\n"
            "If the model is ready, why isn't the code?")
    assert repair_stub_tail(text) == text


def test_the_only_sentence_is_never_cut():
    assert repair_stub_tail("Frontier-Bench SOTA 43.3%?") == \
        "Frontier-Bench SOTA 43.3%?"


def test_first_person_titles_never_reach_the_pool():
    """"My local LLM scored 6/6" became a first-person draft in his voice —
    the title filter's noun list didn't cover "LLM". Any My/Our opener is the
    author's own story."""
    from app.subject import event_subject
    assert event_subject("My local LLM scored 6/6. It was wrong every time") is None
    assert event_subject("Our benchmark says otherwise") is None
    # Third-person titles with possessives mid-sentence still pass.
    assert event_subject("Mistral raises its API prices") is not None


def test_first_person_sentences_are_cut_from_summaries():
    """"We did it with Exabase" rode an HN summary into evidence and came out
    as HIS claim. Same rule source_facts applies to pages, now on summaries."""
    from app.news import _strip_furniture
    raw = ("Exabase posts SOTA on the BEAM benchmark at 10M tokens. "
           "We did it with a smaller model. "
           "The harness is open source under MIT.")
    out = _strip_furniture(raw)
    assert "We did it" not in out
    assert "Exabase posts SOTA" in out
    assert "open source under MIT" in out
