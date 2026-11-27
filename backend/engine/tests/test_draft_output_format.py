"""S2: the draft call asks for ONE object in a strict schema, sized so it cannot be cut.

A real API model honours `max_tokens`. The old call asked for up to three candidates with
seven fields each under a 200-token cap, and on a model that stops at the cap every attempt
came back cut mid-JSON, unparseable, and the ✦ press ended in WAIT. These pin the new shape:
one candidate, only the fields the engine reads, a cap sized from what that object holds,
and a cut answer that is salvaged when its text closed and refused when it did not.
"""
from __future__ import annotations

import json
import math

from app import cloud
from app.llm import (MAX_OUTPUT_TOKENS, POST_LIMIT, ModelClient, draft_fields,
                     draft_response_format, output_budget, parse_draft_object)
from app.schemas import DraftRequest, PostContext


def _request(action: str = "reply", **extra) -> DraftRequest:
    return DraftRequest(action=action,
                        context=PostContext(author="A Dev", author_handle="adev",
                                            text="Been running Qwen locally for a month. The bottleneck was memory bandwidth.",
                                            visibility="public"),
                        research_mode="off", media_policy="no_media", grok_permission=False, **extra)


def _client(monkeypatch, answer):
    client = ModelClient()
    sent: list[dict] = []

    def chat(messages, *, options=None, fmt=None, think=False, lane="quality"):
        sent.append({"messages": messages, "options": dict(options or {}), "fmt": fmt})
        return answer(len(sent)) if callable(answer) else answer

    monkeypatch.setattr(client, "status", lambda: {"available": True})
    monkeypatch.setattr(client.router, "chat", chat)
    return client, sent


def test_the_draft_call_asks_for_one_strict_object_with_only_the_fields_read(monkeypatch):
    client, sent = _client(monkeypatch, json.dumps({"point": "p", "text": "bandwidth is the ceiling, not the weights", "rationale": "r"}))
    candidates, _sources, _status = client.generate(_request("reply"), ["one of mine"], "hint", {})
    assert [candidate["text"] for candidate in candidates] == ["bandwidth is the ceiling, not the weights"]
    fmt = sent[0]["fmt"]
    assert fmt["type"] == "json_schema"
    spec = fmt["json_schema"]
    assert spec["strict"] is True and spec["name"]
    schema = spec["schema"]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert list(schema["properties"]) == ["point", "text", "rationale"]
    assert schema["required"] == ["point", "text", "rationale"]
    # No envelope of several candidates, no fields the engine derives itself.
    for dropped in ("candidates", "strategic_role", "hook", "target_objective", "grok_suggested"):
        assert dropped not in json.dumps(schema)
    assert "Return exactly one JSON object" in sent[0]["messages"][-1]["content"]


def test_a_post_carries_no_point_and_its_cap_matches_its_fields(monkeypatch):
    client, sent = _client(monkeypatch, json.dumps({"text": "a post", "rationale": "r"}))
    client.generate(_request("post"), [], "hint", {})
    schema = sent[0]["fmt"]["json_schema"]["schema"]
    assert list(schema["properties"]) == ["text", "rationale"]
    assert sent[0]["options"]["num_predict"] == output_budget(_request("post"), ("text", "rationale"))


def test_the_cap_holds_a_full_length_draft_with_room_to_spare():
    """A text past X's limit, a full point and a full rationale must still close their JSON."""
    worst = json.dumps({"point": "w" * 120, "text": "x" * int(POST_LIMIT * 1.4), "rationale": "y" * 130})
    needed = math.ceil(len(worst) / 3.5)
    assert output_budget(_request("reply"), draft_fields(_request("reply"))) >= needed
    assert output_budget(_request("reply"), draft_fields(_request("reply"))) <= MAX_OUTPUT_TOKENS
    thread = _request("post", thread_requested=True)
    assert output_budget(thread, draft_fields(thread)) == MAX_OUTPUT_TOKENS


