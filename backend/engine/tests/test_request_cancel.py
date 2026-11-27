"""Request context (CONTRACTS §3.2, §3.3): headers, threads, cancel, time zone."""
from __future__ import annotations

import contextvars
import json
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import main, posting_hours, request_context, security
from app.schemas import DraftCandidate


def _authorized() -> TestClient:
    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {security.ensure_token()}"})
    return client


def test_ids_and_zones_are_validated():
    assert request_context.clean_request_id("res_01H8Z-abc.def:1") == "res_01H8Z-abc.def:1"
    for bad in ("", "has space", "semi;colon", "x" * 129, None):
        assert request_context.clean_request_id(bad) is None
    assert request_context.clean_time_zone("America/New_York") == "America/New_York"
    assert request_context.clean_time_zone("UTC") == "UTC"
    for bad in ("Mars/Olympus_Mons", "../../etc/passwd", "", "America/New York", None):
        assert request_context.clean_time_zone(bad) is None


def test_a_cancel_reaches_every_thread_working_for_the_request():
    started = threading.Event()
    stopped = threading.Event()

    def pipeline_step() -> None:
        started.set()
        for _ in range(500):
            if request_context.cancelled():
                stopped.set()
                return
            threading.Event().wait(0.01)

    with request_context.bind("res_cancel_me") as state:
        worker = threading.Thread(target=contextvars.copy_context().run, args=(pipeline_step,))
        worker.start()
        assert started.wait(5)
        assert request_context.inflight_count() == 1
        assert request_context.cancel("res_cancel_me") is True
        worker.join(timeout=10)
        assert stopped.is_set()
        assert state.cancelled
    assert request_context.inflight_count() == 0
    assert request_context.cancel("res_cancel_me") is False


def test_the_middleware_binds_the_gateway_headers():
    def echo(_request):
        return JSONResponse({"request_id": request_context.request_id(),
                             "time_zone": request_context.time_zone()})

    app = request_context.RequestContextMiddleware(Starlette(routes=[Route("/", echo)]))
    with TestClient(app) as client:
        bound = client.get("/", headers={"X-Starling-Request-Id": "res_77",
                                         "X-Starling-Time-Zone": "Europe/Amsterdam"}).json()
        assert bound == {"request_id": "res_77", "time_zone": "Europe/Amsterdam"}
        unbound = client.get("/", headers={"X-Starling-Time-Zone": "Not/AZone"}).json()
        assert unbound == {"request_id": None, "time_zone": None}


def test_the_cancel_route_stops_a_running_request_and_needs_the_token():
    with _authorized() as client:
        assert client.post("/internal/requests/res_unknown/cancel").json() == {"ok": True, "cancelled": False}
        with request_context.bind("res_live") as state:
            response = client.post("/internal/requests/res_live/cancel")
            assert response.json() == {"ok": True, "cancelled": True}
            assert state.cancel_event.is_set()
    with TestClient(main.app) as anonymous:
        denied = anonymous.post("/internal/requests/res_live/cancel")
        assert denied.status_code == 404 and denied.content == b""


def test_a_cancel_during_a_stream_stops_the_pipeline_it_belongs_to(monkeypatch):
    """The stream's stop event IS the request's cancel event, so the gateway's
    cancel route stops the pipeline the same way a client disconnect does."""

    class FakeModel:
        text_model = "starling-quality"

        def status(self):
            return {"available": True, "text_model": "starling-quality", "provider": "starling-services"}

    observed: dict = {}

    def cancelled_mid_draft(request, llm, emit, stop_event=None, emit_status=None, *, brief=None):
        emit("Half a ")
        observed["cancel_found"] = request_context.cancel("res_stream_1")
        observed["stop_set"] = bool(stop_event is not None and stop_event.is_set())
        return []

    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    monkeypatch.setattr(main, "generate_drafts_streaming", cancelled_mid_draft)
    with _authorized() as client:
        response = client.post("/browser/assist/stream", headers={"X-Starling-Request-Id": "res_stream_1"}, json={
            "mode": "enhance_composer",
            "composer": {"action": "reply", "draft_text": "my rough draft"},
            "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
        })
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert observed == {"cancel_found": True, "stop_set": True}
    final = events[-1]
    assert final["event"] == "final"
    assert final["response"]["cancelled"] is True


def test_the_request_time_zone_drives_the_accounts_clock(monkeypatch):
    with request_context.bind(time_zone="Asia/Tokyo"):
        assert posting_hours.effective_timezone() == "Asia/Tokyo"
    # No header: the stored choice or nothing, never the server's own clock (API-native pass).
    assert posting_hours.effective_timezone() in (None, "UTC")


