"""A scripted stand-in for the OpenAI Chat Completions API (an `httpx.MockTransport`, no network).

Queue steps with `fake.queue(...)`; each upstream request consumes one step, and the default step
answers with a JSON completion (or, for `POST /responses`, a web search answer in the Responses
shape: a `web_search_call` item, then a `message` whose `output_text` carries `url_citation`
annotations). Every request is captured so tests can assert exactly what was sent.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

Step = Callable[[httpx.Request, "Captured"], Awaitable[httpx.Response]]


@dataclass
class Captured:
    method: str
    path: str
    headers: dict[str, str]
    body: Any

    @property
    def bearer(self) -> str:
        value = self.headers.get("authorization", "")
        return value[len("Bearer "):] if value.startswith("Bearer ") else ""


def usage(prompt: int = 120, completion: int = 30, cached: int = 0) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def ok_json(
    content: str = "Fake completion text.",
    *,
    prompt: int = 120,
    completion: int = 30,
    cached: int = 0,
    include_usage: bool = True,
) -> Step:
    async def step(request: httpx.Request, captured: Captured) -> httpx.Response:
        payload: dict[str, Any] = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1,
            "model": (captured.body or {}).get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        }
        if include_usage:
            payload["usage"] = usage(prompt, completion, cached)
        return httpx.Response(200, json=payload)

    return step


def responses_usage(inputs: int = 3_200, outputs: int = 90, cached: int = 0, reasoning: int = 0) -> dict[str, Any]:
    return {
        "input_tokens": inputs,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": outputs,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": inputs + outputs,
    }


SEARCH_SENTENCES = (
    ("Example Labs released Model X on September 14, 2026, with a 400,000-token context window.",
     "https://news.example.com/model-x", "Example Labs releases Model X"),
    ("Its API price is $1.25 per million input tokens, according to the company's pricing page.",
     "https://example.com/pricing", "Pricing - Example Labs"),
)


def web_search_ok(
    sentences: tuple[tuple[str, str, str], ...] = SEARCH_SENTENCES,
    *,
    searches: int = 1,
    inputs: int = 3_200,
    outputs: int = 90,
    cached: int = 0,
    include_usage: bool = True,
    query: str = "Example Labs Model X release",
) -> Step:
    """A Responses answer: `searches` web_search_call items, then one cited message.

    Each sentence is followed by an inline citation, `([host](url))`, whose `url_citation`
    annotation spans exactly that markup, as the provider does.
    """

    async def step(request: httpx.Request, captured: Captured) -> httpx.Response:
        text = ""
        annotations: list[dict[str, Any]] = []
        for sentence, url, title in sentences:
            host = url.split("//", 1)[-1].split("/", 1)[0]
            text += ("" if not text else " ") + sentence + " "
            marker = f"([{host}]({url}))"
            annotations.append({"type": "url_citation", "start_index": len(text),
                                "end_index": len(text) + len(marker), "url": url, "title": title})
            text += marker
        output: list[dict[str, Any]] = [
            {"type": "web_search_call", "id": f"ws_{index}", "status": "completed",
             "action": {"type": "search", "query": query,
                        "sources": [{"type": "url", "url": url} for _, url, _ in sentences]}}
            for index in range(searches)
        ]
        output.append({"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": text, "annotations": annotations}]})
        payload: dict[str, Any] = {"id": "resp_fake", "object": "response", "status": "completed",
                                   "model": (captured.body or {}).get("model"), "output": output}
        if include_usage:
            payload["usage"] = responses_usage(inputs, outputs, cached)
        return httpx.Response(200, json=payload)

    return step


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode("utf-8")


def ok_sse(
    pieces: tuple[str, ...] = ("Fake ", "stream ", "text."),
    *,
    prompt: int = 120,
    completion: int = 30,
    cached: int = 0,
    include_usage: bool = True,
    done: bool = True,
    fail_after: int | None = None,
) -> Step:
    """SSE chunks, then a usage chunk, then `[DONE]`; `fail_after=n` drops the connection after n chunks."""

    async def step(request: httpx.Request, captured: Captured) -> httpx.Response:
        model = (captured.body or {}).get("model")
        chunks = [
            _sse(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
            )
            for piece in pieces
        ]
        if include_usage:
            chunks.append(
                _sse(
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [],
                        "usage": usage(prompt, completion, cached),
                    }
                )
            )
        if done:
            chunks.append(b"data: [DONE]\n\n")

        async def body():
            for index, chunk in enumerate(chunks):
                if fail_after is not None and index >= fail_after:
                    raise httpx.ReadError("connection lost", request=request)
                yield chunk

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())

    return step


def error(
    status: int,
    code: str | None = None,
    *,
    type_: str | None = None,
    param: str | None = None,
    message: str = "error from the fake provider",
    headers: dict[str, str] | None = None,
) -> Step:
    async def step(request: httpx.Request, captured: Captured) -> httpx.Response:
        body = {"error": {"message": message, "type": type_, "param": param, "code": code}}
        return httpx.Response(status, json=body, headers=headers)

    return step


def rate_limited(retry_after: str | None = None) -> Step:
    headers = {"retry-after": retry_after} if retry_after is not None else None
    return error(429, "rate_limit_exceeded", type_="requests", message="Rate limit reached.", headers=headers)


def quota(code: str = "insufficient_quota") -> Step:
    return error(429, code, type_=code, message="You exceeded your current quota.")


def unsupported(param: str) -> Step:
    return error(
        400,
        "unsupported_parameter",
        type_="invalid_request_error",
        param=param,
        message=f"Unsupported parameter: '{param}' is not supported with this model.",
    )


def server_error(status: int = 500) -> Step:
    return error(status, None, type_="server_error", message="The server had an error while processing your request.")


def timeout() -> Step:
    async def step(request: httpx.Request, captured: Captured) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    return step


def connect_error() -> Step:
    async def step(request: httpx.Request, captured: Captured) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return step


class FakeOpenAI:
    def __init__(self, default: Step | None = None) -> None:
        self.default = default  # None: SSE for streaming requests, JSON otherwise
        self.script: deque[Step] = deque()
        self.requests: list[Captured] = []

    def queue(self, *steps: Step) -> "FakeOpenAI":
        self.script.extend(steps)
        return self

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = None
        captured = Captured(
            method=request.method,
            path=request.url.path,
            headers={name.lower(): value for name, value in request.headers.items()},
            body=body,
        )
        self.requests.append(captured)
        if self.script:
            step = self.script.popleft()
        elif self.default is not None:
            step = self.default
        elif captured.path.endswith("/responses"):
            step = web_search_ok()
        else:
            step = ok_sse() if isinstance(body, dict) and body.get("stream") else ok_json()
        return await step(request, captured)
