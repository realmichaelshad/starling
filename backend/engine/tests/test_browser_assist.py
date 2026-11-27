from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main, security, storage


class FakeModel:
    def __init__(self, chat_answer=None):
        self._chat_answer = chat_answer

    def status(self):
        return {"available": True}

    def strategy_chat(self, history, brief):
        if callable(self._chat_answer):
            return self._chat_answer(history, brief)
        return self._chat_answer or "Keep the point concrete and make the first line carry it."


class Candidate:
    def __init__(self, text: str, identifier: str = "candidate-1"):
        self.id = identifier
        self.text = text
        self.rationale = ["Keeps the user's point while making the phrasing cleaner."]

    def model_dump(self, mode="json"):
        return {"id": self.id, "text": self.text, "rationale": self.rationale}


def _api(monkeypatch):
    client = TestClient(main.app)
    client.__enter__()
    token = security.ensure_token()
    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    return client, {"Authorization": f"Bearer {token}"}


def test_chat_write_request_routes_through_gated_pipeline(monkeypatch):
    """'write me a post about X' in chat must use generate_drafts (gates,
    no-fabrication rules), never freestyle LLM text."""
    client, headers = _api(monkeypatch)
    seen = {}

    def gated(request, llm, *, brief=None):
        seen["request"] = request
        return [Candidate("Opus 5 predictions keep sliding — the only honest date is 'when it ships'.")]

    monkeypatch.setattr(main, "generate_drafts", gated)
    try:
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "chat", "message": "write me a post about claude opus 5"})
        body = response.json()
        assert response.status_code == 200
        assert seen["request"].action == "post"
        assert seen["request"].thread_requested is False
        assert "never invent releases" in seen["request"].direction
        assert body["answer"].startswith("Opus 5 predictions")
        assert body["decision"]["draft"] == body["answer"]
        assert body["generation_id"] and body["candidate_id"] == "candidate-1"
        tracked = storage.get_generation(body['generation_id'])
        assert tracked["action"] == "post" and tracked["candidates"][0]["text"] == body["decision"]["draft"]

        # Thread request flags the pipeline's thread mode.
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "chat", "message": "write me a thread about local models"})
        assert response.status_code == 200
        assert seen["request"].thread_requested is True

        # Gate rejection → honest refusal, not freestyle.
        monkeypatch.setattr(main, "generate_drafts", lambda request, llm, *, brief=None: [])
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "chat", "message": "write me a post about something"})
        assert "gates" in response.json()["answer"]
    finally:
        client.__exit__(None, None, None)


def test_chat_delete_without_context_answers_deterministically(monkeypatch):
    """No post open → the honest 'open it first' line, straight from the
    advisor — never model freestyle, never parroted meta-instructions."""
    client, headers = _api(monkeypatch)
    try:
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "chat", "message": "should i delete my last post?"})
        assert response.status_code == 200
        answer = response.json()["answer"]
        assert "Open the post" in answer
        assert "instruction" not in answer.lower()
        assert "first person" not in answer.lower()
    finally:
        client.__exit__(None, None, None)


def test_chat_brief_carries_account_facts(monkeypatch):
    client, headers = _api(monkeypatch)
    captured = {}

    def fake_chat(history, brief):
        captured["brief"] = brief
        return "ok"

    try:
        monkeypatch.setattr(main, "ModelClient", lambda: FakeModel(chat_answer=fake_chat))
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "chat", "message": "what should my strategy be this week?"})
        assert response.status_code == 200
        facts = captured["brief"].get("account_facts")
        assert facts is not None
        assert "posts_today" in facts and "now" in facts and "recent_posts" in facts

        # Pure fact questions short-circuit to a deterministic instant answer.
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "chat", "message": "what day is it today?"})
        body = response.json()
        assert body["answer"].startswith("It's ")
        assert body["decision"]["confidence"] == "strong"
    finally:
        client.__exit__(None, None, None)


