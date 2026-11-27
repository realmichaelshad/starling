from __future__ import annotations

from fastapi.testclient import TestClient

from app import main, storage, security
from app.schemas import BrowserAssistRequest, ComposerEnhancement, PostContext


def test_reply_composer_returns_an_honest_already_replied_result_before_generation():
    storage.record_observed_post({
        "post_id": "existing-reply", "post_type": "reply", "parent_post_id": "source-post",
        "text": "Already replied here.", "origin": "external",
    })
    request = BrowserAssistRequest(
        mode="enhance_composer",
        context=PostContext(post_id="source-post", author="Source", text="Original post", visibility="public"),
        composer=ComposerEnhancement(action="reply", draft_text="", intent="create"),
    )

    result = main._assist_composer_sync(request, object(), {"text_ready": True}, 0.0)

    assert result["ok"] is True
    assert result["decision"]["already_replied"] is True
    assert result["decision"].get("draft") is None
    assert "already replied" in result["decision"]["rationale"].lower()


def test_post_level_and_chat_reply_routes_share_the_durable_duplicate_guard(monkeypatch):
    storage.record_observed_post({
        "post_id": "existing-reply", "post_type": "reply", "parent_post_id": "source-post",
        "text": "Already replied here.", "origin": "external",
    })

    class LocalModelFixture:
        def status(self):
            return {"available": True}

    monkeypatch.setattr(main, "ModelClient", LocalModelFixture)
    with TestClient(main.app) as client:
        token = security.ensure_token()
        response = client.post("/browser/assist", headers={"Authorization": f"Bearer {token}"}, json={
            "mode": "context",
            "context": {"post_id": "source-post", "author": "Source", "text": "Original post", "visibility": "public"},
        })
    assert response.status_code == 200
    assert response.json()["decision"]["already_replied"] is True

    chat = main._chat_write_request(
        BrowserAssistRequest(mode="chat", context=PostContext(
            post_id="source-post", author="Source", text="Original post", visibility="public")),
        "reply to this", "reply-dedup-chat", object(), {}, 0.0,
    )
    assert chat["decision"]["already_replied"] is True
    assert "generation_id" not in chat
