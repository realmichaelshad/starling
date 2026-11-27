"""Candidate variety without temperature.

Hosted models may ignore sampling parameters, so a retry that only warmed the
temperature could hand back the same draft. Variety now lives in the prompt: a
retry is told how the earlier candidates opened and asked for a different
opening and angle, and identical drafts collapse into one candidate.
"""
from __future__ import annotations

import pytest

from app import intelligence, llm as llm_module
from app.llm import ModelClient, _sampling_for, variation_note
from app.schemas import DraftCandidate, DraftRequest, PostContext


def _request(action: str = "post") -> DraftRequest:
    return DraftRequest(action=action,
                        context=PostContext(author="A", text="A visible public post about agents", visibility="public"),
                        research_mode="off", media_policy="no_media", grok_permission=False)


def _candidate(text: str, score: float = 0.5, cid: str = "c") -> DraftCandidate:
    return DraftCandidate(id=cid, text=text, performance={}, rationale=["r"], retrieved_examples=[],
                          sources=[], growth_score=score)


@pytest.mark.parametrize("action", ["reply", "quote", "post"])
def test_retries_keep_the_same_sampling_hint(action):
    request = _request(action)
    first = _sampling_for(request, 0)
    assert _sampling_for(request, 1) == first
    assert _sampling_for(request, 2) == first


def test_the_first_attempt_carries_no_variation():
    assert variation_note(0, ["anything at all"]) == ""
    assert intelligence._retry_note(_request("post"), [], 0, tried=["anything"]) == ""


def test_every_retry_names_the_earlier_openings_and_asks_for_a_new_move():
    earlier = ["Agents are finally cheap enough to run all day.",
               "agents are finally cheap enough to run all day!"]
    second = variation_note(1, earlier)
    third = variation_note(2, [*earlier, "Nobody budgets for the retries an agent makes."])
    assert "attempt 2" in second and "attempt 3" in third
    assert second.count('"Agents are finally cheap enough to') == 1, "the same opening is named once"
    assert '"Nobody budgets for the retries an' in third
    assert second != third
    moves = [move for move in llm_module._VARIATION_MOVES if move in second or move in third]
    assert len(moves) == 2, "each retry asks for a different opening move"


def test_retry_prompts_differ_from_the_first_and_from_each_other(monkeypatch):
    client = ModelClient()
    prompts: list[str] = []

    def capture(messages, *, options, fmt=None, think=False, lane="quality"):
        prompts.append(messages[-1]["content"])
        return '{"candidates": [{"text": "Agents are finally cheap enough to run all day."}]}'

    monkeypatch.setattr(client, "status", lambda: {"available": True})
    monkeypatch.setattr(client.router, "chat", capture)
    request = _request("post")
    tried: list[str] = []
    for attempt in range(3):
        note = intelligence._retry_note(request, [], attempt, tried=tried)
        candidates, _sources, _status = client.generate(request, [], "hint", {}, attempt=attempt, retry_note=note)
        tried.extend(candidate["text"] for candidate in candidates)
    assert len(prompts) == 3
    assert len(set(prompts)) == 3, "every attempt must send a different prompt"
    assert "AN EARLIER ATTEMPT WAS NOT USED" not in prompts[0]
    assert 'It opened: "Agents are finally cheap enough to run all' in prompts[1]
    # The shared prefix is untouched: the variation rides at the tail.
    assert prompts[1].startswith(prompts[0].rsplit("Return exactly:", 1)[0].split("\n")[0])


# RETIRED (API-native pass, 2026-09-22): test_a_reply_retry_keeps_its_own_note_and_adds_the_variation: every surface's retry carries one note (why it was not used, how it opened).


def test_identical_drafts_are_not_both_kept():
    merged = intelligence._merge_ranked(
        [_candidate("Agents are cheap now.", 0.4, "c1")],
        [_candidate("  agents are CHEAP now!! ", 0.9, "c2"), _candidate("A different draft entirely.", 0.2, "c3")],
        action="post")
    assert [candidate.id for candidate in merged] == ["c1", "c3"]


def test_the_generation_loop_feeds_every_tried_draft_to_the_next_attempt(monkeypatch):
    """End to end through `generate_drafts`: the writer returns the same text twice,
    the second attempt is told what the first opened with, and the result holds
    that draft once."""
    notes: list[str] = []

    class Model:
        def performance_hint(self):
            return "hint"

    class Writer:
        def generate(self, request, examples, hint, brief, *, angle_instruction="", attempt=0, retry_note=""):
            notes.append(retry_note)
            return [{"text": "Same draft every single time."}], [], "unavailable"

    # API-native pass: an attempt after the first exists only because the drafts before it
    # failed an integrity check, so the first assembly rejects (and says why).
    def assemble(raw_candidates, sources, research_status, request, examples, model, brief, safety, llm,
                 revise_budget=None, rejections=None):
        if len(notes) == 1:
            rejections.append("copies_the_post")
            return []
        return [_candidate(raw["text"], 0.1, f"c{len(notes)}") for raw in raw_candidates]

    monkeypatch.setattr(intelligence, "get_personal_model", lambda: Model())
    monkeypatch.setattr(intelligence, "_voice_examples", lambda model, request, exclude=None: [])
    monkeypatch.setattr(intelligence, "_candidate_angles", lambda request, plan: [{}, {}])
    monkeypatch.setattr(intelligence, "CANDIDATE_PLAN", {"post": 2})
    monkeypatch.setattr(intelligence, "_assemble_candidates", assemble)
    monkeypatch.setattr(intelligence, "_support_checked", lambda best, request, llm: best)
    monkeypatch.setattr(intelligence, "EARLY_EXIT_SCORE", 10.0)
    result = intelligence.generate_drafts(_request("post"), Writer(), brief={})
    assert len(notes) == 2
    assert notes[0] == ""
    assert '"Same draft every single time."' in notes[1]
    assert "it handed the post back instead of answering it" in notes[1]
    assert [candidate.text for candidate in result] == ["Same draft every single time."]
