"""The cloud-only router: lanes, per-call timing, refusals recorded for the request."""
from __future__ import annotations

import contextvars
import threading

import pytest

from app import cloud, deadline, providers, request_context
from app.providers import ProviderRouter

TOKEN = "p" * 43


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    cloud.set_services_token(TOKEN)


def test_without_the_service_nothing_is_attempted(monkeypatch):
    monkeypatch.setattr(cloud, "chat", lambda *a, **k: pytest.fail("no call without a service"))
    monkeypatch.setattr(cloud, "chat_stream", lambda *a, **k: pytest.fail("no call without a service"))
    router = ProviderRouter()
    assert router.status()["available"] is False
    assert router.chat([{"role": "user", "content": "hi"}]) is None
    events = list(router.chat_stream([{"role": "user", "content": "hi"}]))
    assert len(events) == 1 and events[0]["done"] is True
    assert events[0]["timing"]["code"] == "services_unconfigured"
    assert router.embed(["anything"]) is None


def test_lanes_map_to_the_service_aliases(configured):
    router = ProviderRouter()
    assert router.status()["available"] is True
    assert router.model_for("quality") == router.text_model == "starling-quality"
    assert router.model_for("fast") == router.fast_model == "starling-fast"
    assert router.model_for("vision") == "starling-vision"
    assert ProviderRouter(text_model="custom-model").text_model == "custom-model"
    assert providers.get_router() is providers.get_router()


def test_a_chat_returns_the_text_and_stamps_its_timing(monkeypatch, configured):
    asked = []

    def fake_chat(model, messages, *, options=None, fmt=None):
        asked.append((model, fmt))
        return "a draft", {"total_ms": 12.0, "eval_count": 3}

    monkeypatch.setattr(cloud, "chat", fake_chat)
    router = ProviderRouter()
    assert router.chat([{"role": "user", "content": "hi"}], options={}, fmt="json", lane="fast") == "a draft"
    assert asked == [("starling-fast", "json")]
    assert router.last_timing == {"total_ms": 12.0, "eval_count": 3,
                                  "provider": providers.PROVIDER_NAME, "model": "starling-fast"}


def test_last_timing_belongs_to_the_call_not_to_the_shared_router(monkeypatch, configured):
    """One router serves every concurrent request; a shared attribute would hand
    one request the timing of another."""
    router = providers.get_router()
    barrier = threading.Barrier(2)

    def fake_chat(model, messages, *, options=None, fmt=None):
        tag = messages[0]["content"]
        barrier.wait(timeout=5)
        return tag, {"total_ms": 1.0, "tag": tag}

    monkeypatch.setattr(cloud, "chat", fake_chat)
    seen: dict[str, dict] = {}

    def work(tag: str) -> None:
        router.chat([{"role": "user", "content": tag}])
        seen[tag] = router.last_timing

    threads = [threading.Thread(target=contextvars.copy_context().run, args=(work, tag)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert seen["a"]["tag"] == "a"
    assert seen["b"]["tag"] == "b"


def test_a_fatal_refusal_is_recorded_and_stops_the_request(monkeypatch, configured):
    calls = []

    def refuse(model, messages, *, options=None, fmt=None):
        calls.append(model)
        raise cloud.ServicesError("fair_use_exceeded", status=402)

    monkeypatch.setattr(cloud, "chat", refuse)
    router = ProviderRouter()
    with request_context.bind("res_1"):
        assert router.chat([{"role": "user", "content": "hi"}]) is None
        failure = request_context.fatal_failure()
        assert failure is not None and failure.code == "fair_use_exceeded"
        assert router.last_timing["code"] == "fair_use_exceeded"
        assert deadline.expired(), "a request the service refused must start no further calls"
        assert router.chat([{"role": "user", "content": "again"}]) is None
    assert calls == ["starling-quality"]


def test_a_single_oversized_call_does_not_stop_the_request(monkeypatch, configured):
    monkeypatch.setattr(cloud, "chat", lambda *a, **k: (_ for _ in ()).throw(
        cloud.ServicesError("input_too_large", status=413)))
    router = ProviderRouter()
    with request_context.bind("res_2"):
        assert router.chat([{"role": "user", "content": "x" * 10}]) is None
        assert request_context.last_failure().code == "input_too_large"
        assert request_context.fatal_failure() is None
        assert not deadline.expired()


def test_a_stream_passes_deltas_through_and_stamps_the_final_timing(monkeypatch, configured):
    def fake_stream(model, messages, *, options=None, stop_event=None):
        yield {"delta": "one "}
        yield {"delta": "two"}
        yield {"done": True, "timing": {"total_ms": 5.0, "eval_count": 2}}

    monkeypatch.setattr(cloud, "chat_stream", fake_stream)
    events = list(ProviderRouter().chat_stream([{"role": "user", "content": "hi"}]))
    assert events[:2] == [{"delta": "one "}, {"delta": "two"}]
    assert events[-1]["timing"]["model"] == "starling-quality"
    assert events[-1]["timing"]["eval_count"] == 2


def test_a_stream_cut_short_by_the_service_says_so(monkeypatch, configured):
    def failing_stream(model, messages, *, options=None, stop_event=None):
        yield {"delta": "partial"}
        raise cloud.ServicesError("ai_unavailable", status=503)

    monkeypatch.setattr(cloud, "chat_stream", failing_stream)
    with request_context.bind("res_3"):
        events = list(ProviderRouter().chat_stream([{"role": "user", "content": "hi"}]))
        assert events[0] == {"delta": "partial"}
        final = events[-1]["timing"]
        assert final["error"] is True and final["truncated"] is True
        assert final["code"] == "ai_unavailable"
        assert request_context.fatal_failure().code == "ai_unavailable"


def test_a_cancelled_request_starts_no_call(monkeypatch, configured):
    monkeypatch.setattr(cloud, "chat", lambda *a, **k: pytest.fail("a cancelled request reached the service"))
    monkeypatch.setattr(cloud, "chat_stream", lambda *a, **k: pytest.fail("a cancelled request reached the service"))
    router = ProviderRouter()
    with request_context.bind("res_4") as state:
        state.cancel_event.set()
        assert router.chat([{"role": "user", "content": "hi"}]) is None
        assert list(router.chat_stream([{"role": "user", "content": "hi"}])) == [
            {"done": True, "timing": {"error": True, "expired": True}}]
