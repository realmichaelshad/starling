"""A scripted stand-in for the Anthropic Messages API (`POST /v1/messages`), with no network.

The anthropic SDK is built on httpx2, so the fake is an `httpx2.MockTransport` handed to the services
app as its upstream transport (AI_PROVIDER=anthropic). `serve_loopback()` runs the same script as a
real HTTP server on 127.0.0.1, for a test that goes through the SDK's own socket transport with
AI_BASE_URL pointing at it.

Queue steps with `fake.queue(...)`; each request consumes one step. The default step answers with
a thinking block and then a text block, as Claude Haiku 5.5 does: a Message object, or SSE events
when the request streams. Every request is captured so tests can assert exactly what the SDK sent.
Never a real API: the conftest blocks every host name but loopback.
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Awaitable, Callable, Iterator

import httpx2

Step = Callable[[httpx2.Request, "Captured"], Awaitable[httpx2.Response]]


@dataclass
class Captured:
    method: str
    path: str
    url: str
    headers: dict[str, str]
    body: Any

    @property
    def bearer(self) -> str:
        value = self.headers.get("authorization", "")
        return value[len("Bearer "):] if value.startswith("Bearer ") else ""

    @property
    def stream(self) -> bool:
        return isinstance(self.body, dict) and bool(self.body.get("stream"))


def _usage(input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> dict[str, Any]:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def reply(
    text: str | tuple[str, ...] = ("Fake ", "Claude ", "text."),
    *,
    input_tokens: int = 120,
    output_tokens: int = 30,
    cache_read: int = 0,
    cache_write: int = 0,
    stop_reason: str = "end_turn",
    thinking: bool = True,
    fail_after: int | None = None,
    stop_details: dict[str, Any] | None = None,
) -> Step:
    """A Message (or its SSE events when the request streams). `text` pieces are the stream's deltas;
    `fail_after=n` drops the connection after n SSE events."""
    pieces = (text,) if isinstance(text, str) else tuple(text)

    async def step(request: httpx2.Request, captured: Captured) -> httpx2.Response:
        model = (captured.body or {}).get("model")
        if not captured.stream:
            content: list[dict[str, Any]] = []
            if thinking:
                content.append({"type": "thinking", "thinking": "", "signature": "sig-fake"})
            if "".join(pieces):
                content.append({"type": "text", "text": "".join(pieces)})
            message = {"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                       "content": content, "stop_reason": stop_reason, "stop_sequence": None,
                       "usage": _usage(input_tokens, output_tokens, cache_read, cache_write)}
            if stop_details is not None:
                message["stop_details"] = stop_details
            return httpx2.Response(200, json=message)

        events = [
            _event("message_start", {"type": "message_start", "message": {
                "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": _usage(input_tokens, 1, cache_read, cache_write)}}),
            _event("ping", {"type": "ping"}),
        ]
        index = 0
        if thinking:
            events += [
                _event("content_block_start", {"type": "content_block_start", "index": index,
                                               "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
                _event("content_block_delta", {"type": "content_block_delta", "index": index,
                                               "delta": {"type": "thinking_delta", "thinking": "THINKING-NOT-RELAYED"}}),
                _event("content_block_delta", {"type": "content_block_delta", "index": index,
                                               "delta": {"type": "signature_delta", "signature": "sig-fake"}}),
                _event("content_block_stop", {"type": "content_block_stop", "index": index}),
            ]
            index += 1
        if "".join(pieces):
            events.append(_event("content_block_start", {"type": "content_block_start", "index": index,
                                                         "content_block": {"type": "text", "text": ""}}))
            events += [_event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                      "delta": {"type": "text_delta", "text": piece}})
                       for piece in pieces if piece]
            events.append(_event("content_block_stop", {"type": "content_block_stop", "index": index}))
        delta: dict[str, Any] = {"stop_reason": stop_reason, "stop_sequence": None}
        if stop_details is not None:
            delta["stop_details"] = stop_details
        events += [
            # Stream usage is cumulative; the input count comes only from message_start here.
            _event("message_delta", {"type": "message_delta", "delta": delta,
                                     "usage": {"output_tokens": output_tokens}}),
            _event("message_stop", {"type": "message_stop"}),
        ]

        async def body():
            for number, chunk in enumerate(events):
                if fail_after is not None and number >= fail_after:
                    raise httpx2.ReadError("connection lost", request=request)
                yield chunk

        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body())

    return step


def refusal(text: str = "", *, input_tokens: int = 150, output_tokens: int = 7, category: str = "cyber") -> Step:
    """HTTP 200 with `stop_reason: "refusal"`: the model's safety classifiers declined."""
    return reply(text or (), input_tokens=input_tokens, output_tokens=output_tokens, stop_reason="refusal",
                 thinking=False, stop_details={"type": "refusal", "category": category, "explanation": None})


def error(status: int, type_: str, message: str = "error from the fake provider",
          headers: dict[str, str] | None = None) -> Step:
    async def step(request: httpx2.Request, captured: Captured) -> httpx2.Response:
        body = {"type": "error", "error": {"type": type_, "message": message}, "request_id": "req_fake"}
        return httpx2.Response(status, json=body, headers=headers)

    return step


def rate_limited(retry_after: str | None = None) -> Step:
    headers = {"retry-after": retry_after} if retry_after is not None else None
    return error(429, "rate_limit_error", "Number of request tokens has exceeded your per-minute rate limit", headers)


def overloaded() -> Step:
    return error(529, "overloaded_error", "Overloaded")


def server_error(status: int = 500) -> Step:
    return error(status, "api_error", "Internal server error")


def stream_error_event(type_: str = "overloaded_error") -> Step:
    """HTTP 200 for a stream whose first event is an `error` (an overload as the stream opens)."""

    async def step(request: httpx2.Request, captured: Captured) -> httpx2.Response:
        payload = _event("error", {"type": "error", "error": {"type": type_, "message": "Overloaded"}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    return step


def timeout() -> Step:
    async def step(request: httpx2.Request, captured: Captured) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    return step


def connect_error() -> Step:
    async def step(request: httpx2.Request, captured: Captured) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    return step


class FakeAnthropic:
    def __init__(self, default: Step | None = None) -> None:
        self.default = default
        self.script: deque[Step] = deque()
        self.requests: list[Captured] = []

    def queue(self, *steps: Step) -> "FakeAnthropic":
        self.script.extend(steps)
        return self

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = None
        captured = Captured(
            method=request.method,
            path=request.url.path,
            url=str(request.url),
            headers={name.lower(): value for name, value in request.headers.items()},
            body=body,
        )
        self.requests.append(captured)
        if self.script:
            step = self.script.popleft()
        elif self.default is not None:
            step = self.default
        else:
            step = reply()
        return await step(request, captured)


@contextmanager
def serve_loopback(fake: FakeAnthropic) -> Iterator[str]:
    """Serve `fake` over real HTTP on 127.0.0.1; yields the base URL for AI_BASE_URL."""

    async def answer(request: httpx2.Request) -> tuple[int, list[tuple[str, str]], bytes]:
        response = await fake._handle(request)
        body = await response.aread()
        return response.status_code, list(response.headers.items()), body

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
            length = int(self.headers.get("content-length") or 0)
            request = httpx2.Request("POST", f"http://{self.headers.get('host')}{self.path}",
                                     headers=list(self.headers.items()), content=self.rfile.read(length))
            status, headers, body = asyncio.run(answer(request))
            self.send_response(status)
            for name, value in headers:
                if name.lower() not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
