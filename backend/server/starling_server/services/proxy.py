"""`POST /v1/chat/completions`: validation, upstream request shape and metered relay (CONTRACTS 4.1).

The engine sends an OpenAI Chat Completions subset naming a Starling lane. The proxy validates it,
builds the upstream request itself (model per lane, reasoning effort, clamped output tokens, hashed
safety identifier and prompt cache key, usage in streams, `n=1`, no tools, no logprobs) and meters
every attempt. Prompts and outputs are never logged or stored.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from ..config import Settings
from .auth import authenticate, metering_headers, read_body
from .capabilities import CapabilityTable
from .errors import ServiceError
from .media import DataUriError, parse_image_data_uri
from .metering import Admission, AttemptRecord, CallContext, Metering
from .upstream import AttemptFailure, StreamInterrupted, UpstreamClient, UpstreamSuccess

log = logging.getLogger(__name__)
router = APIRouter()

LANE_SETTINGS = {
    "starling-quality": "ai_model_quality",
    "starling-fast": "ai_model_fast",
    "starling-vision": "ai_model_vision",
}
LANES = tuple(LANE_SETTINGS)
ROLES = frozenset({"system", "user", "assistant"})
ACCEPTED_FIELDS = frozenset(
    {"model", "messages", "max_tokens", "max_completion_tokens", "stream", "response_format", "temperature", "top_p"}
)
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_MESSAGES = 100
MAX_PARTS = 64
IMAGE_TOKEN_ESTIMATE = 1_000  # fixed input-token estimate per image
_SCHEMA_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
SSE_INTERRUPTED = b'data: {"error": {"code": "ai_unavailable", "message": "the AI stream was interrupted"}}\n\n'


@dataclass(frozen=True)
class ChatRequest:
    lane: str
    messages: list[dict[str, Any]]
    max_output_tokens: int
    stream: bool = False
    response_format: dict[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    input_chars: int = 0
    image_count: int = 0

    @property
    def estimated_input_tokens(self) -> int:
        """ceil(chars / 4) plus a fixed estimate per image."""
        return math.ceil(self.input_chars / 4) + IMAGE_TOKEN_ESTIMATE * self.image_count


def _bad(message: str) -> ServiceError:
    return ServiceError(400, "bad_request", message)


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite numbers are not accepted")


def _optional_number(data: dict[str, Any], name: str, low: float, high: float) -> float | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise _bad(f"{name} must be a number between {low:g} and {high:g}")
    return float(value)


def _response_format(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _bad("response_format must be an object")
    kind = value.get("type")
    if kind in ("json_object", "text"):
        if set(value) != {"type"}:
            raise _bad("response_format has unexpected fields")
        return {"type": kind}
    if kind == "json_schema":
        schema = value.get("json_schema")
        if set(value) != {"type", "json_schema"} or not isinstance(schema, dict):
            raise _bad("response_format json_schema needs a json_schema object")
        if set(schema) - {"name", "schema", "strict", "description"}:
            raise _bad("response_format json_schema has unexpected fields")
        if not isinstance(schema.get("name"), str) or not _SCHEMA_NAME_RE.fullmatch(schema["name"]):
            raise _bad("response_format json_schema needs a name of letters, digits, _ or -")
        if "schema" in schema and not isinstance(schema["schema"], dict):
            raise _bad("response_format json_schema schema must be an object")
        if "strict" in schema and not isinstance(schema["strict"], bool):
            raise _bad("response_format json_schema strict must be true or false")
        if "description" in schema and not isinstance(schema["description"], str):
            raise _bad("response_format json_schema description must be a string")
        return {"type": "json_schema", "json_schema": schema}
    raise _bad("response_format type must be json_object or json_schema")


def parse_chat_request(raw: bytes, settings: Settings) -> ChatRequest:
    """Validate the engine's request body; raises ServiceError 400 or 413."""
    try:
        data = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _bad("the body must be a JSON object") from None
    if not isinstance(data, dict):
        raise _bad("the body must be a JSON object")
    unknown = sorted(set(data) - ACCEPTED_FIELDS)
    if unknown:
        name = unknown[0]
        raise _bad(f"field {name} is not accepted" if _FIELD_NAME_RE.fullmatch(name) else "a field is not accepted")

    lane = data.get("model")
    if lane not in LANE_SETTINGS:
        raise _bad("model must be starling-quality, starling-fast or starling-vision")
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise _bad("messages must be a non-empty array")
    if len(raw_messages) > MAX_MESSAGES:
        raise _bad("too many messages")

    messages: list[dict[str, Any]] = []
    chars = 0
    images = 0
    for item in raw_messages:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise _bad("each message must have exactly role and content")
        role = item["role"]
        if role not in ROLES:
            raise _bad("message role must be system, user or assistant")
        content = item["content"]
        if isinstance(content, str):
            chars += len(content)
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list) or not content or len(content) > MAX_PARTS:
            raise _bad("message content must be a string or a non-empty array of parts")
        parts: list[dict[str, Any]] = []
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "text":
                if set(part) != {"type", "text"} or not isinstance(part["text"], str):
                    raise _bad("a text part must have exactly type and text")
                chars += len(part["text"])
                parts.append({"type": "text", "text": part["text"]})
            elif kind == "image_url":
                if role != "user":
                    raise _bad("images are accepted only in user messages")
                image = part.get("image_url")
                if set(part) != {"type", "image_url"} or not isinstance(image, dict):
                    raise _bad("an image part must have exactly type and image_url")
                if set(image) - {"url", "detail"} or not isinstance(image.get("url"), str):
                    raise _bad("image_url must have a url")
                detail = image.get("detail")
                if detail is not None and detail not in ("low", "high", "auto"):
                    raise _bad("image_url detail must be low, high or auto")
                url = image["url"]
                if not url.startswith("data:"):
                    raise _bad("remote image URLs are not accepted; send a data URI")
                try:
                    parse_image_data_uri(url)
                except DataUriError as exc:
                    if exc.kind == "too_large":
                        raise ServiceError(413, "input_too_large", exc.message) from None
                    raise _bad(exc.message) from None
                images += 1
                copied: dict[str, Any] = {"url": url}
                if detail is not None:
                    copied["detail"] = detail
                parts.append({"type": "image_url", "image_url": copied})
            else:
                raise _bad("content part type must be text or image_url")
        messages.append({"role": role, "content": parts})

    requested: list[int] = []
    for name in ("max_tokens", "max_completion_tokens"):
        value = data.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _bad(f"{name} must be a positive integer")
        requested.append(value)
    max_output = min([settings.ai_max_output_tokens, *requested])

    stream = data.get("stream", False)
    if stream is None:
        stream = False
    if not isinstance(stream, bool):
        raise _bad("stream must be true or false")
    response_format = _response_format(data.get("response_format"))
    if response_format is not None and response_format["type"] == "json_schema":
        chars += len(json.dumps(response_format["json_schema"], separators=(",", ":")))

    chat = ChatRequest(
        lane=lane,
        messages=messages,
        max_output_tokens=max_output,
        stream=stream,
        response_format=response_format,
        temperature=_optional_number(data, "temperature", 0.0, 2.0),
        top_p=_optional_number(data, "top_p", 0.0, 1.0),
        input_chars=chars,
        image_count=images,
    )
    if chat.estimated_input_tokens > settings.ai_max_input_tokens_per_call:
        raise ServiceError(413, "input_too_large", "the estimated input is above the per-call limit")
    return chat