def test_enhance_composer_returns_one_same_format_draft_and_persists_lifecycle(monkeypatch):
    client, headers = _api(monkeypatch)
    monkeypatch.setattr(main, "generate_drafts", lambda request, llm, *, brief=None: [Candidate("The actual test is whether it changes a real decision.")])
    try:
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "enhance_composer",
            "composer": {"action": "reply", "draft_text": "the real test is if it changes a decision"},
            "context": {"author": "A", "text": "Agents are changing developer work", "visibility": "public"},
        })
        body = response.json()
        assert response.status_code == 200
        assert body["decision"]["action"] == "reply"
        assert body["decision"]["native_intent"] == "reply"
        assert body["decision"]["draft"] == "The actual test is whether it changes a real decision."
        assert body["decision"]["action"] != "wait"
        assert body["generation_id"] and body["candidate_id"] == "candidate-1"
        generation = storage.get_generation(body['generation_id'])
        assert generation["action"] == "reply"
        assert generation["candidates"][0]["text"] == body["decision"]["draft"]
    finally:
        client.__exit__(None, None, None)


def test_enhance_composer_keeps_user_draft_when_quality_gate_has_no_candidate(monkeypatch):
    client, headers = _api(monkeypatch)
    monkeypatch.setattr(main, "generate_drafts", lambda request, llm, *, brief=None: [])
    try:
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "enhance_composer",
            "composer": {"action": "post", "draft_text": "models need to be useful not just impressive"},
        })
        body = response.json()
        assert response.status_code == 200
        assert body["decision"]["action"] == "post"
        assert body["decision"]["draft"] == "models need to be useful not just impressive"
        assert body["decision"]["action"] != "wait"
    finally:
        client.__exit__(None, None, None)


def test_enhancement_is_text_only_and_retrieves_only_quote_guidance(monkeypatch):
    client, headers = _api(monkeypatch)
    seen = {}

    def generate_once(request, llm, *, brief=None):
        seen["request"] = request
        return [Candidate("The quote matters because it exposes the tradeoff.", "quote-candidate")]

    monkeypatch.setattr(main, "generate_drafts", generate_once)
    try:
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "enhance_composer",
            "composer": {"action": "quote", "draft_text": "this shows the real tradeoff"},
            "context": {"author": "A", "text": "A useful source post", "quote_text": "The original claim", "visibility": "public"},
        })
        assert response.status_code == 200
        assert seen["request"].action == "quote"
        assert seen["request"].research_mode == "off"
        assert seen["request"].media_policy == "no_media"
        assert seen["request"].grok_permission is False
        assert seen["request"].enhancement_mode is True
        assert seen["request"].original_draft == "this shows the real tradeoff"

        generation = storage.get_generation(response.json()['generation_id'])
        guidance = generation["strategy"]["composer_enhancement_guidance"]
        assert guidance["source"]["mode"] == "private_local_strategy"
        assert "quoted source" in " ".join(guidance["rules"]).lower()
        assert "replies earn borrowed distribution" not in " ".join(guidance["rules"]).lower()
        assert "one polished draft only" in guidance["output_contract"].lower()
    finally:
        client.__exit__(None, None, None)


def test_enhancement_never_substitutes_a_generic_offline_reply(monkeypatch):
    client, headers = _api(monkeypatch)

    def unavailable_enhancement(request, llm, *, brief=None):
        assert request.enhancement_mode is True
        return []

    monkeypatch.setattr(main, "generate_drafts", unavailable_enhancement)
    try:
        original = "I think the key issue is whether it changes a real decision."
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "enhance_composer",
            "composer": {"action": "reply", "draft_text": original},
            "context": {"author": "A", "text": "A completely different source post", "visibility": "public"},
        })
        assert response.status_code == 200
        assert response.json()["decision"]["draft"] == original
    finally:
        client.__exit__(None, None, None)