def test_a_long_enhance_gets_room_for_its_own_length():
    long_draft = "word " * 180
    request = DraftRequest(action="post", context=PostContext(author="You", text=long_draft, visibility="public"),
                           enhancement_mode=True, original_draft=long_draft)
    assert output_budget(request, draft_fields(request)) > output_budget(_request("post"), ("text", "rationale"))


def test_an_object_cut_inside_its_rationale_keeps_the_draft():
    cut = '{"point": "the point", "text": "bandwidth caps tokens per second first", "rationale": "Adds the conse'
    parsed = parse_draft_object(cut)
    assert parsed is not None and parsed["text"] == "bandwidth caps tokens per second first"
    assert parsed["point"] == "the point" and parsed.get("salvaged") is True


def test_an_object_cut_inside_its_text_is_refused():
    assert parse_draft_object('{"point": "the point", "text": "bandwidth caps tokens per sec') is None
    assert parse_draft_object('{"text": "') is None
    assert parse_draft_object("") is None


def test_escapes_in_a_salvaged_text_are_decoded():
    parsed = parse_draft_object('{"text": "two lines\\nand a \\"quote\\"", "rationale": "cut')
    assert parsed["text"] == 'two lines\nand a "quote"'


def test_the_legacy_envelope_is_read_as_its_first_candidate_only():
    raw = json.dumps({"candidates": [{"text": "first"}, {"text": "second"}]})
    assert parse_draft_object(raw)["text"] == "first"


def test_generate_turns_a_cut_text_into_no_draft_and_a_cut_rationale_into_a_draft(monkeypatch):
    client, _sent = _client(monkeypatch, '{"point": "p", "text": "half a sente')
    candidates, _sources, _status = client.generate(_request("reply"), [], "hint", {})
    assert candidates == []
    client, _sent = _client(monkeypatch, '{"point": "p", "text": "a whole reply that closed", "rationale": "because it')
    candidates, _sources, _status = client.generate(_request("reply"), [], "hint", {})
    assert [candidate["text"] for candidate in candidates] == ["a whole reply that closed"]


def test_the_services_payload_carries_the_schema_unchanged():
    fmt = draft_response_format(("text", "rationale"))
    payload = cloud.build_payload("starling-quality", [{"role": "user", "content": "hi"}],
                                  {"num_predict": 219}, stream=False, fmt=fmt)
    assert payload["response_format"] == fmt
    assert payload["max_tokens"] == 219
    # A malformed hint is dropped rather than sent as something the proxy refuses.
    bad = cloud.build_payload("starling-quality", [{"role": "user", "content": "hi"}], {}, stream=False,
                              fmt={"type": "json_schema"})
    assert "response_format" not in bad
    legacy = cloud.build_payload("starling-quality", [{"role": "user", "content": "hi"}], {}, stream=False, fmt="json")
    assert legacy["response_format"] == {"type": "json_object"}


def test_the_stream_asks_for_a_one_field_metadata_tail(monkeypatch):
    client = ModelClient()
    seen: dict = {}

    def chat_stream(messages, *, options=None, think=False, stop_event=None, lane="quality"):
        seen["prompt"] = messages[-1]["content"]
        seen["options"] = dict(options or {})
        yield {"delta": "a draft\n@@META@@\n{\"rationale\": \"short\"}"}
        yield {"done": True, "timing": {}}

    monkeypatch.setattr(client, "status", lambda: {"available": True})
    monkeypatch.setattr(client.router, "chat_stream", chat_stream)
    raw = client.generate_stream(_request("post"), [], "hint", {}, lambda _text: None)
    assert raw["text"] == "a draft" and raw["rationale"] == "short"
    assert '{"rationale":' in seen["prompt"] and "target_objective" not in seen["prompt"]
    assert seen["options"]["num_predict"] == output_budget(_request("post"), ("text", "rationale"), stream=True)