def model_for_lane(settings: Settings, lane: str) -> str:
    return str(getattr(settings, LANE_SETTINGS[lane]))


# Reasoning tokens share `max_completion_tokens` with the visible answer. The engine sizes its cap
# for the VISIBLE output only (one draft object, a sentence, a short chat answer), so with a
# reasoning effort above `none` a model could spend the whole cap thinking and return an empty or
# cut answer with `finish_reason: "length"`. This allowance is added on top of the visible cap
# whenever a reasoning effort is sent; it is a ceiling, not a spend: only tokens the model
# actually uses are billed and metered.
REASONING_ALLOWANCE = {"minimal": 512, "low": 2048, "medium": 4096, "high": 8192, "xhigh": 16384}
DEFAULT_REASONING_ALLOWANCE = 2048


def reasoning_allowance(effort: str | None) -> int:
    """Extra output tokens a reasoning effort needs beyond the visible answer (0 for none)."""
    value = str(effort or "").strip().lower()
    if not value or value == "none":
        return 0
    return REASONING_ALLOWANCE.get(value, DEFAULT_REASONING_ALLOWANCE)


def identity_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def build_upstream_body(
    chat: ChatRequest, settings: Settings, capabilities: CapabilityTable, identity: str
) -> dict[str, Any]:
    """The exact OpenAI Chat Completions request for this call (CONTRACTS 4.1).

    With AI_PROVIDER=anthropic, anthropic_upstream.translate_request turns this into the
    Messages API call."""
    model = model_for_lane(settings, chat.lane)
    body: dict[str, Any] = {
        "model": model,
        "messages": chat.messages,
        "max_completion_tokens": chat.max_output_tokens,
        "n": 1,
    }
    if settings.ai_reasoning_effort and capabilities.allows(model, "reasoning_effort"):
        body["reasoning_effort"] = settings.ai_reasoning_effort
        body["max_completion_tokens"] = chat.max_output_tokens + reasoning_allowance(settings.ai_reasoning_effort)
    if capabilities.allows(model, "safety_identifier"):
        body["safety_identifier"] = identity_hash(identity)
    if capabilities.allows(model, "prompt_cache_key"):
        body["prompt_cache_key"] = identity_hash(f"{identity}:{chat.lane}")
    if chat.response_format is not None:
        body["response_format"] = chat.response_format
    if chat.temperature is not None and capabilities.allows(model, "temperature"):
        body["temperature"] = chat.temperature
    if chat.top_p is not None and capabilities.allows(model, "top_p"):
        body["top_p"] = chat.top_p
    if chat.stream:
        body["stream"] = True
        if capabilities.allows(model, "stream_options"):
            body["stream_options"] = {"include_usage": True}
    return body


