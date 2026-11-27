"""The engine's only way out: the services client (CONTRACTS §3.1, §3.2, §4).

Payload shape, attribution headers, streaming with the usage chunk, error codes,
unix-socket transport and the world-service calls, all against a fake service.
"""
from __future__ import annotations

import contextvars
import json
import threading

import httpx
import pytest

from app import cloud, request_context

TOKEN = "s" * 43


@pytest.fixture()
def services(monkeypatch):
    seen: list[httpx.Request] = []
    replies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if replies:
            reply = replies.pop(0)
            return reply(request) if callable(reply) else reply
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    cloud.set_services_token(TOKEN)
    monkeypatch.setattr(cloud, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://services"))
    return seen, replies


def _sse(*events: object) -> bytes:
    lines = []
    for event in events:
        lines.append("data: " + (event if isinstance(event, str) else json.dumps(event)))
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_configuration_is_environment_plus_an_in_memory_token(monkeypatch):
    monkeypatch.delenv("STARLING_SERVICES_URL", raising=False)
    assert not cloud.is_configured()
    monkeypatch.setenv("STARLING_SERVICES_URL", "unix:/run/starling/services.sock")
    assert not cloud.is_configured(), "a URL without the stdin token is not usable"
    cloud.set_services_token(TOKEN)
    assert cloud.is_configured()
    assert cloud.model_for("quality") == "starling-quality"
    assert cloud.model_for("fast") == "starling-fast"
    assert cloud.model_for("vision") == "starling-vision"
    assert cloud.model_for("anything else") == "starling-quality"
    monkeypatch.setenv("STARLING_MODEL_FAST", "custom-fast")
    assert cloud.model_for("fast") == "custom-fast"
    cloud.reset()
    assert not cloud.has_services_token()


def test_chat_sends_the_contract_payload(services):
    seen, _replies = services
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo=", "detail": "low"}}
    content, timing = cloud.chat(
        "starling-quality",
        [{"role": "system", "content": "rules"}, {"role": "system", "content": "voice"},
         {"role": "user", "content": [{"type": "text", "text": "what is this?"}, image]}],
        options={"num_predict": 120, "temperature": 0.3, "top_p": 0.9}, fmt="json")
    assert content == "ok"
    assert "total_ms" in timing
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["model"] == "starling-quality"
    assert body["stream"] is False
    assert body["max_tokens"] == 120
    assert body["temperature"] == 0.3 and body["top_p"] == 0.9
    assert body["response_format"] == {"type": "json_object"}
    assert "stream_options" not in body
    assert body["messages"][0] == {"role": "system", "content": "rules\n\nvoice"}
    # Content parts stay parts: the image is not flattened away.
    assert body["messages"][1]["content"] == [{"type": "text", "text": "what is this?"}, image]


def test_every_call_is_attributed_to_its_request_or_to_the_background_bucket(services):
    seen, _replies = services
    cloud.chat("starling-fast", [{"role": "user", "content": "hi"}])
    assert seen[-1].headers["x-starling-bucket"] == "background"
    assert "x-starling-request-id" not in seen[-1].headers

    with request_context.bind("res_42"):
        cloud.chat("starling-fast", [{"role": "user", "content": "hi"}])
        assert seen[-1].headers["x-starling-request-id"] == "res_42"
        assert "x-starling-bucket" not in seen[-1].headers

        # A worker thread started with a copied context keeps the attribution...
        copied = threading.Thread(target=contextvars.copy_context().run,
                                  args=(cloud.chat, "starling-fast", [{"role": "user", "content": "x"}]))
        copied.start()
        copied.join()
        assert seen[-1].headers["x-starling-request-id"] == "res_42"

        # ...and one started without it does not, which is why every thread site copies.
        bare = threading.Thread(target=cloud.chat, args=("starling-fast", [{"role": "user", "content": "x"}]))
        bare.start()
        bare.join()
        assert seen[-1].headers["x-starling-bucket"] == "background"


def test_a_stream_yields_deltas_and_reads_the_usage_chunk(services):
    seen, replies = services
    replies.append(httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(
        {"choices": [{"delta": {"content": "Hello "}}]},
        {"choices": [{"delta": {"content": "world"}}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 4,
                                  "prompt_tokens_details": {"cached_tokens": 100}}},
        "[DONE]")))
    events = list(cloud.chat_stream("starling-quality", [{"role": "user", "content": "hi"}],
                                    options={"num_predict": 50}))
    assert [event["delta"] for event in events if "delta" in event] == ["Hello ", "world"]
    done = events[-1]
    assert done["done"] is True
    assert done["timing"]["prompt_eval_count"] == 120
    assert done["timing"]["eval_count"] == 4
    assert done["timing"]["cached_prompt_tokens"] == 100
    assert done["timing"]["cancelled"] is False
    body = json.loads(seen[0].content)
    assert body["stream"] is True
    # The proxy rejects fields outside CONTRACTS §4.1 and adds include_usage upstream itself.
    assert "stream_options" not in body
    assert set(body) <= {"model", "messages", "max_tokens", "max_completion_tokens", "stream",
                         "response_format", "temperature", "top_p"}