def test_the_engine_never_asks_its_own_machine_for_the_zone(monkeypatch):
    assert not hasattr(posting_hours, "detect_timezone"), "the engine is a server; its zone is not the customer's"
    with request_context.bind(time_zone="America/Chicago"):
        assert posting_hours.effective_timezone() == "America/Chicago"


def test_an_ai_refusal_during_a_stream_becomes_an_error_event(monkeypatch):
    class FakeModel:
        text_model = "starling-quality"

        def status(self):
            return {"available": True, "text_model": "starling-quality", "provider": "starling-services"}

    def refused(request, llm, emit, stop_event=None, emit_status=None, *, brief=None):
        request_context.record_failure(request_context.AIFailure(
            code="fair_use_exceeded", message="This account has reached its AI fair-use limit.",
            retry_after_s=None, fatal=True))
        return []

    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    monkeypatch.setattr(main, "generate_drafts_streaming", refused)
    with _authorized() as client:
        response = client.post("/browser/assist/stream", headers={"X-Starling-Request-Id": "res_refused"}, json={
            "mode": "enhance_composer",
            "composer": {"action": "reply", "draft_text": "my rough draft"},
            "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
        })
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert events[-1] == {"event": "error", "error": {
        "code": "fair_use_exceeded", "message": "This account has reached its AI fair-use limit."}}
    assert not any(event["event"] == "final" for event in events), "a refused request is not a final draft"


def test_a_usable_draft_still_finishes_normally_after_an_auxiliary_refusal(monkeypatch):
    class FakeModel:
        text_model = "starling-quality"

        def status(self):
            return {"available": True, "text_model": "starling-quality", "provider": "starling-services"}

    def drafted_anyway(request, llm, emit, stop_event=None, emit_status=None, *, brief=None):
        request_context.record_failure(request_context.AIFailure(
            code="input_too_large", message="too large", fatal=False))
        emit("Sharper version.")
        return [DraftCandidate(id="c1", text="Sharper version.", performance={}, rationale=["Tightened."],
                               retrieved_examples=[], sources=[])]

    monkeypatch.setattr(main, "ModelClient", lambda: FakeModel())
    monkeypatch.setattr(main, "generate_drafts_streaming", drafted_anyway)
    with _authorized() as client:
        response = client.post("/browser/assist/stream", json={
            "mode": "enhance_composer",
            "composer": {"action": "reply", "draft_text": "my rough draft"},
            "context": {"author": "Someone", "text": "The original post", "visibility": "public"},
        })
    final = [json.loads(line) for line in response.text.splitlines() if line.strip()][-1]
    assert final["event"] == "final"
    assert final["response"]["ok"] is True
    assert final["response"]["decision"]["draft"] == "Sharper version."


class _ChatModel:
    text_model = "starling-quality"

    def status(self):
        return {"available": True, "text_model": "starling-quality", "provider": "starling-services"}


def _chat_draft_events(monkeypatch, writer):
    monkeypatch.setattr(main, "ModelClient", lambda: _ChatModel())
    monkeypatch.setattr(main, "generate_drafts_streaming", writer)
    with _authorized() as client:
        response = client.post("/browser/assist/stream", headers={"X-Starling-Request-Id": "res_chat"}, json={
            "mode": "chat", "message": "write me a post about open weight coding models"})
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_an_ai_outage_behind_an_empty_chat_draft_is_an_error_not_a_canned_answer(monkeypatch):
    """The customer must see the outage (and must not be charged), not advice to "add an angle"."""
    def refused(request, llm, emit, stop_event=None, emit_status=None, *, brief=None):
        request_context.record_failure(request_context.AIFailure(
            code="ai_unavailable", message="Starling's AI service is not answering. Try again shortly.",
            retry_after_s=30, fatal=True))
        return []

    events = _chat_draft_events(monkeypatch, refused)
    assert events[-1]["event"] == "error"
    assert events[-1]["error"]["code"] == "ai_unavailable"
    assert not any(event["event"] == "final" for event in events)


def test_a_chat_draft_that_fails_the_gates_is_marked_withheld(monkeypatch):
    """Negative control for the test above: with no AI failure the explanation stays, flagged as not a product."""
    def gated(request, llm, emit, stop_event=None, emit_status=None, *, brief=None):
        return []

    final = _chat_draft_events(monkeypatch, gated)[-1]
    assert final["event"] == "final"
    assert final["response"]["ok"] is True and final["response"]["withheld"] is True
    assert "gates" in final["response"]["answer"]
