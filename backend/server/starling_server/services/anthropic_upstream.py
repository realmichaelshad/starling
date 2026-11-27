"""Claude through the Anthropic Messages API, behind the proxy's OpenAI-shaped contract.

Used when `AI_PROVIDER=anthropic` (docs/AI-PROVIDER.md). Engines and the proxy keep speaking the
OpenAI Chat Completions subset of CONTRACTS 4.1. `UpstreamClient` hands each attempt here; this
module turns the request `proxy.build_upstream_body` built into one Messages API call made with the
official `anthropic` SDK, and turns the answer back into a `chat.completion` object or
`chat.completion.chunk` SSE lines, so the proxy's relay, its metering and the engines are unchanged.

Request:

* `system` messages, in order, become the top-level `system` text. User and assistant turns become
  `messages`: consecutive turns of one role are merged and blank text is dropped. The conversation
  must start and end with a user turn (Claude Haiku 5.5 refuses an assistant prefill), so a short
  user turn is added when it does not.
* `image_url` data URIs become base64 image blocks (an http(s) URL becomes a URL image block).
* `response_format` `json_schema` becomes `output_config.format` (structured outputs). The schema
  goes through `anthropic.transform_schema`, which moves constraints the API does not enforce into
  the field descriptions. `json_object` has no Messages equivalent: an instruction is added to
  `system`, and a non-streamed answer is trimmed to its JSON object.
* `reasoning_effort` becomes `output_config.effort` (`none` and `minimal` become `low`). Thinking
  stays adaptive, the model's default, and thinking tokens share `max_tokens` with the answer, so
  `max_tokens` is the engine's visible cap plus the proxy's reasoning allowance for that effort.
* `safety_identifier` becomes `metadata.user_id`. `n`, `prompt_cache_key`, `stream_options`,
  `temperature` and `top_p` are dropped (Claude Haiku 5.5 refuses sampling parameters).

Answer:

* Text blocks are joined; thinking blocks are dropped. `stop_reason` becomes `finish_reason`
  (`end_turn` and `stop_sequence` are `stop`, `max_tokens` is `length`).
* `usage` becomes OpenAI usage: `prompt_tokens` counts every input token, uncached, cache reads and
  cache writes; `prompt_tokens_details.cached_tokens` the cache reads.
* `stop_reason: "refusal"` (the model's safety classifiers declined) is a 400 `bad_request`, the
  failure the engine already treats as concerning one call only. The attempt is still metered with
  the provider's own usage, because it was billed.
* SDK exceptions map to the same failures as OpenAI's HTTP errors (`upstream.classify_error`):
  401/403 is `ai_auth` (the other key is tried), 404 `ai_model`, billing `ai_quota`, 429
  `ai_rate_limited`, 5xx and 529 overloaded `ai_unavailable`; rate limits, server errors, timeouts
  and connection errors are retried by `UpstreamClient`.

The SDK's own retries are off (`max_retries=0`) so that every attempt goes through the metering
hooks. Nothing here logs prompts, answers, error bodies or keys.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import anthropic
import httpx2

from ..config import Settings
from .errors import ServiceError
from .proxy import reasoning_allowance
from .upstream import (
    CONNECT_TIMEOUT_S,
    DEFAULT_RATE_LIMIT_RETRY_S,
    AttemptFailure,
    StreamInterrupted,
    _safe_code,
    parse_retry_after,
)

# Importing the SDK raises these loggers to DEBUG when ANTHROPIC_LOG is set, and its debug lines
# carry request options, prompts included. This module is imported after the services process set
# its logger levels, so it sets them again.
for _name in ("anthropic", "httpx2", "httpcore2"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# AI_REASONING_EFFORT (and the proxy's `reasoning_effort`) to `output_config.effort`.
EFFORTS = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
# What Claude Haiku 5.5 uses when no effort is sent.
MODEL_DEFAULT_EFFORT = "medium"
# `max_tokens` when the request names no cap (the proxy always names one).
DEFAULT_MAX_TOKENS = 4096
MAX_OUTPUT_TOKENS = 128_000
FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "pause_turn": "stop",
}
JSON_OBJECT_INSTRUCTION = (
    "Reply with one JSON object and nothing else: no text before or after it and no code fences."
)
OPENING_TURN = "(conversation start)"
CONTINUE_TURN = "Continue."
REFUSAL_MESSAGE = "the AI model declined to answer this request"
# A 5-minute cache write costs 1.25 input tokens. Starling asks for no prompt caching, so there are
# none; if there ever are, they are counted at their cost, never under it.
CACHE_WRITE_WEIGHT = 1.25
# An `error` event that opens a stream arrives with HTTP 200; its type gives the status it means.
_ERROR_TYPE_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "billing_error": 402,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "overloaded_error": 529,
}
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504, 529})
# Anthropic reports an exhausted credit balance or a reached workspace spend limit as a plain 400
# whose message says so; there is no separate error type for it. These need the owner, like
# OpenAI's quota codes, so they are not reported as the engine's bad request.
_QUOTA_PHRASES = ("credit balance", "usage limit")


# ---------------------------------------------------------------------------- request


@dataclass(frozen=True)
class MessagesCall:
    params: dict[str, Any]  # keyword arguments for `messages.create`, without `stream`
    json_object: bool = False


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return ""


def image_block(image: Any) -> dict[str, Any] | None:
    """An OpenAI `image_url` object as an Anthropic image block, or None."""
    url = image.get("url") if isinstance(image, dict) else None
    if not isinstance(url, str):
        return None
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type, _, encoding = header[len("data:"):].partition(";")
        if encoding != "base64" or not media_type.startswith("image/") or not data:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    if url.startswith(("https://", "http://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    blocks: list[dict[str, Any]] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
        elif part.get("type") == "image_url":
            block = image_block(part.get("image_url"))
            if block is not None:
                blocks.append(block)
    return blocks


def _output_format(response_format: Any) -> tuple[dict[str, Any] | None, bool]:
    """(`output_config.format`, whether the answer must be a bare JSON object without a schema)."""
    if not isinstance(response_format, dict):
        return None, False
    kind = response_format.get("type")
    if kind == "json_schema":
        definition = response_format.get("json_schema")
        schema = definition.get("schema") if isinstance(definition, dict) else None
        if not isinstance(schema, dict):
            return None, True
        try:
            schema = anthropic.transform_schema(schema)
        except (TypeError, ValueError, AttributeError):
            pass  # sent as given; the API says so if it cannot use it
        return {"type": "json_schema", "schema": schema}, False
    if kind == "json_object":
        return None, True
    return None, False


def max_tokens_for(body: dict[str, Any], effort: str | None) -> int:
    """The engine's visible cap plus the thinking allowance for the effort Claude will use.

    The proxy added `reasoning_allowance(reasoning_effort)` to the visible cap; that is taken off
    again, because `none` adds nothing while Claude Haiku 5.5 may still think at `low`.
    """
    sent = body.get("max_completion_tokens") or body.get("max_tokens")
    if isinstance(sent, bool) or not isinstance(sent, int) or sent < 1:
        visible = DEFAULT_MAX_TOKENS
    else:
        visible = max(1, sent - reasoning_allowance(body.get("reasoning_effort")))
    return min(MAX_OUTPUT_TOKENS, visible + reasoning_allowance(effort or MODEL_DEFAULT_EFFORT))


def translate_request(body: dict[str, Any]) -> MessagesCall:
    """The proxy's OpenAI Chat Completions body as Messages API arguments."""
    system: list[str] = []
    turns: list[dict[str, Any]] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in ("system", "developer"):
            text = _text_of(message.get("content"))
            if text.strip():
                system.append(text)
            continue
        if role not in ("user", "assistant"):
            continue
        blocks = _blocks(message.get("content"))
        if not blocks:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})

    output_format, json_object = _output_format(body.get("response_format"))
    if json_object:
        system.append(JSON_OBJECT_INSTRUCTION)
    if not turns:
        # Instructions only: Messages needs a user turn, so the instructions become it.
        text = "\n\n".join(system) or CONTINUE_TURN
        turns, system = [{"role": "user", "content": [{"type": "text", "text": text}]}], []
    if turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": OPENING_TURN}]})
    if turns[-1]["role"] != "user":
        turns.append({"role": "user", "content": [{"type": "text", "text": CONTINUE_TURN}]})

    sent_effort = body.get("reasoning_effort")
    effort = EFFORTS.get(str(sent_effort).strip().lower()) if sent_effort else None
    params: dict[str, Any] = {
        "model": str(body.get("model", "")),
        "max_tokens": max_tokens_for(body, effort),
        "messages": turns,
    }
    if system:
        params["system"] = "\n\n".join(system)
    output_config: dict[str, Any] = {}
    if effort is not None:
        output_config["effort"] = effort
    if output_format is not None:
        output_config["format"] = output_format
    if output_config:
        params["output_config"] = output_config
    identifier = body.get("safety_identifier")
    if isinstance(identifier, str) and identifier:
        params["metadata"] = {"user_id": identifier}
    return MessagesCall(params=params, json_object=json_object)


