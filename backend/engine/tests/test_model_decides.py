"""D1 + S3: on a ✦ press the model decides, in the same call that drafts.

The decision used to be keyword lists: on 30 fresh, public, repliable posts it refused
21 before any model saw them ("I could not find a point worth adding to this one").
Now the first draft call also returns a verdict; only a confident model "wait" or a
deterministic hard stop gives WAIT, and the reply's point comes back in the same object
(no separate point call).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import cloud, main, providers
from app.schemas import BrowserAssistRequest, PostContext

NOW = datetime.now(timezone.utc)
GOOD = "so the upgrade path for local qwen is faster memory, not a bigger model"


@pytest.fixture()
def model_calls(monkeypatch):
    """A stand-in services proxy: records every call and answers from a script."""
    calls: list[dict] = []
    script: dict = {"verdict": "act", "confidence": 0.9, "text": GOOD}

    def chat(model, messages, *, options=None, fmt=None):
        calls.append({"messages": messages, "fmt": fmt, "options": dict(options or {})})
        props = ((fmt or {}).get("json_schema") or {}).get("schema", {}).get("properties", {}) \
            if isinstance(fmt, dict) else {}
        answer = {}
        for name in props:
            if name == "verdict":
                answer[name] = script["verdict"]
            elif name == "confidence":
                answer[name] = script["confidence"]
            elif name == "why":
                answer[name] = "a concrete point to add about local inference"
            elif name == "point":
                answer[name] = "Faster memory, not a bigger model, is the local upgrade path."
            elif name == "text":
                deciding_wait = "verdict" in props and script["verdict"] == "wait"
                answer[name] = "" if deciding_wait else script["text"]
            else:
                answer[name] = "adds the consequence"
        return json.dumps(answer) if props else "NONE", {"total_ms": 5.0}

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:9")
    cloud.set_services_token("t" * 43)
    monkeypatch.setattr(cloud, "chat", chat)
    providers.reset_router()
    yield calls, script
    cloud.reset()
    providers.reset_router()


def _context(**extra) -> PostContext:
    base = dict(post_id="1790000000000000777", author="Some Dev", author_handle="somedev",
                text="Been running Qwen locally for a month. The bottleneck was never the model, "
                     "it was memory bandwidth.",
                post_created_at=(NOW - timedelta(minutes=18)).isoformat(), visibility="public",
                visible_metrics={"views": 5200, "likes": 140, "replies": 18},
                thread_scanned=True, visible_reply_count=18, visible_reply_ages_minutes=[2, 5, 9])
    base.update(extra)
    return PostContext(**base)


def _press(context: PostContext) -> dict:
    return asyncio.run(main.browser_assist(BrowserAssistRequest(mode="context", context=context)))


def test_the_reply_is_decided_and_drafted_in_one_call(model_calls):
    calls, _script = model_calls
    result = _press(_context())
    decision = result["decision"]
    assert decision["action"] == "reply" and decision["draft"] == GOOD
    assert decision.get("model_decided") is True
    assert len(calls) == 1, "no separate point call (S3) and no separate decision call (D1)"
    props = list(calls[0]["fmt"]["json_schema"]["schema"]["properties"])
    assert props[:3] == ["verdict", "why", "confidence"] and props.index("point") < props.index("text")
    prompt = calls[0]["messages"][-1]["content"]
    assert "DECIDE FIRST, THEN WRITE" in prompt and "minutes ago" in prompt


def test_a_post_the_keywords_refused_now_reaches_the_model(model_calls):
    calls, script = model_calls
    script["text"] = "three rewrites is the real onboarding tax, the walk is where the last one gets decided"
    context = _context(post_id="1790000000000000778", author="Dev Lead", author_handle="devlead",
                       text="Shipped the new onboarding flow today. Took three rewrites and one very long walk.")
    decision = _press(context)["decision"]
    assert calls, "the model was asked"
    assert decision["action"] == "reply" and decision["draft"]
    assert "could not find a point worth adding" not in decision["rationale"]


def test_a_confident_model_wait_is_a_wait_with_its_reason_and_no_draft(model_calls):
    calls, script = model_calls
    script.update(verdict="wait", confidence=0.9)
    result = _press(_context(post_id="1790000000000000779"))
    decision = result["decision"]
    assert decision["action"] == "wait" and not decision.get("draft")
    assert decision["rationale"] == "a concrete point to add about local inference"
    assert decision.get("model_decided") is True
    assert result.get("generation_id") is None
    assert len(calls) == 1


def test_an_unsure_model_wait_does_not_refuse_the_press(model_calls):
    calls, script = model_calls
    script.update(verdict="wait", confidence=0.4)
    decision = _press(_context(post_id="1790000000000000780"))["decision"]
    assert decision["action"] == "reply", "below 0.7 a model 'wait' never refuses"
    assert len(calls) >= 2, "the next attempt drafts"


def test_a_hard_stop_never_reaches_the_model(model_calls):
    calls, _script = model_calls
    ad = _context(post_id="1790000000000000781",
                  text="Use code SAVE50 for 50% off our course. Link in bio. Limited seats, buy now!")
    decision = _press(ad)["decision"]
    assert decision["action"] in ("wait", "skip")
    assert calls == [], "an ad is a deterministic stop"
    dead = _context(post_id="1790000000000000782",
                    post_created_at=(NOW - timedelta(hours=30)).isoformat(),
                    visible_reply_ages_minutes=[900, 1100, 1300], visible_reply_count=40)
    decision = _press(dead)["decision"]
    assert decision["action"] in ("wait", "skip") and calls == []
