"""Threads are a deliberate, user-requested capability: rules swap only when
asked, parts parse cleanly, every part is gated, and the hook post is scored
without the fake-inline-thread penalty."""
from __future__ import annotations

import json

from app.growth_scorer import score_candidate
from app.llm import ModelClient, SINGLE_POST_RULE, THREAD_RULES, _draft_rules, split_thread_parts
from app.schemas import DraftRequest, PostContext


def _request(thread: bool) -> DraftRequest:
    return DraftRequest(action="post", thread_requested=thread,
                        context=PostContext(author="a", text="agents in production work", visibility="public"))


def test_rules_swap_only_when_thread_requested():
    assert SINGLE_POST_RULE in _draft_rules(_request(False))
    assert THREAD_RULES not in _draft_rules(_request(False))
    assert THREAD_RULES in _draft_rules(_request(True))
    assert SINGLE_POST_RULE not in _draft_rules(_request(True))


def test_split_thread_parts():
    text = "Part one hook about agents.\n---\nPart two with the evidence.\n --- \nPart three payoff."
    parts = split_thread_parts(text)
    assert len(parts) == 3
    assert parts[0].startswith("Part one")
    assert split_thread_parts("Just a single post, no separator.") == ["Just a single post, no separator."]


class _FakeRouter:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_timing = {}

    def chat(self, messages, *, options, fmt=None, think=False, lane="quality"):
        return self.content


def test_generate_attaches_thread_parts(monkeypatch):
    client = ModelClient()
    thread_text = ("Agents fail in production for one boring reason.\n---\n"
                   "The failure is state, not intelligence: every retry loses the context that mattered.\n---\n"
                   "Fix the state layer first; the model was never the bottleneck.")
    payload = json.dumps({"candidates": [{"text": thread_text, "rationale": "r", "hook": "h"}]})
    monkeypatch.setattr(client, "router", _FakeRouter(payload))
    monkeypatch.setattr(client, "status", lambda: {"available": True})
    candidates, _, _ = client.generate(_request(True), [], "", {})
    assert candidates[0]["thread_parts"] and len(candidates[0]["thread_parts"]) == 3
    assert candidates[0]["text"] == candidates[0]["thread_parts"][0]


def test_thread_hook_skips_marker_penalty():
    hook = "Agents fail in production for one boring reason 🧵"
    penalized, _ = score_candidate(hook, action="post", objective="followers", context_text="agents production")
    exempt, _ = score_candidate(hook, action="post", objective="followers", context_text="agents production",
                                thread_hook=True)
    assert exempt > penalized


def test_thread_candidates_gate_every_part():
    from app.intelligence import _assemble_candidates, get_personal_model
    from app.safety import assess_context
    request = _request(True)
    model = get_personal_model()
    context_safety = assess_context(request.context)
    bad_thread = [{
        "text": "Most production agents fail on state handling, not on model quality.",
        "thread_parts": ["Most production agents fail on state handling, not on model quality.",
                         "Like and repost if you agree with this — follow me for more!"],
    }]
    result = _assemble_candidates(bad_thread, [], "unavailable", request, [], model,
                                  {"objective": "followers"}, context_safety)
    assert result == []  # the bait part sinks the whole thread
    good_thread = [{
        "text": "Most production agents fail on state handling, not on model quality.",
        "thread_parts": ["Most production agents fail on state handling, not on model quality.",
                         "The real failure mode in production agents is state handling, not model quality."],
    }]
    result = _assemble_candidates(good_thread, [], "unavailable", request, [], model,
                                  {"objective": "followers"}, context_safety)
    assert len(result) == 1
    assert len(result[0].thread_parts) == 2