# ---------------------------------------------------------------------------- usage


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_counts(usage: Any) -> tuple[int, int, int] | None:
    """(input tokens including cached, cached input tokens, output tokens) from an OpenAI usage object."""
    if not isinstance(usage, dict):
        return None
    prompt = _count(usage.get("prompt_tokens"))
    completion = _count(usage.get("completion_tokens"))
    if prompt is None or completion is None:
        return None
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    return prompt, min(cached or 0, prompt), completion


def _message_chars(payload: dict[str, Any]) -> int:
    total = 0
    choices = payload.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            for key in ("content", "refusal"):
                if isinstance(message.get(key), str):
                    total += len(message[key])
    return total


def json_attempt_record(payload: dict[str, Any], chat: ChatRequest) -> AttemptRecord:
    counts = usage_counts(payload.get("usage"))
    if counts is not None:
        return AttemptRecord(status="ok", input_tokens=counts[0], cached_input_tokens=counts[1], output_tokens=counts[2])
    return AttemptRecord(
        status="ok",
        input_tokens=chat.estimated_input_tokens,
        output_tokens=math.ceil(_message_chars(payload) / 4),
        estimated=True,
    )


class SseTracker:
    """Reads the relayed SSE lines to find the usage chunk and count streamed characters."""

    def __init__(self) -> None:
        self.usage: tuple[int, int, int] | None = None
        self.output_chars = 0
        self.done = False
        self.upstream_error = False

    def feed(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            self.done = True
            return
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if isinstance(event.get("error"), dict):
            self.upstream_error = True
        counts = usage_counts(event.get("usage"))
        if counts is not None:
            self.usage = counts
        choices = event.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                for key in ("content", "refusal"):
                    if isinstance(delta.get(key), str):
                        self.output_chars += len(delta[key])

    def record(self, chat: ChatRequest, status: str, error_code: str | None) -> AttemptRecord:
        if self.usage is not None:
            return AttemptRecord(
                status=status,
                input_tokens=self.usage[0],
                cached_input_tokens=self.usage[1],
                output_tokens=self.usage[2],
                error_code=error_code,
            )
        return AttemptRecord(
            status=status,
            input_tokens=chat.estimated_input_tokens,
            output_tokens=math.ceil(self.output_chars / 4),
            estimated=True,
            error_code=error_code,
        )


# ---------------------------------------------------------------------------- hooks


class MeteredHooks:
    """Connects upstream attempts to metering admission and recording."""

    def __init__(self, metering: Metering, ctx: CallContext, chat: ChatRequest) -> None:
        self._metering = metering
        self._ctx = ctx
        self._chat = chat

    async def before(self) -> Admission:
        return await self._metering.admit(self._ctx)

    async def failed(self, admission: Admission, failure: AttemptFailure) -> None:
        if failure.usage is not None:
            # The provider answered and billed, but the answer is unusable (a Claude refusal).
            input_tokens, cached, output = failure.usage
            await self._metering.record(
                admission,
                AttemptRecord(
                    status="error",
                    error_code=failure.upstream_code,
                    input_tokens=input_tokens,
                    cached_input_tokens=cached,
                    output_tokens=output,
                ),
            )
            return
        billed = failure.possibly_billed
        await self._metering.record(
            admission,
            AttemptRecord(
                status="error",
                error_code=failure.upstream_code,
                input_tokens=self._chat.estimated_input_tokens if billed else 0,
                estimated=billed,
            ),
        )

    def abandoned(self, admission: Admission) -> None:
        self._metering.record_nowait(
            admission,
            AttemptRecord(
                status="cancelled",
                error_code="cancelled",
                input_tokens=self._chat.estimated_input_tokens,
                estimated=True,
            ),
        )

    async def key_rejected(self, slot: str, status: int | None, fallback: str | None) -> None:
        await self._metering.record_alert(
            "ai_key_rejected", {"key": slot, "upstream_status": status, "retried_with": fallback}
        )

    async def succeeded(self, admission: Admission, record: AttemptRecord) -> int:
        try:
            return await self._metering.record(admission, record)
        except asyncio.CancelledError:
            self._metering.record_nowait(admission, record)
            raise


class UnmeteredHooks:
    """For the operator CLI check, which runs outside the services process and its database."""

    async def before(self) -> None:
        return None

    async def failed(self, admission: Any, failure: AttemptFailure) -> None:
        return None

    def abandoned(self, admission: Any) -> None:
        return None

    async def key_rejected(self, slot: str, status: int | None, fallback: str | None) -> None:
        return None

    async def succeeded(self, admission: Any, record: AttemptRecord) -> int:
        return 0


# ---------------------------------------------------------------------------- proxy


class _StreamRelay:
    def __init__(self, metering: Metering, success: UpstreamSuccess, chat: ChatRequest, ctx: CallContext) -> None:
        self._metering = metering
        self._success = success
        self._chat = chat
        self._ctx = ctx
        self._tracker = SseTracker()
        self._closed = False

    async def lines(self) -> AsyncIterator[bytes]:
        status, code = "cancelled", "client_disconnected"
        try:
            assert self._success.response is not None
            async for line in self._success.response.aiter_lines():
                self._tracker.feed(line)
                yield (line + "\n").encode("utf-8")
            if self._tracker.done and not self._tracker.upstream_error:
                status, code = "ok", None
            else:
                # A provider adapter may name what ended the stream (anthropic_upstream: "refusal").
                status, code = "error", getattr(self._success.response, "failure_code", None) or "stream_incomplete"
        except (httpx.HTTPError, StreamInterrupted):
            status, code = "error", "stream_interrupted"
            yield SSE_INTERRUPTED
        finally:
            await self.close(status, code)

    async def close(self, status: str = "cancelled", code: str | None = "client_disconnected") -> None:
        if self._closed:
            return
        self._closed = True
        record = self._tracker.record(self._chat, status, code)
        with anyio.CancelScope(shield=True):
            try:
                if self._success.response is not None:
                    await self._success.response.aclose()
            except Exception:  # the upstream connection is already gone
                pass
            try:
                await self._metering.record(self._success.admission, record)
            except Exception as exc:
                log.error("could not record AI stream usage (%s)", type(exc).__name__)
        log.info(
            "ai stream finished bucket=%s lane=%s status=%s estimated=%s",
            self._ctx.bucket,
            self._ctx.lane,
            status,
            record.estimated,
        )


class _RelayResponse(StreamingResponse):
    """A streaming response that always settles metering, even if the body was never iterated."""

    def __init__(self, relay: _StreamRelay) -> None:
        super().__init__(
            relay.lines(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
        )
        self._relay = relay

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._relay.close()


class ChatProxy:
    def __init__(
        self,
        settings: Settings,
        metering: Metering,
        upstream: UpstreamClient,
        capabilities: CapabilityTable,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._metering = metering
        self._upstream = upstream
        self._capabilities = capabilities
        self._monotonic = monotonic

    def _require_price(self, model: str) -> None:
        if not self._metering.has_price(model):
            raise ServiceError(
                503, "ai_unavailable", "no price is configured for the AI model, so its spend cannot be metered"
            )

    async def handle(self, request: Request) -> Response:
        subject = authenticate(request, tenant=True, admin=False)
        reservation_id, bucket = metering_headers(request)
        raw = await read_body(
            request, MAX_BODY_BYTES, ServiceError(413, "input_too_large", "the request body is too large")
        )
        chat = parse_chat_request(raw, self._settings)
        ctx = CallContext(
            bucket=bucket,
            lane=chat.lane,
            model=model_for_lane(self._settings, chat.lane),
            account_id=subject.tenant_id,
            reservation_id=reservation_id,
        )
        identity = str(subject.tenant_id)
        if chat.stream:
            return await self.stream(chat, ctx, identity)
        _, raw_payload = await self.complete_json(chat, ctx, identity)
        return Response(content=raw_payload, media_type="application/json", headers={"Cache-Control": "no-store"})

    async def complete_json(self, chat: ChatRequest, ctx: CallContext, identity: str) -> tuple[dict[str, Any], bytes]:
        self._require_price(ctx.model)
        hooks = MeteredHooks(self._metering, ctx, chat)
        body = build_upstream_body(chat, self._settings, self._capabilities, identity)
        success = await self._upstream.complete(
            body, stream=False, deadline=self._monotonic() + self._settings.ai_timeout_s, hooks=hooks
        )
        assert success.payload is not None and success.raw is not None
        record = json_attempt_record(success.payload, chat)
        try:
            await hooks.succeeded(success.admission, record)
        except Exception as exc:
            log.error("could not record AI usage (%s)", type(exc).__name__)
        log.info(
            "ai call ok bucket=%s lane=%s latency_ms=%d estimated=%s",
            ctx.bucket,
            ctx.lane,
            success.latency_ms,
            record.estimated,
        )
        return success.payload, success.raw

    async def stream(self, chat: ChatRequest, ctx: CallContext, identity: str) -> StreamingResponse:
        self._require_price(ctx.model)
        hooks = MeteredHooks(self._metering, ctx, chat)
        body = build_upstream_body(chat, self._settings, self._capabilities, identity)
        success = await self._upstream.complete(
            body, stream=True, deadline=self._monotonic() + self._settings.ai_timeout_s, hooks=hooks
        )
        return _RelayResponse(_StreamRelay(self._metering, success, chat, ctx))


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await request.app.state.proxy.handle(request)