def test_messages_are_sent_in_exactly_the_shape_the_proxy_accepts(services):
    seen, _replies = services
    cloud.chat("starling-vision", [
        {"role": "system", "content": [{"type": "text", "text": "rules", "cache": "yes"},
                                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}],
         "name": "not accepted"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/remote.png", "detail": "low"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo=", "detail": "tiny"}},
            {"type": "audio", "audio": "x"}]},
        {"role": "tool", "content": None},
        {"role": "user", "content": [{"type": "audio"}]},
    ], options={"temperature": 7, "top_p": -1, "num_predict": 50})
    body = json.loads(seen[0].content)
    assert body["messages"] == [
        {"role": "system", "content": [{"type": "text", "text": "rules"}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]},
        {"role": "user", "content": ""},
        {"role": "user", "content": ""},
    ]
    assert all(set(message) == {"role", "content"} for message in body["messages"])
    assert body["temperature"] == 2.0 and body["top_p"] == 0.0


def test_a_stop_ends_the_stream_early(services):
    _seen, replies = services
    replies.append(httpx.Response(200, content=_sse(
        {"choices": [{"delta": {"content": "one "}}]},
        {"choices": [{"delta": {"content": "two "}}]},
        {"choices": [{"delta": {"content": "three"}}]}, "[DONE]")))
    stop = threading.Event()
    received = []
    for event in cloud.chat_stream("starling-quality", [{"role": "user", "content": "hi"}], stop_event=stop):
        if "delta" in event:
            received.append(event["delta"])
            stop.set()
        else:
            assert event["timing"]["cancelled"] is True
    assert received == ["one "]


def test_an_error_inside_the_stream_is_raised_after_the_delivered_tokens(services):
    _seen, replies = services
    replies.append(httpx.Response(200, content=_sse(
        {"choices": [{"delta": {"content": "partial"}}]},
        {"error": {"code": "ai_unavailable", "message": "upstream went away"}})))
    received = []
    with pytest.raises(cloud.ServicesError) as caught:
        for event in cloud.chat_stream("starling-quality", [{"role": "user", "content": "hi"}]):
            received.append(event)
    assert received == [{"delta": "partial"}]
    assert caught.value.code == "ai_unavailable"


