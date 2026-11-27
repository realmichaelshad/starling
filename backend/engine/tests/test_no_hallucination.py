"""Anti-hallucination routing: the model must DEFER, not invent, on anything
outside its facts. Every case here corresponds to a confirmed hallucination
from the 54-question audit (weather, Grok=Microsoft, +2 followers, etc.)."""
from __future__ import annotations

import pytest

from app import main
from app.strategy import _facts_question_answer


def test_off_topic_deflects_instead_of_fabricating():
    for q in ["whats the weather", "what time is it in tokyo", "who won the game",
              "whats the price of bitcoin", "who is the president"]:
        a = main._out_of_scope_answer(q)
        assert a and "strategist" in a.lower(), q


def test_model_comparisons_defer():
    for q in ["is grok better than claude", "grok vs claude",
              "which is better claude or gpt", "compare fable and opus"]:
        a = main._out_of_scope_answer(q)
        assert a and ("verify" in a.lower() or "benchmark" in a.lower()), q


def test_on_topic_questions_are_not_deflected():
    for q in ["give me some ideas", "what makes a good tweet", "is now a good time to post"]:
        assert main._out_of_scope_answer(q) is None, q


def test_growth_question_gives_advice_not_the_count():
    a = _facts_question_answer("how do i get more followers")
    # advice, never the raw count ("You have 16 followers")
    assert a and "16" not in a
    assert "reply" in a.lower() or "post" in a.lower()


def test_am_i_growing_never_invents_a_delta():
    a = _facts_question_answer("am i growing")
    assert a is not None
    # honest: either a real observed change or "I don't have..." — never a made-up jump
    assert "increased by" not in a.lower() or "observed" in a.lower()


def test_account_summary_labels_median_not_average():
    a = _facts_question_answer("hows my account doing")
    assert a is not None
    assert "average" not in a.lower()


def test_training_cutoff_is_stripped():
    out = main._trim_chat_answer("Grok is fine. I can only compare up to my last update in early 2023. It helps.")
    assert "last update" not in out.lower() and "2023" not in out


def test_ideas_route_to_grounded_radar():
    for q in ["give me some ideas", "what should i tweet", "im out of ideas",
              "what should i jump on"]:
        assert main._RADAR_Q.search(q), q


def test_grounding_strips_outside_companies():
    from app import grounding as g
    gs = g.grounding_set(pillars=["Anthropic", "Claude", "OpenAI", "Grok", "xAI", "Kimi"],
                         own_texts=["Kimi K3 is out"], trend_names=[])
    out, removed = g.ground_answer("Anthropic's Claude is strong. Microsoft's Opus is better.", gs)
    assert removed and "Microsoft" not in out
    assert "Anthropic's Claude is strong." in out


def test_grounding_keeps_the_accounts_real_topics():
    from app import grounding as g
    gs = g.grounding_set(pillars=["Anthropic", "OpenAI", "Grok", "xAI", "Google", "Kimi"],
                         own_texts=[], trend_names=[])
    text = "OpenAI and Grok are worth watching. Anthropic shipped Opus."
    out, removed = g.ground_answer(text, gs)
    assert removed == [] and out == text


def test_grounding_catches_possessive_company_names():
    from app import grounding as g
    gs = g.grounding_set(pillars=["Claude"], own_texts=[], trend_names=[])
    assert "moonshot" in {c for c in g.ungrounded_companies("Moonshot AI's Kimi topped it.", gs)}
    assert "meta" in {c for c in g.ungrounded_companies("Meta's new model is out.", gs)}


def test_ambiguous_directive_asks_instead_of_fabricating():
    for m in ["make it better", "do the thing", "yes", "do it", "the other one"]:
        a = main._ambiguous_answer(m, has_context=False)
        assert a is not None and "not sure" in a.lower(), m
    # with a post open, these are actionable, not ambiguous
    assert main._ambiguous_answer("do it", has_context=True) is None


def test_help_gives_a_fixed_capability_menu_not_a_model_guess():
    for q in ["help", "what can you do", "what do you do", "who are you"]:
        a = main._help_answer(q)
        assert a is not None and "strategist" in a.lower(), q
        # never invents a follower delta like the model did ("14 vs 16")
        assert "14" not in a and "vs 16" not in a
    # a real request slips through to normal routing
    assert main._help_answer("help me write a post about kimi") is None


def test_meta_instruction_echo_is_stripped():
    leaked = ("Here are ideas. Remember to use real data from account_facts only; "
              "don't guess or make up numbers. Stick strictly to topics.")
    out = main._ground_answer(leaked)
    assert "account_facts" not in out and "make up" not in out.lower()
    assert "Here are ideas." in out


def test_web_lookup_detects_knowledge_questions():
    assert main._WEB_LOOKUP_Q.search("is grok better than claude")
    assert main._WEB_LOOKUP_Q.search("whats the latest on gpt 5")
    assert main._WEB_LOOKUP_Q.search("search for kimi benchmarks")
    # account/draft/greeting questions must NOT trigger a web search
    assert not main._WEB_LOOKUP_Q.search("what do you think of my last post")
    assert not main._WEB_LOOKUP_Q.search("hey")
    assert not main._WEB_LOOKUP_Q.search("write me a post about kimi")


def test_web_lookup_off_when_research_disabled(monkeypatch):
    from app import research
    monkeypatch.setattr(research, "research_enabled", lambda: False)
    assert main._web_lookup_answer("whats the latest on gpt 5") is None


def test_web_lookup_never_searches_and_never_invents(monkeypatch):
    """General web search is not available to the hosted engine, so a lookup
    question gets no fabricated "I looked it up" answer: the lookup declines and
    the caller falls back to its honest "I can't verify that" line."""
    from app import cloud, research
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(cloud, "fetch", lambda url: pytest.fail("a lookup must not fetch pages"))
    monkeypatch.setattr(cloud, "summary", lambda query_id: pytest.fail("a lookup must not ask for summaries"))
    assert main._web_lookup_answer("whats the latest on claude opus") is None
