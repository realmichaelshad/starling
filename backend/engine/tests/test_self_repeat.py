"""Starling must never hand back something it already wrote.

The real incident: the very first draft of a session came back as
"Why is Anthropic so quiet about Opus 5? ... The silence is making me think
they're cooking." — a draft Starling had produced earlier, never posted, and
therefore present in no archive and no example list. Two separate holes let it
through, and both are pinned here:

  1. The copy check only ever compared against the 3-5 voice examples in the
     prompt, so 28 of his 33 archive posts were invisible to it.
  2. Nothing at all compared a draft against Starling's OWN 136 past drafts.
     The one repeat-detector read `recent_angles`, which is written only when a
     generation is marked posted against a campaign — and this account has zero
     campaigns, so it has always been empty and the fatigue score has always
     been 0.0.
"""
from __future__ import annotations

import app.intelligence as intelligence


THE_REPEAT = ("Why is Anthropic so quiet about Opus 5? It'll be significantly "
              "better than Fable and way cheaper.\n\nThe silence is making me "
              "think they're cooking.")


def _with_corpus(monkeypatch, texts):
    monkeypatch.setattr(intelligence, "_OWN_WRITING_CACHE", None)
    monkeypatch.setattr(intelligence, "_own_writing", lambda: list(texts))


def test_past_draft_is_caught_even_though_it_was_never_posted(monkeypatch):
    _with_corpus(monkeypatch, [THE_REPEAT])
    assert intelligence._echoes_own_writing(THE_REPEAT.lower(), [])


def test_reworded_past_draft_is_caught(monkeypatch):
    """The old gate needed the WHOLE post verbatim, so a reword walked through."""
    _with_corpus(monkeypatch, [THE_REPEAT])
    reworded = ("why is anthropic so quiet about opus 5? it will be way better "
                "than fable and much cheaper. feels like they are cooking.")
    assert intelligence._echoes_own_writing(reworded, [])


def test_lifted_opening_with_a_new_tail_is_caught(monkeypatch):
    _with_corpus(monkeypatch, [THE_REPEAT])
    lifted = ("why is anthropic so quiet about opus 5? worth watching closely "
              "this week i think.")
    assert intelligence._echoes_own_writing(lifted, [])


def test_a_genuinely_new_take_on_the_same_subject_is_allowed(monkeypatch):
    """The gate must not stop him posting about his own beat twice."""
    _with_corpus(monkeypatch, [THE_REPEAT])
    fresh = ("opus 5 at half the price changes the build-vs-buy maths for small "
             "teams, and nobody is pricing that in yet.")
    assert not intelligence._echoes_own_writing(fresh, [])


def test_corpus_reads_past_drafts_not_just_the_prompt_examples(monkeypatch):
    """_own_writing must pull from all three sources, not the examples list."""
    monkeypatch.setattr(intelligence, "_OWN_WRITING_CACHE", None)
    monkeypatch.setattr(intelligence, "performance_records",
                        lambda: [{"text": "archived post"}])
    monkeypatch.setattr(intelligence, "learning_examples", lambda: ["accepted draft"])
    monkeypatch.setattr(intelligence, "previously_drafted_texts",
                        lambda: ["a draft that was never posted"])
    assert set(intelligence._own_writing()) == {
        "archived post", "accepted draft", "a draft that was never posted"}


def test_corpus_is_not_cached_long_enough_to_repeat_within_a_session(monkeypatch):
    """Two clicks a minute apart must see each other's drafts."""
    assert intelligence._OWN_WRITING_TTL_S <= 30.0


# ---------------------------------------------------------------------------
# The ungated-chat bypass found while proving the repeat fix.
#
# Asking "post about why anthropic is quiet on opus 5" produced raw model text
# — "Alright, here's a draft post for Anthropic being quiet about Opus 5:" plus
# an emoji and a made-up model name — with no growth_score and no schedule,
# because it never reached the generator at all. "post" led the sentence as an
# imperative verb but carried no separate write verb, so _wants_draft said no
# and the message fell through to conversation. Both the routing and the
# preamble are pinned here.
# ---------------------------------------------------------------------------

from app.main import _trim_chat_answer, _wants_draft


def test_imperative_post_about_is_a_draft_request():
    assert _wants_draft("post about why anthropic is quiet on opus 5")
    assert _wants_draft("tweet about the opus 5 price cut")
    assert _wants_draft("thread on local inference costs")


