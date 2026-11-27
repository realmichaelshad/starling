"""Chat reply-intent routing: asking the chat to reply must be treated as a
REPLY (not silently turned into a standalone post). The user's bug was 'creating
a reply opens the post section.'"""
from __future__ import annotations

import pytest

from app import security

from app.main import _REPLY_INTENT, _QUOTE_INTENT, _wants_draft


@pytest.mark.parametrize("message,is_reply", [
    ("reply to this", True),
    ("respond to this", True),
    ("clap back at him", True),
    ("hit back at this take", True),
    ("fire back", True),
    ("write me a reply", True),
    ("say something back", True),
    # Must NOT read as a reply — these are standalone posts:
    ("write me a post about the response to GPT-5", False),
    ("write me a post about AI agents", False),
    ("what should I post today", False),
    ("his comeback tour is wild", False),
])
def test_reply_intent_detection(message, is_reply):
    assert bool(_REPLY_INTENT.search(message)) is is_reply


def test_quote_intent_detection():
    assert _QUOTE_INTENT.search("quote tweet this")
    assert _QUOTE_INTENT.search("qrt him")
    assert not _QUOTE_INTENT.search("write a post quoting the report")  # 'quoting' isn't the intent word


def test_wants_draft_routes_reply_intent_with_or_without_a_post():
    # A reply request is a draft request either way — with a post it drafts the
    # reply, without one it reaches the "attach the post" nudge.
    assert _wants_draft("respond to this", has_context=True) is True
    assert _wants_draft("clap back", has_context=True) is True
    assert _wants_draft("reply to this", has_context=False) is True
    assert _wants_draft("respond to this", has_context=False) is True
    # A question about the post is NOT a draft request.
    assert _wants_draft("what is this post about", has_context=True) is False


def _api(monkeypatch):
    import pathlib, tempfile
    from app import storage, main
    monkeypatch.setattr(storage, "DB_PATH", pathlib.Path(tempfile.mkdtemp()) / "reply.db")
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    client.__enter__()
    token = security.ensure_token()
    return client, {"Authorization": f"Bearer {token}"}


def test_reply_with_no_post_asks_to_attach_instead_of_making_a_post(monkeypatch):
    client, headers = _api(monkeypatch)
    r = client.post("/browser/assist", headers=headers, json={"mode": "chat", "message": "reply to this"})
    body = r.json()
    # It must NOT silently draft a standalone post — it asks for the target post.
    assert body["decision"]["action"] == "wait"
    assert "attach" in (body.get("answer") or "").lower()


def test_post_responding_to_news_is_not_hijacked_into_a_reply_nudge(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "generate_drafts", lambda req, oll, *, brief=None: [])  # model-free
    client, headers = _api(monkeypatch)
    r = client.post("/browser/assist", headers=headers,
                    json={"mode": "chat", "message": "write me a post responding to the GPT-5 news"})
    # A standalone-post request must never hit the "attach a post to reply to" nudge.
    assert "attach it with the paperclip" not in (r.json().get("answer") or "").lower()
