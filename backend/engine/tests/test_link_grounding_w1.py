"""W1 + W2: a reply is checked against the post's own links and the news pool, and the
facts reach the writer in a block of their own.

`research.search_web` has always returned [] in the hosted engine, so context grounding
never produced evidence and every reply and quote said "No web check on this one" — even
a post whose whole substance was the article it linked, which `/v1/fetch` could read.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import cloud, main, news, providers, radar, research
from app.schemas import BrowserAssistRequest, MediaItem, PostContext

NOW = datetime.now(timezone.utc)
PAGE = ("Example Labs released Orca 2, an open-weight coding model under the Apache 2.0 "
        "licence. It scores 71% on the lab's own agentic coding benchmark and ships with an "
        "inference guide for a single 24 GB card.")


def _link_post(**extra) -> PostContext:
    base = dict(post_id="1790000000000000901", author="Lab News", author_handle="labnews",
                text="Our new open model is out today. Details here:",
                links=["https://example.com/blog/orca-2"], visibility="public",
                post_created_at=(NOW - timedelta(minutes=10)).isoformat(),
                visible_metrics={"views": 22000, "likes": 900, "replies": 120},
                thread_scanned=True, visible_reply_count=120, visible_reply_ages_minutes=[1, 2, 3])
    base.update(extra)
    return PostContext(**base)


@pytest.fixture()
def world(monkeypatch):
    fetched: list[str] = []

    def preview(url, *, timeout_s=None):
        fetched.append(url)
        assert timeout_s is not None and timeout_s <= radar.LINK_READ_BUDGET_S
        return {"text": PAGE, "title": "Orca 2 is out", "image": None, "description": None}

    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "fetch_page_preview", preview)
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    return fetched


def test_the_posts_own_link_is_read_and_becomes_evidence(world):
    grounding = radar.context_grounding(_link_post())
    assert world == ["https://example.com/blog/orca-2"]
    assert grounding is not None and "Apache 2.0" in grounding["evidence"]
    assert grounding["sources"][0]["url"] == "https://example.com/blog/orca-2"
    assert grounding["checked"]["links_read"] == 1


def test_a_link_card_counts_and_x_links_are_never_fetched(world):
    context = _link_post(links=["https://x.com/labnews/status/1", "https://twitter.com/a/status/2"],
                         media=[MediaItem(url="https://example.com/blog/orca-2", kind="link_card")])
    radar.context_grounding(context)
    assert world == ["https://example.com/blog/orca-2"]


def test_a_protected_post_is_never_fetched(world):
    assert radar.context_grounding(_link_post(visibility="protected")) is None
    assert world == []


def test_the_news_pool_is_matched_on_shared_subject_words(monkeypatch, world):
    stories = [
        {"title": "Moonshot releases Kimi K3 with frontier coding scores", "summary": "Kimi K3 leads coding.",
         "source": "The Decoder", "url": "https://example.com/kimi-k3", "created_at": 1},
        {"title": "A bakery opens downtown", "summary": "Fresh bread daily.", "source": "Local",
         "url": "https://example.com/bread", "created_at": 2},
    ]
    monkeypatch.setattr(news, "headlines", lambda *a, **k: stories)
    context = PostContext(author="Dev", author_handle="dev", visibility="public", post_id="k1",
                          text="Honestly, Kimi K3 from Moonshot might be the best coding model this month.")
    grounding = radar.context_grounding(context)
    assert grounding is not None
    assert "Kimi K3" in grounding["evidence"] and "bakery" not in grounding["evidence"].lower()
    assert grounding["checked"]["news_matches"] == 1


def test_a_lookup_question_is_answered_from_the_news_pool_and_says_so(monkeypatch):
    """W3: say what is actually checked. No web search exists; the news pool does."""
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [
        {"title": "Anthropic ships Claude Opus 5 with a longer context window", "summary": "Claude Opus 5 is out.",
         "source": "The Decoder", "url": "https://example.com/opus-5", "created_at": 0}])
    answer = main._web_lookup_answer("whats the latest on claude opus")
    assert answer and "no general web search" in answer
    assert "Claude Opus 5" in answer and "https://example.com/opus-5" in answer
    assert main._web_lookup_answer("whats the latest on woodworking chisels") is None


def test_no_message_tells_the_user_to_open_x_explore():
    """T3: nothing captures X trends, so the product must not promise it does."""
    import pathlib
    for name in ("main.py", "radar.py", "topics.py", "llm.py"):
        source = (pathlib.Path(main.__file__).parent / name).read_text(encoding="utf-8")
        for line in source.splitlines():
            if "x.com/explore" in line:
                assert line.lstrip().startswith("#"), f"{name}: {line.strip()}"


def test_the_press_hands_the_link_facts_to_the_writer_in_their_own_block(monkeypatch, world):
    prompts: list[str] = []

    def chat(model, messages, *, options=None, fmt=None):
        prompts.append(messages[-1]["content"])
        props = list(((fmt or {}).get("json_schema") or {}).get("schema", {}).get("properties", {}))
        answer = {name: "adds the consequence" for name in props}
        answer.update(verdict="act", confidence=0.9, why="the licence is the real news",
                      point="The Apache 2.0 licence matters more than the benchmark.",
                      text="the apache 2.0 licence on orca 2 matters more than the 71% benchmark number")
        return json.dumps({k: answer[k] for k in props}), {}

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:9")
    cloud.set_services_token("t" * 43)
    monkeypatch.setattr(cloud, "chat", chat)
    providers.reset_router()
    try:
        result = asyncio.run(main.browser_assist(BrowserAssistRequest(mode="context", context=_link_post())))
    finally:
        cloud.reset()
        providers.reset_router()
    decision = result["decision"]
    assert prompts, "the model was asked"
    assert "Facts you may use" in prompts[0] and "Apache 2.0" in prompts[0]
    assert "USE THESE FACTS" not in prompts[0].split("Facts you may use")[0], "facts left the direction"
    assert decision["web_checked"] is True
    assert "cannot read it" not in json.dumps(decision)