def test_enhancement_contract_rejects_legacy_image_payload(monkeypatch):
    client, headers = _api(monkeypatch)
    try:
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "enhance_composer",
            "composer": {"action": "post", "draft_text": "A real point"},
            "vision_images": ["aGVsbG8="],
        })
        assert response.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_removed_generic_create_post_mode_is_rejected(monkeypatch):
    client, headers = _api(monkeypatch)
    try:
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "create_post",
            "context": {"author": "You", "text": "A separate drafting workflow", "visibility": "public"},
        })
        assert response.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_chat_is_advice_only_and_never_returns_a_native_action(monkeypatch):
    client, headers = _api(monkeypatch)
    try:
        response = client.post("/browser/assist", headers=headers, json={"mode": "chat", "message": "Should I make this more direct?"})
        body = response.json()
        assert response.status_code == 200
        assert body["answer"]
        assert body["decision"]["action"] == "wait"
        assert "native_intent" not in body["decision"]
    finally:
        client.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# "Format it" means spacing, answered from the writer's measured shape
# --------------------------------------------------------------------------- #

def test_format_intent_survives_the_users_real_typos():
    """This command is typed with letter-order slips ('froamt', 'foramt'), which
    Damerau distance <= 1 does not cover once two letters move."""
    from app.main import _wants_formatting

    for spelling in ("format", "forat", "foramt", "froamt", "fromat", "fomat",
                     "formt", "formating", "formatting"):
        assert _wants_formatting(f"{spelling} it"), spelling
    for phrase in ("fix the spacing", "add line breaks", "space it out"):
        assert _wants_formatting(phrase), phrase


def test_format_intent_does_not_fire_on_questions_or_other_requests():
    from app.main import _wants_formatting

    for phrase in ("what is the format war", "how do i format this",
                   "write me a post about kimi", "reply to this",
                   "make it shorter", "from a to b", "for a moment"):
        assert not _wants_formatting(phrase), phrase


def test_formatting_changes_spacing_only_never_the_words():
    from app.main import _format_answer
    from app.schemas import ComposerEnhancement

    draft = ("Kimi K3 is not the reason Opus 5 slipped.\nSafety evals gate the "
             "release, not the leaderboard.\nThat is why the gap keeps widening.")
    for action in ("post", "reply", "quote"):
        composer = ComposerEnhancement(action=action, draft_text=draft, intent="enhance")
        result = _format_answer(composer, "conv-1", {"provider": "starling-services"}, 0.0, None)
        assert result is not None
        out = result["decision"]["draft"]
        assert out.split() == draft.split(), f"{action}: words or order changed"
        assert result["decision"]["native_intent"] == action
        assert len(out) <= 280


def test_formatting_says_so_honestly_when_nothing_needs_changing():
    """'See if that formatting is relevant to this post' — a one-sentence draft
    is left alone rather than fragmented, and the answer admits it."""
    from app.main import _format_answer
    from app.schemas import ComposerEnhancement

    one_liner = "Kimi K3 is not the reason Opus 5 slipped."
    composer = ComposerEnhancement(action="post", draft_text=one_liner, intent="enhance")
    result = _format_answer(composer, "conv-2", {"provider": "starling-services"}, 0.0, None)
    assert result["decision"]["draft"] == one_liner
    assert "left it alone" in result["answer"]


# --------------------------------------------------------------------------- #
# Web grounding for replies and quotes
# --------------------------------------------------------------------------- #

def test_evidence_never_overflows_the_direction_cap():
    """DraftRequest.direction is capped at 1200. Overflowing it raises a
    validation error that kills drafting outright — a worse failure than a thin
    draft. Nearly shipped exactly that on 2026-07-22."""
    from app.main import _with_evidence
    from app.schemas import DraftRequest, PostContext

    base = "Write one reply. " * 20
    for evidence in (None, "", "x" * 40, "x" * 700, "y" * 50_000):
        direction = _with_evidence(base, evidence)
        assert len(direction) <= 1200
        DraftRequest(action="reply", direction=direction,
                     context=PostContext(author="a", text="b", visibility="public"))


