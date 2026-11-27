from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main, security, storage
from app.opportunities import assess_opportunity
from app.schemas import DraftRequest, PostContext, StrategyNextRequest
from app.storage import set_account_profile
from app.strategy import next_strategy


def test_weak_context_is_explicitly_skipped():
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI agents"]})
    assessment = assess_opportunity(PostContext(author="A", text="wow", visibility="public"))
    assert assessment["action"] == "wait"
    assert assessment["should_draft"] is False


def test_relevant_question_prioritizes_reply_over_quote():
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI agents", "developer tools"]})
    assessment = assess_opportunity(PostContext(author="A", text="How are AI agents changing developer tools?", visibility="public", visible_metrics={"replies": 18, "views": 2000}))
    assert assessment["action"] == "reply"
    assert assessment["should_draft"] is True
    assert assessment["downside"]


def test_x_post_has_a_complete_single_decision_response():
    """The bridge and `/strategy/next` routes are gone. The decision itself is
    `next_strategy`, which the chat path still builds its brief from."""
    set_account_profile({"audience": "AI builders", "content_pillars": ["AI agents", "developer tools"]})
    context = PostContext(author="A", text="How are AI agents changing developer tools?", visibility="public",
                          visible_metrics={"replies": 18, "views": 2000})
    body = next_strategy(StrategyNextRequest(context=context))
    assert body["next_action"]["kind"] in {"reply", "quote", "post", "follow_up", "research", "wait", "skip"}
    assert body["next_action"]["rationale"]
    assert body["active_context"]["text"] == context.text


def test_threads_are_not_a_draft_or_composer_contract():
    """The legacy draft and insert routes are gone. The contracts that remain,
    a draft request and the composer pass, still refuse a thread."""
    request = DraftRequest.model_validate({"action": "reply", "context": {"author": "A", "text": "Useful point", "visibility": "public"}})
    assert request.action == "reply"
    with pytest.raises(ValidationError):
        DraftRequest.model_validate({"action": "thread", "context": {"author": "A", "text": "Useful point", "visibility": "public"}})
    with TestClient(main.app) as client:
        headers = {"Authorization": f"Bearer {security.ensure_token()}"}
        response = client.post("/browser/assist", headers=headers, json={
            "mode": "enhance_composer", "composer": {"action": "thread", "draft_text": "No thread"}})
        assert response.status_code == 422


def test_explicit_preference_is_learned_silently(monkeypatch):
    """`/strategy/chat` is gone; the chat mode of `/browser/assist` records a
    direct first-person instruction without asking."""
    class LocalModelFixture:
        def status(self):
            return {"available": False}

        def strategy_chat(self, history, brief):
            return None

    monkeypatch.setattr(main, "ModelClient", LocalModelFixture)
    with TestClient(main.app) as client:
        token = security.ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post("/browser/assist", headers=headers,
                               json={"mode": "chat", "message": "I'd rather use direct openings."})
        assert response.status_code == 200
        assert response.json().get("preference_proposal") is None
    preferences = storage.confirmed_preferences()
    assert preferences[0]["status"] == "confirmed"
    assert preferences[0]["rule"] == "Prefer use direct openings"