def test_asking_for_advice_is_still_not_a_draft_request():
    """The imperative rule must not swallow questions about whether to post."""
    assert not _wants_draft("should i post about kimi")
    assert not _wants_draft("is now a good time to post")
    assert not _wants_draft("when should i post today")
    assert not _wants_draft("what post did best last week")
    assert not _wants_draft("post it")


def test_the_model_announcing_its_own_draft_never_ships():
    leaked = ("Alright, here's a draft post for Anthropic being quiet about "
              "Opus 5:\n\nAnthropic staying mum on Opus 5 rollout?")
    assert _trim_chat_answer(leaked) == "Anthropic staying mum on Opus 5 rollout?"
    assert _trim_chat_answer("Sure! Here is a revised version:\nOpus 5 is cheap.") \
        == "Opus 5 is cheap."
    assert _trim_chat_answer("Okay, here you go:\nIt beat our cloud bill.") \
        == "It beat our cloud bill."


def test_a_genuine_answer_that_opens_with_here_is_survives():
    """'Here is why your Tuesday posts do better' is an ANSWER, not a preamble."""
    for answer in ("Here is why your Tuesday posts do better: you post at 8pm.",
                   "Here are the three posts that did best last month.",
                   "Your best window is Wednesday 8 PM UTC."):
        assert _trim_chat_answer(answer) == answer


# ---------------------------------------------------------------------------
# The curly apostrophe. The preamble scrubber above was written, tested against
# the straight-quote version of the leak, and passed — then failed live because
# the model emitted U+2019. An audit found 64 regex patterns across the app
# with the same blind spot, including the detector for "not just X, it's Y"
# (a phrase he has rejected by name) and the routers for "what's next" and
# "don't ...". Rather than teach 64 patterns about Unicode, the text is folded
# at the two boundaries: incoming messages and the draft-gate matching copy.
# ---------------------------------------------------------------------------

import re

from app.text_normalizer import fold_punctuation, normalize_typos

CURLY = "\u2019"


def test_the_ai_tell_detector_is_not_blind_to_curly_apostrophes():
    tell = re.compile(r"\bnot just\b[^.!?\n]{0,45}\b(?:it'?s|but)\b")
    raw = f"This is not just a price cut, it{CURLY}s a strategy shift."
    assert not tell.search(raw.lower()), "precondition: raw text evades the pattern"
    assert tell.search(fold_punctuation(raw).lower()), "fold must restore the match"


def test_intent_matching_folds_curly_punctuation():
    assert normalize_typos(f"what{CURLY}s next for my account").startswith("what's next")


def test_the_preamble_scrubber_works_with_either_apostrophe():
    from app.main import _trim_chat_answer
    for apostrophe in ("'", CURLY):
        leaked = (f"Alright, here{apostrophe}s a draft post for Opus 5:"
                  "\n\nAnthropic went quiet.")
        assert _trim_chat_answer(leaked) == "Anthropic went quiet."


def test_folding_does_not_alter_ordinary_text():
    assert fold_punctuation("plain ascii text") == "plain ascii text"
    assert fold_punctuation("") == ""


# ---------------------------------------------------------------------------
# Prompt scaffolding echoed back into the post. The revise pass sends a
# "WHAT IS WRONG:" block listing a draft's faults; the model answered by
# reprinting that heading and the critique beneath it as part of the post.
# Seven of the 146 drafts on this machine carry a leak of this shape, so it is
# a recurring class, not a one-off. Cut at the heading — verified against the
# real archive, which contains no such line.
# ---------------------------------------------------------------------------

from app.llm import _strip_thinking


def test_prompt_scaffolding_never_reaches_the_composer():
    leak = ("Anthropic went quiet on Opus 5 and that is telling.\n\n"
            "WHAT IS WRONG:\n- This echoes an earlier post\n- Too long")
    assert _strip_thinking(leak) == "Anthropic went quiet on Opus 5 and that is telling."


def test_other_scaffolding_headings_are_cut_too():
    for heading in ("REWRITE:", "RULES:", "WHAT TO FIX:"):
        text = f"A real sentence about pricing.\n\n{heading}\n- do better"
        assert _strip_thinking(text) == "A real sentence about pricing."


def test_ordinary_drafts_pass_through_untouched():
    """Colons and capitals in normal writing must survive."""
    for text in ("Opus 5: cheaper than expected, and that changes the maths.",
                 "The real cost is throughput, NOT the benchmark score.",
                 "Three things matter here: price, latency, and context."):
        assert _strip_thinking(text) == text