# ---------------------------------------------------------------------------- answer


def finish_reason(stop_reason: str | None) -> str:
    return FINISH_REASONS.get(stop_reason or "", "stop")


def openai_usage(input_tokens: Any, output_tokens: Any, cache_read: Any = 0, cache_write: Any = 0) -> dict[str, Any]:
    def count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    cached = count(cache_read)
    prompt = count(input_tokens) + cached + math.ceil(count(cache_write) * CACHE_WRITE_WEIGHT)
    completion = count(output_tokens)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def _usage_of(usage: Any) -> dict[str, Any]:
    return openai_usage(
        getattr(usage, "input_tokens", 0),
        getattr(usage, "output_tokens", 0),
        getattr(usage, "cache_read_input_tokens", 0),
        getattr(usage, "cache_creation_input_tokens", 0),
    )


def _usage_tuple(usage: dict[str, Any]) -> tuple[int, int, int]:
    return usage["prompt_tokens"], usage["prompt_tokens_details"]["cached_tokens"], usage["completion_tokens"]


def json_only(text: str) -> str:
    """The JSON object in an answer asked for `json_object`: code fences and prose around it removed."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
    try:
        json.loads(cleaned)
        return cleaned
    except ValueError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        candidate = cleaned[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            pass
    return text


def completion_payload(message: Any, *, created: int, json_object: bool = False) -> dict[str, Any]:
    """A Messages API answer as an OpenAI `chat.completion` object."""
    text = "".join(
        block.text for block in message.content or []
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    )
    if json_object:
        text = json_only(text)
    return {
        "id": message.id,
        "object": "chat.completion",
        "created": created,
        "model": message.model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text},
             "finish_reason": finish_reason(message.stop_reason)}
        ],
        "usage": _usage_of(message.usage),
    }


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, separators=(",", ":"))


class MessagesStream:
    """A Messages API event stream, read as the OpenAI SSE lines the proxy relays.

    `aiter_lines` yields what `httpx.Response.aiter_lines` would for an OpenAI stream: a
    `chat.completion.chunk` per text delta, a chunk with `finish_reason`, a usage chunk and
    `data: [DONE]`, each followed by a blank line. A refusal ends with the usage chunk and an
    `error` event instead (`failure_code` is then "refusal"). A broken stream raises
    `StreamInterrupted`.
    """

    def __init__(self, stream: Any, events: AsyncIterator[Any], first: Any, *, model: str, created: int) -> None:
        self._stream = stream
        self._events = events
        self._first = first
        self._model = model
        self._created = created
        self.failure_code: str | None = None

    def _chunk(self, message_id: str, model: str, **fields: Any) -> str:
        return _sse({"id": message_id, "object": "chat.completion.chunk", "created": self._created,
                     "model": model, **fields})

    async def _all_events(self) -> AsyncIterator[Any]:
        first, self._first = self._first, None
        if first is not None:
            yield first
        async for event in self._events:
            yield event

    async def aiter_lines(self) -> AsyncIterator[str]:
        message_id, model = "msg", self._model
        counts = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}
        stop_reason: str | None = None
        stopped = False
        try:
            async for event in self._all_events():
                kind = getattr(event, "type", None)
                if kind == "message_start":
                    message = event.message
                    message_id = message.id or message_id
                    model = message.model or model
                    _merge_counts(counts, message.usage)
                elif kind == "content_block_delta":
                    delta = event.delta
                    if getattr(delta, "type", None) == "text_delta" and delta.text:
                        yield self._chunk(message_id, model, choices=[
                            {"index": 0, "delta": {"content": delta.text}, "finish_reason": None}])
                        yield ""
                elif kind == "message_delta":
                    stop_reason = event.delta.stop_reason or stop_reason
                    _merge_counts(counts, event.usage)
                elif kind == "message_stop":
                    stopped = True
                    break
        except (anthropic.APIError, httpx2.HTTPError, ValueError) as exc:
            raise StreamInterrupted(type(exc).__name__) from None
        if not stopped:
            raise StreamInterrupted("the stream ended before message_stop")
        usage = openai_usage(counts["input_tokens"], counts["output_tokens"], counts["cache_read_input_tokens"],
                             counts["cache_creation_input_tokens"])
        if stop_reason == "refusal":
            self.failure_code = "refusal"
            yield self._chunk(message_id, model, choices=[], usage=usage)
            yield ""
            yield _sse({"error": {"code": "bad_request", "message": REFUSAL_MESSAGE}})
            yield ""
            return
        yield self._chunk(message_id, model, choices=[
            {"index": 0, "delta": {}, "finish_reason": finish_reason(stop_reason)}])
        yield ""
        yield self._chunk(message_id, model, choices=[], usage=usage)
        yield ""
        yield "data: [DONE]"
        yield ""

    async def aclose(self) -> None:
        closer = getattr(self._events, "aclose", None)
        if closer is not None:
            with contextlib.suppress(Exception):  # still running if the relay closes mid-read
                await closer()
        await self._stream.close()


def _merge_counts(counts: dict[str, int], usage: Any) -> None:
    """Stream usage is cumulative: a later event's count replaces an earlier one."""
    for name in counts:
        value = getattr(usage, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts[name] = value


# ---------------------------------------------------------------------------- errors


def classify_exception(exc: Exception) -> AttemptFailure:
    """An SDK exception as the same failure an OpenAI HTTP error would be (`upstream.classify_error`)."""
    if isinstance(exc, anthropic.APITimeoutError):
        sent = not isinstance(exc.__cause__, (httpx2.ConnectTimeout, httpx2.PoolTimeout))
        return AttemptFailure(
            ServiceError(503, "ai_unavailable", "the AI provider did not answer in time"),
            "timeout",
            retryable=True,
            possibly_billed=sent,
        )
    if isinstance(exc, anthropic.APIConnectionError):
        sent = not isinstance(exc.__cause__, httpx2.ConnectError)
        return AttemptFailure(
            ServiceError(503, "ai_unavailable", "the AI provider could not be reached"),
            "network_error",
            retryable=True,
            possibly_billed=sent,
        )
    if not isinstance(exc, anthropic.APIStatusError):
        return AttemptFailure(
            ServiceError(503, "ai_unavailable", "the AI provider returned an unreadable response"),
            "invalid_response",
            200,
            possibly_billed=True,
        )
    error_type = exc.type if isinstance(exc.type, str) else ""
    status = exc.status_code
    if status == 200:  # an `error` event as the stream opened
        status = _ERROR_TYPE_STATUS.get(error_type, 500)
    label = _safe_code(error_type) or f"http_{status}"
    retry_after = parse_retry_after(exc.response.headers)
    message = str(getattr(exc, "message", "") or "").lower()
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)) or status in (401, 403):
        return AttemptFailure(
            ServiceError(502, "ai_auth", "the AI provider rejected the API key"), label, status, auth=True
        )
    if error_type == "billing_error" or status == 402 or (
        status == 400 and any(phrase in message for phrase in _QUOTA_PHRASES)
    ):
        return AttemptFailure(
            ServiceError(502, "ai_quota", "the AI provider reports insufficient quota or a spend limit; owner action is needed"),
            "billing_error",
            status,
        )
    if isinstance(exc, anthropic.NotFoundError) or status == 404:
        return AttemptFailure(
            ServiceError(502, "ai_model", "the AI provider does not offer the configured model"), label, status
        )
    if isinstance(exc, anthropic.RateLimitError) or status == 429:
        wait = math.ceil(retry_after) if retry_after is not None else DEFAULT_RATE_LIMIT_RETRY_S
        return AttemptFailure(
            ServiceError(503, "ai_rate_limited", "the AI provider is rate limiting requests", max(1, wait)),
            label,
            status,
            retryable=True,
            retry_after_s=retry_after,
        )
    if isinstance(exc, anthropic.RequestTooLargeError) or status == 413:
        return AttemptFailure(
            ServiceError(413, "input_too_large", "the AI provider rejected the input as too large"), label, status
        )
    if status in RETRYABLE_STATUSES:
        wait = math.ceil(retry_after) if retry_after is not None else None
        return AttemptFailure(
            ServiceError(503, "ai_unavailable", "the AI provider is unavailable", wait),
            label,
            status,
            retryable=True,
            retry_after_s=retry_after,
        )
    if 400 <= status < 500:
        return AttemptFailure(ServiceError(400, "bad_request", "the AI provider rejected the request"), label, status)
    return AttemptFailure(
        ServiceError(503, "ai_unavailable", "the AI provider returned an unexpected response"), label, status
    )


