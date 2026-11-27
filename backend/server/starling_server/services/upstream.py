"""Client for the upstream AI provider (OpenAI Chat Completions, and Responses for web search).

`AI_PROVIDER=anthropic` sends each attempt to the Anthropic Messages API instead, through the
official SDK (anthropic_upstream.py translates the OpenAI-shaped request and answer both ways).
Everything below, retries, key order and metering hooks, is the same for both providers.

Retry policy (CONTRACTS 4.1): at most 2 retries with exponential backoff and jitter on 429 rate
limits, 500/502/503/504, timeouts and network errors, honouring `Retry-After` and never sleeping past
the request deadline. Quota and spend-limit errors are never retried. On 401/403 the other configured
key is tried once and an owner alert is raised. A 400 naming an unsupported optional parameter is
retried once without it, and the capability table remembers the refusal for the process.

Every attempt is wrapped by `AttemptHooks`: `before` runs the metering admission (and may refuse),
`failed` records a failed attempt, `abandoned` records an attempt interrupted by cancellation. A
successful attempt is returned to the caller, which records its usage.

Nothing here logs request bodies, response bodies or keys.
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Callable, Protocol

import httpx

from ..config import Settings
from .capabilities import DROPPABLE_PARAMS, CapabilityTable
from .errors import ServiceError

log = logging.getLogger(__name__)

QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "billing_hard_limit_reached",
        "billing_not_active",
    }
)
UNSUPPORTED_CODES = frozenset({"unsupported_parameter", "unsupported_value"})
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
MAX_RETRIES = 2
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0
DEFAULT_RATE_LIMIT_RETRY_S = 5
KEY_REJECTED_COOLDOWN_S = 600.0
CONNECT_TIMEOUT_S = 10.0
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024

_SAFE_CODE_RE = re.compile(r"[a-z0-9_.-]{1,64}")
_PARAM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
_QUOTED_PARAM_RE = re.compile(r"'([A-Za-z_][A-Za-z0-9_.]{0,63})'")
_UNRECOGNIZED_RE = re.compile(r"Unrecognized request argument supplied:\s*([A-Za-z_][A-Za-z0-9_]{0,63})")


@dataclass
class AttemptFailure:
    """Why one upstream attempt failed, and what the proxy answers if it is the last one."""

    error: ServiceError
    upstream_code: str
    upstream_status: int | None = None
    retryable: bool = False
    auth: bool = False
    unsupported_param: str | None = None
    retry_after_s: float | None = None
    possibly_billed: bool = False
    # The provider's own count when it answered and billed but the answer is unusable (a Claude
    # refusal): (input tokens including cached, cached input tokens, output tokens).
    usage: tuple[int, int, int] | None = None


class StreamInterrupted(Exception):
    """A relayed stream broke off after it started (raised while the proxy reads its lines)."""


@dataclass
class UpstreamSuccess:
    admission: Any
    body: dict[str, Any]
    key_slot: str
    latency_ms: int
    # stream=True: the open stream, anything with `aiter_lines()` and `aclose()`: an httpx.Response
    # for OpenAI, an anthropic_upstream.MessagesStream (OpenAI SSE lines) for Anthropic.
    response: Any = None
    payload: dict[str, Any] | None = None  # parsed JSON (stream=False)
    raw: bytes | None = None


class AttemptHooks(Protocol):
    async def before(self) -> Any: ...

    async def failed(self, admission: Any, failure: AttemptFailure) -> None: ...

    def abandoned(self, admission: Any) -> None: ...

    async def key_rejected(self, slot: str, status: int | None, fallback: str | None) -> None: ...


@dataclass
class UpstreamHealth:
    last_ok_at: int | None = None
    last_error_at: int | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    ok_count: int = 0
    error_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class _BodyTooLarge(Exception):
    pass


def parse_retry_after(headers: httpx.Headers, now: float | None = None) -> float | None:
    """Seconds to wait from `Retry-After` (seconds or HTTP date) or `retry-after-ms`."""
    value = (headers.get("retry-after") or "").strip()
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                moment = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError, IndexError):
                moment = None
            if moment is not None:
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                return max(0.0, moment.timestamp() - (time.time() if now is None else now))
        else:
            if math.isfinite(seconds):
                return max(0.0, seconds)
    millis = (headers.get("retry-after-ms") or "").strip()
    if millis:
        try:
            seconds = float(millis) / 1000.0
        except ValueError:
            return None
        if math.isfinite(seconds):
            return max(0.0, seconds)
    return None


def _error_fields(raw: bytes) -> tuple[str, str, str | None, str]:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return "", "", None, ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "", "", None, ""

    def text(name: str) -> str:
        value = error.get(name)
        return value if isinstance(value, str) else ""

    param = error.get("param")
    return text("code"), text("type"), param if isinstance(param, str) else None, text("message")


def _safe_code(value: str) -> str | None:
    value = (value or "").strip().lower()
    return value if _SAFE_CODE_RE.fullmatch(value) else None


def _unsupported_param(code: str, param: str | None, message: str) -> str | None:
    if code in UNSUPPORTED_CODES:
        if param and _PARAM_RE.fullmatch(param):
            return param
        match = _QUOTED_PARAM_RE.search(message)
        return match.group(1) if match else None
    match = _UNRECOGNIZED_RE.search(message)
    return match.group(1) if match else None


def classify_error(status: int, raw: bytes, headers: httpx.Headers) -> AttemptFailure:
    code, err_type, param, message = _error_fields(raw)
    label = _safe_code(code) or _safe_code(err_type) or f"http_{status}"
    retry_after = parse_retry_after(headers)
    if code in QUOTA_CODES or err_type in QUOTA_CODES:
        return AttemptFailure(
            ServiceError(502, "ai_quota", "the AI provider reports insufficient quota or a spend limit; owner action is needed"),
            label,
            status,
        )
    if status in (401, 403):
        return AttemptFailure(ServiceError(502, "ai_auth", "the AI provider rejected the API key"), label, status, auth=True)
    if status == 404:
        return AttemptFailure(
            ServiceError(502, "ai_model", "the AI provider does not offer the configured model"), label, status
        )
    if status == 429:
        wait = math.ceil(retry_after) if retry_after is not None else DEFAULT_RATE_LIMIT_RETRY_S
        return AttemptFailure(
            ServiceError(503, "ai_rate_limited", "the AI provider is rate limiting requests", max(1, wait)),
            label,
            status,
            retryable=True,
            retry_after_s=retry_after,
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
    if status == 400:
        name = _unsupported_param(code, param, message)
        if name is not None or code in UNSUPPORTED_CODES:
            text = (
                f"the AI provider does not accept the parameter {name} for this model"
                if name
                else "the AI provider does not accept a parameter for this model"
            )
            return AttemptFailure(ServiceError(400, "bad_request", text), label, status, unsupported_param=name)
    if status == 413:
        return AttemptFailure(
            ServiceError(413, "input_too_large", "the AI provider rejected the input as too large"), label, status
        )
    if 400 <= status < 500:
        return AttemptFailure(ServiceError(400, "bad_request", "the AI provider rejected the request"), label, status)
    return AttemptFailure(
        ServiceError(503, "ai_unavailable", "the AI provider returned an unexpected response"), label, status
    )


async def _read_limited(response: httpx.Response, limit: int, *, truncate: bool = False) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if total + len(chunk) > limit:
            if truncate:
                chunks.append(chunk[: limit - total])
                break
            raise _BodyTooLarge()
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


class UpstreamClient:
    def __init__(
        self,
        settings: Settings,
        capabilities: CapabilityTable,
        *,
        transport: Any = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
    ) -> None:
        """`transport` replaces the network in tests: an `httpx.AsyncBaseTransport` for OpenAI, an
        `httpx2.AsyncBaseTransport` (the anthropic SDK's HTTP library) for Anthropic."""
        self._settings = settings
        self._capabilities = capabilities
        self._sleep = sleep
        self._monotonic = monotonic
        self._clock = clock
        self._rand = rand
        self._primary_rejected_at: float | None = None
        self.health = UpstreamHealth()
        self._client: httpx.AsyncClient | None = None
        self._anthropic: Any = None
        if settings.ai_provider == "anthropic":
            # Imported here so the OpenAI path never loads the anthropic SDK.
            from .anthropic_upstream import AnthropicUpstream

            self._anthropic = AnthropicUpstream(settings, transport=transport, clock=clock)
            return
        timeout = httpx.Timeout(settings.ai_timeout_s, connect=min(CONNECT_TIMEOUT_S, settings.ai_timeout_s))
        self._client = httpx.AsyncClient(
            base_url=settings.ai_base_url.rstrip("/") + "/",
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            # This client carries AI_API_KEY. Like every other client here it ignores HTTP_PROXY,
            # HTTPS_PROXY, SSL_CERT_FILE and ~/.netrc: a stray environment variable must not be able
            # to send the key through somebody else's proxy.
            trust_env=False,
            # Keep the TLS connection to the provider open between presses. httpx's default drops an
            # idle connection after 5 seconds, so most presses paid a fresh TCP and TLS handshake
            # (tens of milliseconds, more on a distant region) before the first token.
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=120.0),
        )

    @property
    def provider(self) -> str:
        return self._settings.ai_provider

    async def aclose(self) -> None:
        if self._anthropic is not None:
            await self._anthropic.aclose()
        if self._client is not None:
            await self._client.aclose()

    def key_order(self) -> list[tuple[str, str]]:
        """Configured keys in the order to try them (the secondary first for a while after a rejection)."""
        keys: list[tuple[str, str]] = []
        if self._settings.ai_api_key:
            keys.append(("primary", self._settings.ai_api_key))
        if self._settings.ai_api_key_secondary:
            keys.append(("secondary", self._settings.ai_api_key_secondary))
        if (
            len(keys) == 2
            and self._primary_rejected_at is not None
            and self._monotonic() - self._primary_rejected_at < KEY_REJECTED_COOLDOWN_S
        ):
            keys.reverse()
        return keys

    def active_slot(self) -> str | None:
        keys = self.key_order()
        return keys[0][0] if keys else None

    async def complete(
        self,
        body: dict[str, Any],
        *,
        stream: bool,
        deadline: float,
        hooks: AttemptHooks,
        max_retries: int = MAX_RETRIES,
        path: str = "chat/completions",
    ) -> UpstreamSuccess:
        """One upstream call with the retry and key policy above. `path` is relative to
        AI_BASE_URL: `chat/completions` for drafts, `responses` for the web search tool (W4)."""
        keys = self.key_order()
        if not keys:
            raise ServiceError(502, "ai_auth", "no AI API key is configured")
        if self._anthropic is not None and path != "chat/completions":
            # The web search's Responses call exists only on OpenAI (config.validate refuses
            # WEB_SEARCH with AI_PROVIDER=anthropic; this is the backstop).
            raise ServiceError(503, "ai_unavailable", "the configured AI provider does not offer this endpoint")
        body = dict(body)
        model = str(body.get("model", ""))
        index = 0
        retries = 0
        switched_key = False
        dropped_param = False
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ServiceError(503, "ai_unavailable", "the AI provider did not answer in time")
            admission = await hooks.before()
            slot, key = keys[index]
            settled = False
            try:
                started = self._monotonic()
                outcome = await self._attempt(body, key, stream=stream, remaining=remaining, path=path)
                latency_ms = int(max(0.0, self._monotonic() - started) * 1000)
                if isinstance(outcome, AttemptFailure):
                    self._note_failure(outcome)
                    settled = True
                    await hooks.failed(admission, outcome)
                else:
                    self._note_success(slot)
                    settled = True
                    response, payload, raw = outcome
                    return UpstreamSuccess(
                        admission=admission,
                        body=body,
                        key_slot=slot,
                        latency_ms=latency_ms,
                        response=response if stream else None,
                        payload=payload,
                        raw=raw,
                    )
            finally:
                if not settled:
                    hooks.abandoned(admission)

            failure = outcome
            log.info(
                "ai upstream attempt failed status=%s code=%s key=%s",
                failure.upstream_status,
                failure.upstream_code,
                slot,
            )
            if failure.auth:
                if slot == "primary":
                    self._primary_rejected_at = self._monotonic()
                fallback = keys[1 - index][0] if len(keys) == 2 and not switched_key else None
                await hooks.key_rejected(slot, failure.upstream_status, fallback)
                if fallback is not None:
                    switched_key = True
                    index = 1 - index
                    continue
                raise failure.error
            param = failure.unsupported_param
            if param and not dropped_param and param in DROPPABLE_PARAMS and param in body:
                dropped_param = True
                self._capabilities.mark_unsupported(model, param)
                body.pop(param)
                log.warning("ai model %s rejected parameter %s; it is omitted from now on", model, param)
                continue
            if failure.retryable and retries < max_retries:
                delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**retries)) * (0.5 + self._rand() / 2)
                if failure.retry_after_s is not None:
                    delay = max(delay, failure.retry_after_s)
                if self._monotonic() + delay >= deadline:
                    raise failure.error
                retries += 1
                await self._sleep(delay)
                continue
            raise failure.error

    async def _attempt(
        self, body: dict[str, Any], key: str, *, stream: bool, remaining: float, path: str = "chat/completions"
    ) -> AttemptFailure | tuple[Any, dict[str, Any] | None, bytes | None]:
        if self._anthropic is not None:
            return await self._anthropic.attempt(body, key, stream=stream, remaining=remaining)
        assert self._client is not None
        timeout = httpx.Timeout(min(self._settings.ai_timeout_s, remaining), connect=min(CONNECT_TIMEOUT_S, remaining))
        request = self._client.build_request(
            "POST",
            path,
            json=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "text/event-stream" if stream else "application/json",
            },
            timeout=timeout,
        )
        started = self._monotonic()
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            sent = not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
            return AttemptFailure(
                ServiceError(503, "ai_unavailable", "the AI provider did not answer in time"),
                "timeout",
                retryable=True,
                possibly_billed=sent,
            )
        except httpx.HTTPError as exc:
            sent = not isinstance(exc, httpx.ConnectError)
            return AttemptFailure(
                ServiceError(503, "ai_unavailable", "the AI provider could not be reached"),
                "network_error",
                retryable=True,
                possibly_billed=sent,
            )

        if response.status_code != 200:
            try:
                raw = await _read_limited(response, MAX_ERROR_BODY_BYTES, truncate=True)
            except httpx.HTTPError:
                raw = b""
            finally:
                await response.aclose()
            return classify_error(response.status_code, raw, response.headers)

        if stream:
            return response, None, None

        try:
            left = max(0.001, remaining - (self._monotonic() - started))
            async with asyncio.timeout(left):
                raw = await _read_limited(response, MAX_JSON_BODY_BYTES)
        except (TimeoutError, httpx.TimeoutException):
            return AttemptFailure(
                ServiceError(503, "ai_unavailable", "the AI provider did not answer in time"),
                "timeout",
                200,
                retryable=True,
                possibly_billed=True,
            )
        except (httpx.HTTPError, _BodyTooLarge):
            return AttemptFailure(
                ServiceError(503, "ai_unavailable", "the AI provider response could not be read"),
                "invalid_response",
                200,
                possibly_billed=True,
            )
        finally:
            await response.aclose()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return AttemptFailure(
                ServiceError(503, "ai_unavailable", "the AI provider returned an unreadable response"),
                "invalid_response",
                200,
                possibly_billed=True,
            )
        return response, payload, raw

    def _note_success(self, slot: str) -> None:
        health = self.health
        health.last_ok_at = int(self._clock())
        health.ok_count += 1
        health.consecutive_failures = 0
        if slot == "primary":
            self._primary_rejected_at = None

    def _note_failure(self, failure: AttemptFailure) -> None:
        health = self.health
        health.last_error_at = int(self._clock())
        health.last_error_code = failure.upstream_code
        health.error_count += 1
        health.consecutive_failures += 1
