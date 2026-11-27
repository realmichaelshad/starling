"""W4: a real web search, once, only for a claim the post's links and the news pool cannot check.

The search is metered AI spend (services/world/search.py), so the engine side pins WHEN it runs
(a checkable, time-sensitive claim in a public post, nothing else covering it), that it runs
BESIDE the rest of the press within its own budget, that a late one is dropped and said so, and
that whatever it found reaches the writer with its sources: OpenAI requires web results shown to
users to carry visible, clickable citations, so a draft built on them carries `sources`.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import cloud, main, news, providers, radar, request_context, research
from app.schemas import BrowserAssistRequest, PostContext

NOW = datetime.now(timezone.utc)
CLAIM = "Moonshot released Kimi K3 today and it beats Opus 5 on SWE-bench."
RESULTS = [
    {"title": "Moonshot releases Kimi K3", "url": "https://news.example.com/kimi-k3",
     "snippet": "Moonshot released Kimi K3 on Monday; it scores 71.2% on SWE-bench Verified, "
                "ahead of Opus 5 at 69.8%, according to the company."},
    {"title": "Kimi K3 model card", "url": "https://example.com/kimi-k3-card",
     "snippet": "The Kimi K3 model card lists a 256k context window and open weights under a modified MIT licence."},
]


def _post(text=CLAIM, **extra) -> PostContext:
    base = dict(post_id="1790000000000000777", author="AI Watcher", author_handle="aiwatcher",
                text=text, visibility="public",
                post_created_at=(NOW - timedelta(minutes=12)).isoformat(),
                visible_metrics={"views": 18000, "likes": 700, "replies": 60},
                thread_scanned=True, visible_reply_count=60, visible_reply_ages_minutes=[1, 3, 4])
    base.update(extra)
    return PostContext(**base)


@pytest.fixture()
def web(monkeypatch):
    """The world as the claim check sees it: an empty pool, and a scripted search."""
    calls: list[dict] = []
    state = {"answer": ({"sources": RESULTS}, "ok"), "delay": 0.0}

    def fake_search(query, *, timeout_s=cloud.SEARCH_TIMEOUT_S):
        calls.append({"query": query, "timeout_s": timeout_s, "at": time.monotonic()})
        if state["delay"]:
            time.sleep(state["delay"])
        return state["answer"]

    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(cloud, "search", fake_search)
    return calls, state


# --------------------------------------------------------------------------- when


@pytest.mark.parametrize("text", [
    CLAIM,
    "Anthropic raised $13B at a $183B valuation this morning.",
    "OpenAI now lets you pick the model in ChatGPT.",
])
def test_a_checkable_time_sensitive_claim_is_searched_once(web, text):
    calls, _ = web
    grounding = radar.context_grounding(_post(text))
    assert len(calls) == 1 and calls[0]["query"] == text
    assert grounding is not None and grounding["checked"]["search"] in ("used", "nothing_found")


@pytest.mark.parametrize("text", [
    "Honestly, Kimi K3 might be why Opus 5 gets delayed.",      # opinion
    "Is Kimi K3 actually better than Opus 5?",                 # a question
    "Just shipped a new feature in my app",                    # nothing checkable
    "I think Gemini 3 is overrated",                           # opinion
    "Unbelievable excited for what's coming together. Tomorrow is feeling codexy",
    # The writer's own experience: no web page can confirm it.
    "Our AWS bill dropped 40% after we turned off the staging cluster at night.",
    "Made my first $1k from a side project. Took 14 months.",
    # A figure about nothing a page would be about.
    "Just hit 10k stars. Still can't believe people use this thing.",
    "Cold emails with no links get twice the replies. Tested on 400 sends.",
])
def test_opinions_questions_and_hype_never_cost_a_search(web, text):
    calls, _ = web
    radar.context_grounding(_post(text))
    assert calls == []


def test_no_search_when_the_post_links_a_page(web, monkeypatch):
    calls, _ = web
    monkeypatch.setattr(research, "fetch_page_preview", lambda url, *, timeout_s=None: {
        "text": "Moonshot released Kimi K3 today. " * 5, "title": "Kimi K3", "image": None, "description": None})
    grounding = radar.context_grounding(_post(links=["https://example.com/kimi-k3"]))
    assert calls == [] and grounding["checked"]["search"] == "not_needed"
    assert grounding["checked"]["links_read"] == 1


def test_no_search_when_the_news_pool_already_has_the_story(web, monkeypatch):
    calls, _ = web
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [
        {"title": "Moonshot releases Kimi K3 with frontier coding scores", "summary": "Kimi K3 beats Opus 5.",
         "source": "The Decoder", "url": "https://example.com/decoder-kimi", "created_at": 1}])
    grounding = radar.context_grounding(_post())
    assert calls == [] and grounding["checked"]["search"] == "covered"
    assert grounding["checked"]["news_matches"] == 1


def test_a_protected_post_never_searches(web):
    calls, _ = web
    assert radar.context_grounding(_post(visibility="protected")) is None
    assert calls == []


def test_research_off_never_searches(web, monkeypatch):
    calls, _ = web
    monkeypatch.setattr(research, "research_enabled", lambda: False)
    assert radar.context_grounding(_post()) is None
    assert calls == []


def test_only_the_claim_check_may_spend_a_search(web):
    """Every other caller keeps the empty answer it always had: no path spends by accident."""
    calls, _ = web
    assert research.search_web(CLAIM) == []
    assert research.search_web(CLAIM, 5) == []
    from app import analyzer, release_radar, source_facts
    release_radar._STATUS_CACHE.clear()
    release_radar.release_status("Kimi K3")
    source_facts.facts_for("Moonshot releases Kimi K3", None) if hasattr(source_facts, "facts_for") else None
    analyzer.verify_claim(_post())
    assert calls == [], "only radar.context_grounding may search"
    assert research.search_web(CLAIM, claim_check=True)[0]["url"] == RESULTS[0]["url"]
    assert len(calls) == 1


# --------------------------------------------------------------------------- timing


def test_the_search_runs_beside_the_link_reads_and_the_pool(web, monkeypatch):
    calls, state = web
    state["delay"] = 0.6
    started = time.monotonic()
    grounding = radar.context_grounding(_post())
    elapsed = time.monotonic() - started
    assert calls and calls[0]["at"] - started < 0.5 + radar.POOL_MATCH_WAIT_S, "started at once"
    assert elapsed < 0.6 + 0.5, f"collected when it answered, not after other work ({elapsed:.2f}s)"
    assert grounding["checked"]["search"] == "used"


def test_a_late_search_is_dropped_at_its_budget(web, monkeypatch):
    calls, state = web
    state["delay"] = 1.5
    monkeypatch.setattr(radar, "CLAIM_SEARCH_BUDGET_S", 0.4)
    started = time.monotonic()
    grounding = radar.context_grounding(_post())
    assert time.monotonic() - started < 1.0, "the press does not wait past the search budget"
    assert grounding is not None and grounding["checked"]["search"] == "late"
    assert grounding["evidence"] == "" and grounding["sources"] == []
    assert 0.0 < calls[0]["timeout_s"] <= 0.4, "the service is told what is left of the same budget"


# --------------------------------------------------------------------------- the press


FACT_REPLY = "a 71.2 vs 69.8 swe-bench lead is inside the noise, the licence is the real story"
# Without facts a figure would be invented, and the integrity gate rightly refuses it.
PLAIN_REPLY = "a benchmark lead that small is inside the noise, the licence is the real story"


def _press(monkeypatch, context, text=FACT_REPLY):
    prompts: list[str] = []

    def chat(model, messages, *, options=None, fmt=None):
        prompts.append(messages[-1]["content"])
        props = list(((fmt or {}).get("json_schema") or {}).get("schema", {}).get("properties", {}))
        answer = {name: "adds the consequence" for name in props}
        answer.update(verdict="act", confidence=0.9, why="the benchmark claim is checkable",
                      point="The benchmark lead is small.", text=text)
        return json.dumps({k: answer[k] for k in props}), {}

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:9")
    cloud.set_services_token("t" * 43)
    monkeypatch.setattr(cloud, "chat", chat)
    providers.reset_router()
    try:
        result = asyncio.run(main.browser_assist(BrowserAssistRequest(mode="context", context=context)))
    finally:
        cloud.reset()
        providers.reset_router()
    return result["decision"], prompts


def test_search_facts_reach_the_writer_and_the_card_links_them(web, monkeypatch):
    decision, prompts = _press(monkeypatch, _post())
    assert decision["action"] == "reply" and decision.get("draft")
    facts = prompts[0].split("Facts you may use", 1)[1]
    assert "71.2%" in facts and "From the web (Moonshot releases Kimi K3)" in facts
    assert decision["web_checked"] is True
    assert decision["sources"] == [
        {"url": "https://news.example.com/kimi-k3", "title": "Moonshot releases Kimi K3"},
        {"url": "https://example.com/kimi-k3-card", "title": "Kimi K3 model card"},
    ]


def test_a_late_search_leaves_no_sources_and_the_card_says_so(web, monkeypatch):
    calls, state = web
    state["delay"] = 1.5
    monkeypatch.setattr(radar, "CLAIM_SEARCH_BUDGET_S", 0.4)
    decision, prompts = _press(monkeypatch, _post(), PLAIN_REPLY)
    assert decision.get("draft"), decision
    assert "Facts you may use" not in prompts[0]
    assert decision["web_checked"] is False and "sources" not in decision
    # Its own field, shown in the card's note line (the card keeps `reasons` behind "Why?").
    assert "did not come back in time" in decision["search_note"]


def test_a_used_up_search_allowance_is_said_plainly(web, monkeypatch):
    calls, state = web
    state["answer"] = (None, "capped")
    decision, _ = _press(monkeypatch, _post(), PLAIN_REPLY)
    assert decision.get("draft"), decision
    assert "sources" not in decision
    assert "Web search is used up" in decision["search_note"]


def test_the_card_lists_only_pages_whose_facts_reached_the_writer():
    class Request:
        facts = "From the web (A): alpha fact one here for the writer. From the page the post links (B): beta."

    grounding = {"sources": [
        {"kind": "news", "url": "https://n.example/1", "title": "N", "fact": "In the news (X): never made it"},
        {"kind": "search", "url": "https://a.example/1", "title": "A",
         "fact": "From the web (A): alpha fact one here for the writer."},
        {"kind": "link", "url": "https://b.example/1", "title": "B", "fact": "From the page the post links (B): beta."},
        {"kind": "search", "url": "javascript:alert(1)", "title": "J", "fact": "From the web (A): alpha"},
    ]}
    assert main._decision_sources(Request(), grounding) == [
        {"url": "https://a.example/1", "title": "A"}, {"url": "https://b.example/1", "title": "B"}]
    Request.facts = ""
    assert main._decision_sources(Request(), grounding) == []


# --------------------------------------------------------------------------- the wire


def test_cloud_search_is_a_press_call_and_reads_the_refusals(monkeypatch):
    seen: list[httpx.Request] = []
    answers = iter([
        httpx.Response(200, json={"answer": "a", "sources": RESULTS}),
        httpx.Response(429, json={"error": {"code": "search_cap_exceeded", "message": "used"}}),
        httpx.Response(503, json={"error": {"code": "ai_unavailable",
                                            "message": "the AI provider did not answer in time"}}),
        httpx.Response(503, json={"error": {"code": "ai_unavailable", "message": "the AI provider is unavailable"}}),
        httpx.Response(503, json={"error": {"code": "search_unavailable", "message": "off"}}),
    ])

    def handler(request):
        seen.append(request)
        return next(answers)

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    cloud.set_services_token("n" * 43)
    monkeypatch.setattr(cloud, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler),
                                                               base_url="http://services"))
    try:
        assert cloud.search(CLAIM) == (None, "refused") and seen == [], "outside a press: no request"
        with request_context.bind("res_search_1"):
            statuses = [cloud.search(CLAIM)[1] for _ in range(5)]
    finally:
        cloud.reset()
    assert statuses == ["ok", "capped", "late", "unavailable", "refused"]
    assert seen[0].url.path == "/v1/search" and seen[0].headers["x-starling-request-id"] == "res_search_1"
    body = json.loads(seen[0].content)
    assert body["query"] == CLAIM and 1.0 <= body["timeout_s"] <= cloud.SEARCH_TIMEOUT_S


def test_chat_research_sources_have_the_link_shape():
    source = main.__file__
    text = open(source, encoding="utf-8").read()
    assert '"sources": cited' in text, "chat research sources are normalised to {url, title}"


# --------------------------------------------------------------------------- no fact without its link


def test_the_writer_gets_facts_only_from_the_pages_the_card_lists():
    facts = [
        {"kind": "news", "title": "N1", "url": "https://n.example/1", "fact": "In the news (N): one. " * 5},
        {"kind": "news", "title": "N0", "url": "", "fact": "In the news (X): a story with no page at all."},
        {"kind": "link", "title": "L1", "url": "https://l.example/1", "fact": "From the page the post links (L1): x. " * 4},
        {"kind": "search", "title": "S1", "url": "https://s.example/1", "fact": "From the web (S1): searched. " * 3},
        {"kind": "search", "title": "S2", "url": "https://s.example/2", "fact": "From the web (S2): searched too. " * 3},
    ]
    chosen = radar._listed_facts(facts)
    assert [item["title"] for item in chosen] == ["S1", "S2", "L1"], "search first, then the post's links"
    assert all(item["url"] for item in chosen) and len(chosen) <= radar.MAX_LISTED_SOURCES
    assert len(" ".join(item["fact"] for item in chosen)) <= radar._GROUNDING_EVIDENCE_CAP


def test_every_fact_in_the_writers_block_has_its_page_in_decision_sources(web, monkeypatch):
    """The coordinator's rule (W4): cap the facts block itself at the pages that will be listed."""
    import re as _re
    pages = {
        "https://example.com/kimi-k3-launch": ("Kimi K3 launch", " ".join(
            f"Moonshot says Kimi K3 launch detail {n} is part of the release notes today." for n in range(12))),
        "https://example.com/kimi-k3-pricing": ("Kimi K3 pricing", " ".join(
            f"Kimi K3 pricing detail {n} is listed by Moonshot on the pricing page." for n in range(12))),
    }
    monkeypatch.setattr(research, "fetch_page_preview", lambda url, *, timeout_s=None: {
        "text": pages[url][1], "title": pages[url][0], "image": None, "description": None})
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [
        {"title": "Moonshot's Kimi K3 tops the SWE-bench table", "summary": "Kimi K3 from Moonshot leads.",
         "source": "The Decoder", "url": "https://news.example.com/kimi-1", "created_at": 1},
        {"title": "Kimi K3 weights from Moonshot are open", "summary": "Moonshot opened the Kimi K3 weights.",
         "source": "HN", "url": "https://news.example.com/kimi-2", "created_at": 2},
    ])
    context = _post("Moonshot put out Kimi K3, details below.", links=list(pages))
    decision, prompts = _press(monkeypatch, context, PLAIN_REPLY)
    listed = decision["sources"]
    assert 2 <= len(listed) <= 3, listed
    assert [source["url"] for source in listed][:2] == list(pages), "the post's own pages come first"
    block = prompts[0].split("Facts you may use", 1)[1].split("\n", 2)[1]
    segments = [s.strip() for s in _re.split(r"(?=From the page the post links \(|In the news \(|From the web \()", block)
                if s.strip()]
    titles = [source["title"] for source in listed]
    for segment in segments:
        assert any(title in segment for title in titles), f"a fact reached the writer without its link: {segment[:80]}"
    assert len(segments) == len(listed)
    for story in ("tops the SWE-bench table", "weights from Moonshot are open"):
        if not any(story in title for title in titles):
            assert story not in block, "an unlisted page's fact reached the writer"
