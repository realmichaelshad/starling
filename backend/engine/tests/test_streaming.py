"""/browser/assist/stream: token order, gated final, sentinel handling, terminal events."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import main, security
from app.llm import STREAM_SENTINEL, ModelClient
from app.main import app
from app.schemas import DraftCandidate


def _client() -> TestClient:
    client = TestClient(app)
    token = security.ensure_token()
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


class FakeModel:
    text_model = "starling-quality"

    def status(self):
        return {"available": True, "text_model": "starling-quality", "provider": "starling-services"}

    def strategy_chat_stream(self, messages, brief, emit, stop_event=None):
        for piece in ("Reply ", "to the ", "fresh post."):
            emit(piece)
        return "Reply to the fresh post."

    def strategy_chat(self, messages, brief):
        return "Reply to the fresh post."


def _read_events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_stream_chat_emits_tokens_then_final(monkeypatch):
    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    client = _client()
    response = client.post("/browser/assist/stream", json={"mode": "chat", "message": "what should I do?"})
    assert response.status_code == 200
    events = _read_events(response)
    kinds = [event["event"] for event in events]
    assert kinds[0] == "status"
    tokens = "".join(event["text"] for event in events if event["event"] == "token")
    assert tokens == "Reply to the fresh post."
    final = events[-1]
    assert final["event"] == "final"
    assert kinds.count("final") == 1
    assert final["response"]["ok"] is True
    assert final["response"]["answer"] == "Reply to the fresh post."
    # Chat stays advice-only.
    assert final["response"]["decision"]["action"] == "wait"


def test_stream_composer_final_matches_assist_shape(monkeypatch):
    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())

    def fake_stream(request, llm, emit, stop_event=None, emit_status=None, *, brief=None):
        emit("Sharper ")
        emit("version.")
        return [DraftCandidate(id="c1", text="Sharper version.", performance={}, rationale=["Tightened."],
                               retrieved_examples=[], sources=[])]

    monkeypatch.setattr(main, "generate_drafts_streaming", fake_stream)
    client = _client()
    response = client.post("/browser/assist/stream", json={
        "mode": "enhance_composer",
        "composer": {"action": "reply", "draft_text": "my rough draft"},
        "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
    })
    events = _read_events(response)
    tokens = "".join(event["text"] for event in events if event["event"] == "token")
    assert tokens == "Sharper version."
    final = events[-1]["response"]
    assert final["ok"] is True
    assert final["decision"]["draft"] == "Sharper version."
    assert final["decision"]["action"] == "reply"  # never wait on user-initiated enhance
    assert final["generation_id"]


def test_stream_composer_keeps_original_when_gates_reject(monkeypatch):
    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    monkeypatch.setattr(main, "generate_drafts_streaming",
                        lambda request, llm, emit, stop_event=None, emit_status=None, *, brief=None: [])
    client = _client()
    response = client.post("/browser/assist/stream", json={
        "mode": "enhance_composer",
        "composer": {"action": "quote", "draft_text": "keep my words"},
        "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
    })
    final = _read_events(response)[-1]["response"]
    assert final["ok"] is True
    assert final["decision"]["draft"] == "keep my words"
    assert final["decision"]["action"] == "quote"
    # His own words handed back are not a product: the gateway reads this mark and uses no
    # credit for it (CONTRACTS §3.4). Without it the full `draft` field looked like one.
    assert final["unchanged"] is True


def test_a_real_rewrite_is_not_marked_unchanged(monkeypatch):
    """Negative control for the mark above: a tightened draft is a product and says nothing."""
    from app.schemas import DraftCandidate

    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    monkeypatch.setattr(
        main, "generate_drafts_streaming",
        lambda request, llm, emit, stop_event=None, emit_status=None, *, brief=None: [
            DraftCandidate(id="c1", text="Keep the words that carry it.", performance={},
                           rationale=["Tightened."], retrieved_examples=[], sources=[])])
    client = _client()
    response = client.post("/browser/assist/stream", json={
        "mode": "enhance_composer",
        "composer": {"action": "quote", "draft_text": "keep my words"},
        "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
    })
    final = _read_events(response)[-1]["response"]
    assert final["ok"] is True and final["decision"]["draft"] == "Keep the words that carry it."
    assert "unchanged" not in final


def test_stream_rejects_unsupported_modes(monkeypatch):
    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    client = _client()
    response = client.post("/browser/assist/stream", json={
        "mode": "context",
        "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
    })
    assert response.status_code == 400


def test_a_bad_request_ends_the_stream_with_an_error_event_not_a_500(monkeypatch):
    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    client = _client()
    response = client.post("/browser/assist/stream", json={"mode": "chat", "message": "   "})
    assert response.status_code == 200
    events = _read_events(response)
    terminal = events[-1]
    assert terminal["event"] == "error"
    assert terminal["error"]["code"] == "bad_request"
    assert "message" in terminal["error"]["message"].lower()
    assert not any(event["event"] == "final" for event in events)


def test_a_created_reply_with_no_post_says_so_and_drafts_nothing(monkeypatch):
    """X's modal reply box once sent Create reply with no post. The 400 that answered it reached
    the reader, through the gateway, as "Starling's AI isn't responding". It is a final with no
    draft (nothing charged) and the reason in the rationale, and no AI call is made."""
    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())

    def never(*args, **kwargs):
        raise AssertionError("no draft may be attempted without the post")

    monkeypatch.setattr(main, "generate_drafts_streaming", never)
    client = _client()
    for action, words in (("reply", "you're replying to"), ("quote", "you're quoting")):
        response = client.post("/browser/assist/stream", json={
            "mode": "enhance_composer",
            "composer": {"action": action, "draft_text": "", "intent": "create"},
        })
        events = _read_events(response)
        terminal = events[-1]
        assert terminal["event"] == "final", terminal
        final = terminal["response"]
        assert final["ok"] is True
        assert not final["decision"].get("draft")
        assert words in final["decision"]["rationale"]
        assert "AI" not in final["decision"]["rationale"]


def test_an_engine_crash_ends_the_stream_with_an_error_event(monkeypatch):
    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())

    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "generate_drafts_streaming", crash)
    client = _client()
    response = client.post("/browser/assist/stream", json={
        "mode": "enhance_composer",
        "composer": {"action": "reply", "draft_text": "my rough draft"},
        "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
    })
    terminal = _read_events(response)[-1]
    assert terminal["event"] == "error"
    assert terminal["error"]["code"] == "engine_error"
    assert "boom" not in terminal["error"]["message"]


def test_a_refused_chat_answer_says_why(monkeypatch):
    """When the AI service refuses the request, chat must not fall back to a
    canned strategy line that hides the reason."""
    from app import request_context

    class RefusedModel(FakeModel):
        def strategy_chat_stream(self, messages, brief, emit, stop_event=None):
            request_context.record_failure(request_context.AIFailure(
                "budget_exhausted", "Starling's AI service is paused for now. Try again later.", 1800, True))
            return None

    monkeypatch.setattr(main, "ModelClient", lambda: RefusedModel())
    client = _client()
    response = client.post("/browser/assist/stream", json={"mode": "chat", "message": "what should I do?"})
    terminal = _read_events(response)[-1]
    assert terminal == {"event": "error", "error": {
        "code": "budget_exhausted", "message": "Starling's AI service is paused for now. Try again later.",
        "retry_after_s": 1800}}


def test_a_stream_that_says_nothing_is_abandoned_not_waited_out(monkeypatch):
    """The budget for a stream is justified by the reader watching tokens arrive.
    A stream that has produced nothing past the first-token budget ends with an
    honest, retryable error instead of minutes of spinner."""
    import time as _time

    from app import deadline

    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    monkeypatch.setattr(deadline, "FIRST_TOKEN_BUDGET_S", 0.5)

    def silent_and_slow(request, llm, emit, stop_event=None, emit_status=None, *, brief=None):
        # Never emits, and stops as soon as the stream gives up on it.
        for _ in range(60):
            if stop_event is not None and stop_event.is_set():
                break
            _time.sleep(0.1)
        return []

    monkeypatch.setattr(main, "generate_drafts_streaming", silent_and_slow)
    client = _client()

    terminal = None
    with client.stream("POST", "/browser/assist/stream", json={
            "mode": "enhance_composer",
            "composer": {"action": "reply", "draft_text": "", "intent": "create"},
            "context": {"author": "Someone", "text": "The original post",
                        "visibility": "public"},
    }) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("event") in ("final", "error"):
                terminal = event
                break

    assert terminal is not None, "the stream never produced a terminal event"
    assert terminal["event"] == "error"
    assert terminal["error"]["code"] == "ai_unavailable"
    message = terminal["error"]["message"].lower()
    assert "first word" in message
    assert "try again" in message
    # NO TIMING ASSERTION: `Request.is_disconnected()` blocks under TestClient
    # (it does not under uvicorn), so elapsed time here measures the harness.
    # The latency is verified against a real worker with
    # STARLING_FIRST_TOKEN_BUDGET_S set small.


def test_a_slow_stream_that_is_talking_is_never_abandoned(monkeypatch):
    """The condition is silence, not duration."""
    import time as _time

    from app import deadline

    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    monkeypatch.setattr(deadline, "FIRST_TOKEN_BUDGET_S", 0.5)

    def talks_then_thinks(request, llm, emit, stop_event=None, emit_status=None, *, brief=None):
        emit("Still ")
        _time.sleep(1.5)                 # 3x the budget, but it has spoken
        emit("here.")
        return [DraftCandidate(id="c1", text="Still here.", performance={},
                               rationale=["Tightened."], retrieved_examples=[], sources=[])]

    monkeypatch.setattr(main, "generate_drafts_streaming", talks_then_thinks)
    client = _client()
    response = client.post("/browser/assist/stream", json={
        "mode": "enhance_composer",
        "composer": {"action": "reply", "draft_text": "", "intent": "create"},
        "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
    })
    final = _read_events(response)[-1]["response"]
    assert final["ok"] is True, final.get("error")
    assert final["decision"]["draft"] == "Still here."


def test_the_press_log_never_writes_outside_the_app_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("STARLING_APP_DIR", str(tmp_path))
    assert str(tmp_path) in main._press_log_path()


def test_generate_stream_sentinel_split_never_leaks(monkeypatch):
    """The sentinel and metadata tail never appear in emitted preview tokens."""
    client = ModelClient()
    chunks = ["A sharp", " point about agents.", "\n", STREAM_SENTINEL[:4], STREAM_SENTINEL[4:],
              '{"rationale":"clear","hook":"A sharp point"}']

    def fake_chat_stream(messages, *, options, think=False, stop_event=None):
        for chunk in chunks:
            yield {"delta": chunk}
        yield {"done": True, "timing": {"total_ms": 10.0}}

    monkeypatch.setattr(client.router, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(client.router, "status", lambda: {"available": True})
    monkeypatch.setattr(client, "status", lambda: {"available": True})

    from app.schemas import DraftRequest, PostContext
    request = DraftRequest(action="reply", context=PostContext(author="A", text="post", visibility="public"),
                           research_mode="off", media_policy="no_media", grok_permission=False)
    emitted: list[str] = []
    raw = client.generate_stream(request, [], "hint", {}, emitted.append)
    preview = "".join(emitted)
    assert STREAM_SENTINEL not in preview
    assert "rationale" not in preview
    assert raw["text"] == "A sharp point about agents."
    assert raw["rationale"] == "clear"
    assert raw["hook"] == "A sharp point"


def test_the_first_token_budget_can_be_tuned_and_switched_off(monkeypatch):
    from app import deadline

    monkeypatch.delenv("STARLING_FIRST_TOKEN_BUDGET_S", raising=False)
    assert deadline.first_token_budget() == deadline.FIRST_TOKEN_BUDGET_S

    monkeypatch.setenv("STARLING_FIRST_TOKEN_BUDGET_S", "5")
    assert deadline.first_token_budget() == 5.0

    # 0 means "stop doing this", not "give up instantly".
    monkeypatch.setenv("STARLING_FIRST_TOKEN_BUDGET_S", "0")
    assert deadline.first_token_budget() == float("inf")

    # Garbage must not disable the guard silently.
    monkeypatch.setenv("STARLING_FIRST_TOKEN_BUDGET_S", "soon")
    assert deadline.first_token_budget() == deadline.FIRST_TOKEN_BUDGET_S