@pytest.mark.parametrize("status, code, retry_after, expected, fatal", [
    (400, "bad_request", None, "bad_request", False),
    (401, "unauthorized", None, "unauthorized", True),
    (402, "fair_use_exceeded", None, "fair_use_exceeded", True),
    (409, "reservation_inactive", None, "reservation_inactive", True),
    (413, "input_too_large", None, "input_too_large", False),
    (429, "call_cap_exceeded", None, "call_cap_exceeded", True),
    (502, "ai_auth", None, "ai_auth", True),
    (502, "ai_model", None, "ai_model", True),
    (502, "ai_quota", None, "ai_quota", True),
    (503, "ai_rate_limited", "7", "ai_rate_limited", True),
    (503, "ai_unavailable", None, "ai_unavailable", True),
    (503, "budget_exhausted", "3600", "budget_exhausted", True),
    (409, "", None, "reservation_inactive", True),
    (500, "something_new", None, "ai_unavailable", True),
])
def test_service_errors_become_engine_errors(services, status, code, retry_after, expected, fatal):
    _seen, replies = services
    headers = {"Retry-After": retry_after} if retry_after else {}
    body = {"error": {"code": code, "message": "service text that must not be echoed"}} if code else {}
    replies.append(httpx.Response(status, headers=headers, json=body))
    with pytest.raises(cloud.ServicesError) as caught:
        cloud.chat("starling-quality", [{"role": "user", "content": "hi"}])
    error = caught.value
    assert error.code == expected
    assert error.fatal is fatal
    assert error.retry_after_s == (int(retry_after) if retry_after else None)
    assert error.message and "service text" not in error.message
    failure = error.failure()
    assert (failure.code, failure.fatal) == (expected, fatal)


def test_a_transport_failure_is_ai_unavailable(monkeypatch):
    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    cloud.set_services_token(TOKEN)

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(cloud, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(refuse), base_url="http://services"))
    with pytest.raises(cloud.ServicesError) as caught:
        cloud.chat("starling-quality", [{"role": "user", "content": "hi"}])
    assert caught.value.code == "ai_unavailable"


def test_a_unix_url_talks_over_a_unix_socket(monkeypatch):
    made: list[dict] = []
    real_transport = httpx.HTTPTransport

    class Recorder(real_transport):
        def __init__(self, *args, **kwargs):
            made.append(dict(kwargs))
            kwargs.pop("uds", None)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cloud.httpx, "HTTPTransport", Recorder)
    cloud.invalidate()
    monkeypatch.setenv("STARLING_SERVICES_URL", "unix:/run/starling/services.sock")
    client = cloud._client()
    assert made[-1].get("uds") == "/run/starling/services.sock"
    assert str(client.base_url).startswith("http://starling-services")
    assert client._trust_env is False

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:8123")
    client = cloud._client()
    assert "uds" not in made[-1]
    assert str(client.base_url).rstrip("/") == "http://127.0.0.1:8123"
    cloud.invalidate()


def test_world_calls_use_the_contract_routes(services):
    seen, replies = services
    replies.extend([
        httpx.Response(200, json={"generated_at": 1, "items": []}),
        httpx.Response(200, json={"final_url": "https://example.com/a", "title": "A", "text": "t", "published_at": 0}),
        httpx.Response(200, json={"data_url": "data:image/png;base64,AAAA", "mime": "image/png"}),
        httpx.Response(200, json={"data_url": "https://not-a-data-url", "mime": "image/png"}),
        httpx.Response(200, json={"answer": "summary", "sources": []}),
        httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}}),
    ])
    assert cloud.news_headlines(500) == {"generated_at": 1, "items": []}
    assert cloud.fetch("https://example.com/a")["title"] == "A"
    assert cloud.image("https://example.com/a.png")["mime"] == "image/png"
    assert cloud.image("https://example.com/b.png") is None
    assert cloud.summary("coding-agents") == {"answer": "summary", "sources": []}
    assert cloud.fetch("https://example.com/missing") is None
    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", "/v1/news/headlines"), ("POST", "/v1/fetch"), ("POST", "/v1/image"),
        ("POST", "/v1/image"), ("POST", "/v1/summary"), ("POST", "/v1/fetch")]
    assert dict(seen[0].url.params) == {"limit": "150"}
    assert json.loads(seen[1].content) == {"url": "https://example.com/a"}
    assert json.loads(seen[4].content) == {"query_id": "coding-agents"}


def test_nothing_is_requested_without_the_service(monkeypatch):
    monkeypatch.delenv("STARLING_SERVICES_URL", raising=False)
    monkeypatch.setattr(cloud, "_client", lambda: pytest.fail("no request may be made"))
    assert cloud.news_headlines() is None
    assert cloud.fetch("https://example.com/a") is None
    assert cloud.summary("coding-agents") is None