# ---------------------------------------------------------------------------- client


class AnthropicUpstream:
    """One Messages API attempt per call, for `UpstreamClient` (which owns retries and keys)."""

    def __init__(self, settings: Settings, *, transport: Any = None, clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._clock = clock
        if transport is None:
            # Built here rather than by the SDK: its default transport mounts HTTP_PROXY and
            # HTTPS_PROXY from the environment even with trust_env=False, and this client carries
            # AI_API_KEY. Like every client here, a stray environment variable must not be able to
            # send the key through somebody else's proxy.
            transport = httpx2.AsyncHTTPTransport(
                trust_env=False,
                limits=httpx2.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=120.0),
            )
        self._http = anthropic.DefaultAsyncHttpxClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx2.Timeout(settings.ai_timeout_s, connect=min(CONNECT_TIMEOUT_S, settings.ai_timeout_s)),
        )
        self._clients: dict[str, anthropic.AsyncAnthropic] = {}

    def _client(self, key: str) -> anthropic.AsyncAnthropic:
        client = self._clients.get(key)
        if client is None:
            # An explicit key and base URL: the SDK then reads no ANTHROPIC_* credentials, base
            # URL or profile from the environment. No SDK retries and no OpenTelemetry spans.
            client = anthropic.AsyncAnthropic(
                api_key=key,
                base_url=self._settings.ai_base_url,
                http_client=self._http,
                max_retries=0,
                open_telemetry=False,
            )
            self._clients[key] = client
        return client

    async def aclose(self) -> None:
        await self._http.aclose()

    async def attempt(
        self, body: dict[str, Any], key: str, *, stream: bool, remaining: float
    ) -> AttemptFailure | tuple[Any, dict[str, Any] | None, bytes | None]:
        call = translate_request(body)
        client = self._client(key)
        timeout = httpx2.Timeout(min(self._settings.ai_timeout_s, remaining), connect=min(CONNECT_TIMEOUT_S, remaining))
        try:
            async with asyncio.timeout(max(0.001, remaining)):
                if not stream:
                    message = await client.messages.create(**call.params, timeout=timeout)
                else:
                    events = await client.messages.create(**call.params, stream=True, timeout=timeout)
                    iterator = aiter(events)
                    opened = False
                    try:
                        first = await anext(iterator)  # an overload or rate limit can arrive here, still retryable
                        opened = True
                    finally:
                        if not opened:
                            with contextlib.suppress(Exception):
                                await events.close()
        except TimeoutError:
            return AttemptFailure(
                ServiceError(503, "ai_unavailable", "the AI provider did not answer in time"),
                "timeout",
                retryable=True,
                possibly_billed=True,
            )
        except anthropic.APIError as exc:
            return classify_exception(exc)
        except httpx2.HTTPError:
            return AttemptFailure(
                ServiceError(503, "ai_unavailable", "the AI provider could not be reached"),
                "network_error",
                retryable=True,
                possibly_billed=True,
            )
        except (StopAsyncIteration, ValueError):
            return AttemptFailure(
                ServiceError(503, "ai_unavailable", "the AI provider returned an unreadable response"),
                "invalid_response",
                200,
                possibly_billed=True,
            )

        if stream:
            return MessagesStream(events, iterator, first, model=call.params["model"], created=int(self._clock())), None, None
        if message.stop_reason == "refusal":
            usage = _usage_of(message.usage)
            return AttemptFailure(
                ServiceError(400, "bad_request", REFUSAL_MESSAGE),
                "refusal",
                200,
                possibly_billed=True,
                usage=_usage_tuple(usage),
            )
        payload = completion_payload(message, created=int(self._clock()), json_object=call.json_object)
        return None, payload, json.dumps(payload, separators=(",", ":")).encode("utf-8")