def test_evidence_is_included_when_there_is_room_and_dropped_when_not():
    from app.main import _with_evidence

    roomy = _with_evidence("Write one reply.", "Anthropic shipped Opus 4.8 on July 9. " * 3)
    assert "USE THESE FACTS" in roomy

    # A direction already at the cap must not gain a truncated stub of facts.
    packed = _with_evidence("z" * 1180, "Anthropic shipped Opus 4.8 on July 9.")
    assert "USE THESE FACTS" not in packed
    assert len(packed) <= 1200


def test_context_grounding_refuses_a_post_with_no_named_subject(monkeypatch):
    """Searching a vague post's filler returns dictionary pages — measured:
    "this is so true honestly" fetched definitions of "honestly". As evidence
    that is strictly worse than none, so it must not ground at all."""
    from app import radar, research

    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "search_web",
                        lambda *a, **k: pytest.fail("must not search a subjectless post"))

    from app.schemas import PostContext
    vague = PostContext(author="a", text="this is so true honestly",
                        visibility="public", post_id="v1")
    assert radar.context_grounding(vague) is None


def test_context_grounding_searches_only_a_checkable_claim_and_sends_that_sentence(monkeypatch):
    # W4 (2026-09-22): a search is now real and metered, so it runs only for a checkable,
    # time-sensitive claim, and it is sent the claim's own sentence (the search tool writes
    # its own queries from it) instead of a keyword list for every post.
    from app import radar, research

    seen = {}

    def fake_search(query, max_results=4, **kwargs):
        assert kwargs.get("claim_check") is True, "only the claim check may spend a search"
        seen["query"] = query
        return [{"title": "Kimi K3 review", "snippet": "Moonshot released Kimi K3 "
                 "with frontier scores across coding and reasoning benchmarks.",
                 "url": "https://example.com/kimi"}]

    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "search_web", fake_search)

    from app.schemas import PostContext
    opinion = PostContext(author="a", visibility="public", post_id="k0",
                          text="Honestly, Kimi K3 might be why Opus 5 gets delayed.")
    radar.context_grounding(opinion)
    assert "query" not in seen, "an opinion never costs a search"
    ctx = PostContext(author="a", visibility="public", post_id="k1",
                      text="Honestly what a week. Moonshot released Kimi K3 today and it beats Opus 5 on SWE-bench.")
    grounding = radar.context_grounding(ctx)
    assert grounding is not None
    assert seen["query"] == "Moonshot released Kimi K3 today and it beats Opus 5 on SWE-bench."
    assert "honestly" not in seen["query"].lower(), "filler must not reach the query"
    assert "Moonshot" in grounding["evidence"]
    assert grounding["sources"][0]["url"].startswith("https://")
    assert grounding["checked"]["search"] == "used"


# --------------------------------------------------------------------------- #
# A reply is about THEIR post, not about this account's favourite subjects
# --------------------------------------------------------------------------- #

_ACCOUNT_TOPICS = ("opus", "fable", "kimi", "gpt-5", "claude", "anthropic", "grok")


def _brief_for(action: str, text: str = "Lanterns trailer drops tomorrow."):
    import json
    from app.intelligence import strategy_brief
    from app.llm import _compact_brief
    from app.schemas import DraftRequest, PostContext

    context = PostContext(author="DC", author_handle="DCOfficial",
                          visibility="public", post_id="lant", text=text)
    brief = strategy_brief(DraftRequest(action=action, context=context,
                                        research_mode="off"))
    return json.dumps(_compact_brief(brief, action=action), default=str).lower()


def test_reply_prompt_carries_no_topic_steering():
    """Regression for the worst output of the project: asked to reply to
    "Lanterns trailer drops tomorrow", Starling answered "if it's not better
    than Fable, it's just another flash in the pan. Opus 5's delay is already a
    warning sign."

    The account's subjects reached the writer through `live_trends`,
    `content_pillars`, a past post sitting in `recent_chat_instructions`, and —
    the real culprit — `voice_profile.frequent_vocabulary`, which is filed under
    VOICE but on a narrow beat is a list of TOPICS. A reply's subject belongs to
    the post being replied to.
    """
    for action in ("reply", "quote"):
        blob = _brief_for(action)
        leaked = [topic for topic in _ACCOUNT_TOPICS if topic in blob]
        assert not leaked, f"{action} prompt still steers toward {leaked}"
        for field in ("live_trends", "content_pillars", "frequent_vocabulary",
                      "recent_chat_instructions"):
            assert field not in blob, f"{action} prompt still contains {field}"


_SYNTHETIC_BRIEF = {
    "objective": "followers",
    "live_trends": [{"name": "Rumors Swirl Around Claude Opus 5"}],
    "topic_memory": {"opus": 0.8},
    "recent_chat_instructions": ["Honestly, Kimi K3 might delay Opus 5."],
    "recent_angles_to_avoid_repeating": ["fable pricing"],
    "account_model": {
        "positioning": "AI news",
        "content_pillars": ["fable", "opus", "gpt-5"],
        "voice_profile": {"median_words": 30, "emoji_rate": 0.0,
                          "frequent_vocabulary": ["fable", "opus", "kimi"]},
    },
}


def test_no_entity_lists_reach_the_writer_for_any_action():
    """The account's entity checklists — frequent_vocabulary, content_pillars,
    positioning-with-tokens, and the raw live_trends feed — must never reach the
    drafting prompt, POST included. They are the "use these words" trap that made
    every post reheat Fable/GPT/Opus. A from-scratch post gets its subject from
    `direction` (radar grounding), not the brief.

    Genuine STYLE (length, emoji rate) still survives on every action."""
    import json
    from app.llm import _compact_brief

    for action in ("post", "reply", "quote"):
        blob = json.dumps(_compact_brief(_SYNTHETIC_BRIEF, action=action), default=str).lower()
        for field in ("frequent_vocabulary", "content_pillars", "live_trends",
                      "recent_chat_instructions"):
            assert field not in blob, f"{action} prompt still carries {field}"
        for entity in ("fable", "opus", "gpt-5", "kimi"):
            assert entity not in blob, f"{action} prompt leaks the entity '{entity}'"
        # SHAPE NUMBERS LEFT THE JSON BRIEF ON 2026-07-30, and the rest of the
        # voice profile followed on 2026-09-21 (V5): the style block states this
        # account's style in English, so the JSON copy only repeated it. Style
        # still reaches the writer, through the style block (tested below).
        assert "median_words" not in blob, (
            f"{action} still carries a shape number the directive already states")
        assert "voice_profile" not in blob, f"{action} ships the style twice"


def test_voice_signals_survive_for_replies():
    """Only topic steering is stripped. Genuine style must still reach the
    writer, or replies stop sounding like the account at all.

    The SHAPE numbers are the exception, since 2026-07-30: `_style_block`
    renders this surface's length and layout as English rules in the same
    prompt, and shipping `median_words` and every surface's `format_styles`
    as JSON beside them meant a reply prompt carried the POST surface's
    length as machine-readable fact. Two measured instructions fighting is
    how the median beat the sampled shape 663 drafts in a row.
    """
    blob = _brief_for("reply")
    # V5 (2026-09-21): genuine style reaches the writer ONCE, as the English style
    # block; the voice-profile JSON beside it only repeated those rates.
    # API-native pass: the habits reach the writer as a DESCRIPTION in the writer block.
    from app.voice_card import measured_habits
    style = " ".join(measured_habits([{"text": "short lowercase take here", "kind": "reply"}] * 3))
    assert "Emoji:" in style and "Hashtags:" in style, "reply prompt lost its style signals"
    assert "voice_profile" not in blob
    for shape_number in ("median_words", "median_blocks", "format_styles"):
        assert shape_number not in blob, (
            f"reply prompt still carries {shape_number}, which the style "
            "directive already states for this surface")
