"""The engine's only way out: the services process (CONTRACTS §4).

Every AI call and every piece of web data goes through `STARLING_SERVICES_URL`.
The engine never holds an AI provider key. It authenticates with the per-engine
services token it receives on stdin (see `app.worker`), which is kept in memory
only and never written to the environment or to disk.

Transport is httpx:

* `unix:/run/starling/services.sock` talks over a Unix domain socket (production);
* `http://127.0.0.1:<port>` is the development form.

Model names are the service's lane aliases (`starling-quality`, `starling-fast`,
`starling-vision`), overridable through `STARLING_MODEL_QUALITY` / `_FAST` /
`_VISION`. The real upstream model is chosen by the services process.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any, Iterator

import httpx

from . import deadline, request_context

LANES = ("quality", "fast", "vision")
DEFAULT_MODELS = {"quality": "starling-quality", "fast": "starling-fast",
                  "vision": "starling-vision"}
_MODEL_ENV = {"quality": "STARLING_MODEL_QUALITY", "fast": "STARLING_MODEL_FAST",
              "vision": "STARLING_MODEL_VISION"}

CONNECT_TIMEOUT_S = 5.0
# How long one drafting call may wait for the services proxy (S5, 2026-09-21).
# A draft on the API model is ~1.5 s; 20 s covers a slow provider and one proxy
# retry without letting a hung call eat the ✦ press's 50 s. It was 75 s, sized
# for a local model. The proxy's own AI_TIMEOUT_S (config, 60 s by default) is
# longer than this, so for now the engine is the side that gives up; 25 s is the
# recommended value there. Every call is still shortened to the request's budget.
CHAT_TIMEOUT_S = 20.0
# Auxiliary calls (a point, a support check, a clarity rewrite, a ranking): one
# short completion each, and the draft never waits on them for long. A caller
# opts in with `options={"timeout_s": AUX_TIMEOUT_S}`.
AUX_TIMEOUT_S = 8.0
# For a stream this bounds the silence BETWEEN chunks, not the whole answer.
STREAM_READ_TIMEOUT_S = 15.0
WORLD_TIMEOUT_S = 12.0


def _call_timeout(options: dict[str, Any] | None) -> float:
    """The per-purpose timeout a caller asked for, never longer than CHAT_TIMEOUT_S."""
    value = (options or {}).get("timeout_s")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return min(float(value), CHAT_TIMEOUT_S)
    return CHAT_TIMEOUT_S

_UNIX_PREFIX = "unix:"
_UNIX_BASE_URL = "http://starling-services"

_token: str | None = None
_state_lock = threading.Lock()
_clients: dict[str, httpx.Client] = {}


# ---- configuration --------------------------------------------------------

def set_services_token(token: str | None) -> None:
    """Install the services token received on stdin. Memory only."""
    global _token
    with _state_lock:
        _token = (token or "").strip() or None


def has_services_token() -> bool:
    return bool(_token)


def services_url() -> str:
    return os.environ.get("STARLING_SERVICES_URL", "").strip()


def is_configured() -> bool:
    """A services URL and a token: without both, nothing is attempted."""
    return bool(services_url() and _token)


def model_for(lane: str) -> str:
    lane = lane if lane in DEFAULT_MODELS else "quality"
    return os.environ.get(_MODEL_ENV[lane], "").strip() or DEFAULT_MODELS[lane]


def invalidate() -> None:
    """Drop pooled connections (the token is kept)."""
    with _state_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:                                   # noqa: BLE001
            pass


def reset() -> None:
    """Test hook: forget the token and every pooled connection."""
    invalidate()
    set_services_token(None)


def _client() -> httpx.Client:
    url = services_url()
    with _state_lock:
        client = _clients.get(url)
        if client is not None:
            return client
        # A Unix socket or loopback HTTP never carries TLS; building a certificate store for it cost
        # 0.25-0.33 s on the first press after every engine start. An https services URL (not the
        # default anywhere) keeps full verification.
        if url.startswith(_UNIX_PREFIX):
            transport = httpx.HTTPTransport(uds=url[len(_UNIX_PREFIX):], verify=False)
            base_url = _UNIX_BASE_URL
        else:
            transport = httpx.HTTPTransport(verify=not url.startswith("http://"))
            base_url = url.rstrip("/")
        # trust_env=False: proxy variables and netrc must never redirect or
        # decorate a call meant for the local services process.
        client = httpx.Client(transport=transport, base_url=base_url, trust_env=False,
                              timeout=httpx.Timeout(CHAT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
        _clients[url] = client
        return client


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if _token:
        headers["Authorization"] = f"Bearer {_token}"
    request_id = request_context.request_id()
    if request_id:
        headers["X-Starling-Request-Id"] = request_id
    else:
        headers["X-Starling-Bucket"] = "background"
    return headers


def _timeout(read_s: float) -> httpx.Timeout:
    read_s = max(1.0, float(read_s))
    return httpx.Timeout(read_s, connect=min(CONNECT_TIMEOUT_S, read_s), write=15.0, pool=5.0)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


# ---- errors ---------------------------------------------------------------

# CONTRACTS §4.1. The engine never echoes the service's own message text: these
# sentences are what the reader sees in `AssistResponse.error`.
_MESSAGES = {
    "bad_request": "The AI service could not accept that request.",
    "unauthorized": "Starling could not authenticate with its AI service.",
    "fair_use_exceeded": "This account has reached its AI fair-use limit for this billing period.",
    "reservation_inactive": "This request is no longer active, so Starling stopped working on it.",
    "input_too_large": "That was too much text for one AI step. Try a shorter message or post.",
    "call_cap_exceeded": "This request needed more AI steps than one request is allowed.",
    "ai_auth": "Starling's AI provider is not available right now. The service owner has been alerted.",
    "ai_model": "Starling's AI provider is not available right now. The service owner has been alerted.",
    "ai_quota": "Starling's AI provider is not available right now. The service owner has been alerted.",
    "ai_rate_limited": "The AI provider is busy right now. Try again in a moment.",
    "ai_unavailable": "The AI provider did not respond. Try again in a moment.",
    "budget_exhausted": "Starling's AI service is paused for now. Try again later.",
}
# A failure that only concerns one call. Everything else stops the request's
# remaining AI work, because the next call would fail the same way.
_NON_FATAL = frozenset({"bad_request", "input_too_large"})
_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 402: "fair_use_exceeded",
                 409: "reservation_inactive", 413: "input_too_large",
                 429: "call_cap_exceeded", 503: "ai_unavailable"}


class ServicesError(Exception):
    """A services call that did not produce a usable answer."""

    def __init__(self, code: str, *, status: int = 0, retry_after_s: int | None = None) -> None:
        self.code = code if code in _MESSAGES else "ai_unavailable"
        self.message = _MESSAGES[self.code]
        self.status = status
        self.retry_after_s = retry_after_s
        super().__init__(f"{self.code} ({status})" if status else self.code)

    @property
    def fatal(self) -> bool:
        return self.code not in _NON_FATAL

    def failure(self) -> request_context.AIFailure:
        return request_context.AIFailure(code=self.code, message=self.message,
                                         retry_after_s=self.retry_after_s, fatal=self.fatal)


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(math.ceil(value)) if math.isfinite(value) and value > 0 else None


def _error_from_response(response: httpx.Response) -> ServicesError:
    code = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "")
    except (ValueError, UnicodeDecodeError):
        code = ""
    if code not in _MESSAGES:
        code = _STATUS_CODES.get(response.status_code, "ai_unavailable")
    return ServicesError(code, status=response.status_code,
                         retry_after_s=_retry_after(response))


# ---- chat completions ------------------------------------------------------

def _clean_part(part: Any) -> dict[str, Any] | None:
    """One content part in exactly the shape the proxy accepts, or None."""
    if not isinstance(part, dict):
        return None
    if part.get("type") == "text":
        return {"type": "text", "text": str(part.get("text") or "")}
    if part.get("type") == "image_url":
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else None
        if not isinstance(url, str) or not url.startswith("data:image/"):
            return None                 # the proxy accepts data URIs only
        cleaned: dict[str, Any] = {"url": url}
        if image.get("detail") in ("low", "high", "auto"):
            cleaned["detail"] = image["detail"]
        return {"type": "image_url", "image_url": cleaned}
    return None


def _clean_message(message: dict[str, Any]) -> dict[str, Any]:
    """Exactly `role` and `content`, the only keys the proxy accepts on a message."""
    role = str(message.get("role") or "user")
    if role not in ("system", "user", "assistant"):
        role = "user"
    content = message.get("content")
    if isinstance(content, list):
        # Content parts (text and image_url) travel intact: flattening them to a
        # string would silently drop the image a vision call exists to read. The
        # proxy accepts images in user messages only.
        parts = [cleaned for cleaned in (_clean_part(part) for part in content)
                 if cleaned is not None and (cleaned["type"] == "text" or role == "user")]
        return {"role": role, "content": parts or ""}
    return {"role": role, "content": "" if content is None else str(content)}


def collapse_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold each run of consecutive plain-text system turns into one."""
    folded: list[dict[str, Any]] = []
    for message in messages:
        cleaned = _clean_message(message)
        previous = folded[-1] if folded else None
        if (cleaned["role"] == "system" and previous is not None
                and previous["role"] == "system"
                and isinstance(cleaned["content"], str)
                and isinstance(previous["content"], str)):
            previous["content"] = f"{previous['content']}\n\n{cleaned['content']}".strip()
            continue
        folded.append(cleaned)
    return folded


def _response_format(fmt: Any) -> dict[str, Any] | None:
    """What `fmt` asks for, in the shape CONTRACTS §4.1 accepts, or None.

    `"json"` is the loose `json_object` mode. A dict is a full `response_format`
    (`json_schema` with a strict schema is what the draft call sends, S2); only
    the two JSON types are passed on, so a malformed hint can never turn into a
    400 that costs the draft.
    """
    if fmt == "json":
        return {"type": "json_object"}
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_object":
            return {"type": "json_object"}
        schema = fmt.get("json_schema")
        if fmt.get("type") == "json_schema" and isinstance(schema, dict) and schema.get("name"):
            return {"type": "json_schema", "json_schema": dict(schema)}
    return None


def build_payload(model: str, messages: list[dict[str, Any]], options: dict[str, Any] | None,
                  *, stream: bool, fmt: str | dict[str, Any] | None = None) -> dict[str, Any]:
    options = dict(options or {})
    payload: dict[str, Any] = {"model": model,
                               "messages": collapse_system_messages(messages),
                               "stream": bool(stream)}
    max_tokens = options.get("max_tokens") or options.get("num_predict")
    if isinstance(max_tokens, (int, float)) and not isinstance(max_tokens, bool) and max_tokens > 0:
        payload["max_tokens"] = int(max_tokens)
    # Forwarded as hints only: the proxy drops sampling parameters a model does
    # not accept, so nothing may depend on them (see llm.variation_note).
    for key, highest in (("temperature", 2.0), ("top_p", 1.0)):
        value = options.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            payload[key] = min(highest, max(0.0, float(value)))
    response_format = _response_format(fmt)
    if response_format is not None:
        payload["response_format"] = response_format
    # No `stream_options`: the proxy accepts only the request fields in CONTRACTS
    # §4.1 (anything else is a 400). It sets `stream_options.include_usage` on the
    # upstream request itself and passes the final usage chunk through, which
    # `chat_stream` reads.
    return payload


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict) and part.get("type") in (None, "text"))
    return ""


def _usage_into(usage: Any, timing: dict[str, Any]) -> None:
    if not isinstance(usage, dict):
        return
    for source, target in (("prompt_tokens", "prompt_eval_count"),
                           ("completion_tokens", "eval_count")):
        value = usage.get(source)
        if isinstance(value, int):
            timing[target] = value
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        timing["cached_prompt_tokens"] = details["cached_tokens"]
    timing["usage_reported"] = True


def chat(model: str, messages: list[dict[str, Any]], *, options: dict[str, Any] | None = None,
         fmt: str | dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """One non-streaming completion. Raises ServicesError on any failure."""
    started = time.perf_counter()
    payload = build_payload(model, messages, options, stream=False, fmt=fmt)
    try:
        response = _client().post("/v1/chat/completions", json=payload, headers=_headers(),
                                  timeout=_timeout(deadline.cap(_call_timeout(options))))
    except httpx.HTTPError as error:
        raise ServicesError("ai_unavailable") from error
    if response.status_code != 200:
        raise _error_from_response(response)
    try:
        data = response.json()
    except ValueError as error:
        raise ServicesError("ai_unavailable", status=response.status_code) from error
    timing: dict[str, Any] = {"total_ms": _elapsed_ms(started)}
    content = ""
    if isinstance(data, dict):
        _usage_into(data.get("usage"), timing)
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = _content_text((choices[0].get("message") or {}).get("content"))
            # "length" means the model hit max_tokens: the caller must treat the
            # answer as possibly cut (S2), never as complete.
            finish = choices[0].get("finish_reason")
            if isinstance(finish, str) and finish:
                timing["finish_reason"] = finish
    return content.strip(), timing


def _stopped(stop_event: threading.Event | None) -> bool:
    return bool((stop_event is not None and stop_event.is_set()) or request_context.cancelled())


def chat_stream(model: str, messages: list[dict[str, Any]], *,
                options: dict[str, Any] | None = None,
                stop_event: threading.Event | None = None) -> Iterator[dict[str, Any]]:
    """Stream a completion as `{"delta"}` events, then one `{"done", "timing"}`.

    Raises ServicesError when the service refuses the call or the connection
    fails, including after some tokens were delivered; the caller decides what
    a partial answer is worth. A stop leaves the loop and closes the connection,
    which is how the service learns the answer was abandoned.
    """
    started = time.perf_counter()
    payload = build_payload(model, messages, options, stream=True)
    read_s = min(STREAM_READ_TIMEOUT_S, deadline.cap(_call_timeout(options)))
    timing: dict[str, Any] = {}
    chunks = 0
    try:
        with _client().stream("POST", "/v1/chat/completions", json=payload,
                              headers=_headers(), timeout=_timeout(read_s)) as response:
            if response.status_code != 200:
                response.read()
                raise _error_from_response(response)
            for raw_line in response.iter_lines():
                if _stopped(stop_event):
                    break
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if isinstance(data.get("error"), dict):
                    raise ServicesError(str(data["error"].get("code") or "ai_unavailable"),
                                        status=response.status_code)
                if isinstance(data.get("usage"), dict):
                    _usage_into(data["usage"], timing)
                for choice in data.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    finish = choice.get("finish_reason")
                    if isinstance(finish, str) and finish:
                        timing["finish_reason"] = finish
                    delta = _content_text((choice.get("delta") or {}).get("content"))
                    if delta:
                        chunks += 1
                        yield {"delta": delta}
    except httpx.HTTPError as error:
        raise ServicesError("ai_unavailable") from error
    timing["total_ms"] = _elapsed_ms(started)
    timing["cancelled"] = _stopped(stop_event)
    if chunks:
        timing["chunks"] = chunks
    yield {"done": True, "timing": timing}


# ---- world service ----------------------------------------------------------

def _world(method: str, path: str, *, body: dict[str, Any] | None = None,
           params: dict[str, Any] | None = None,
           timeout_s: float = WORLD_TIMEOUT_S) -> dict[str, Any] | None:
    """A world-service call. Web data is never worth failing a draft over: any
    problem returns None and the caller carries on without it."""
    if not is_configured() or deadline.expired() or request_context.cancelled():
        return None
    read_s = deadline.cap(timeout_s)
    if read_s < 1.0:
        return None
    try:
        response = _client().request(method, path, json=body, params=params,
                                     headers=_headers(), timeout=_timeout(read_s))
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def news_headlines(limit: int = 150) -> dict[str, Any] | None:
    """`GET /v1/news/headlines` (docs/architecture/news-pool-shape.md)."""
    return _world("GET", "/v1/news/headlines",
                  params={"limit": max(1, min(150, int(limit)))})


def fetch(url: str, *, timeout_s: float = WORLD_TIMEOUT_S) -> dict[str, Any] | None:
    """`POST /v1/fetch`: `{final_url, title, text, published_at}` for a public page.

    `timeout_s` lets a caller that is on a user's clock (the ✦ press reading the
    post's own link, W1) wait less than the service's own 8 s fetch cap.
    """
    return _world("POST", "/v1/fetch", body={"url": str(url)}, timeout_s=timeout_s)


def image(url: str) -> dict[str, Any] | None:
    """`POST /v1/image`: `{data_url, mime}` for a public image, or None."""
    data = _world("POST", "/v1/image", body={"url": str(url)})
    if not data or not str(data.get("data_url") or "").startswith("data:image/"):
        return None
    return data


def summary(query_id: str) -> dict[str, Any] | None:
    """`POST /v1/summary`: the cached summary for an authored query id."""
    return _world("POST", "/v1/summary", body={"query_id": str(query_id)})


# W4: how long a press waits for its one web search, from when the search starts.
SEARCH_TIMEOUT_S = 7.0


class SearchStatus(str):
    """How a search went ("ok", "capped", "refused", "late", "unavailable"), plus why.

    A plain string everywhere it is compared (`status == "capped"`), carrying what the
    service said about a refusal: `reason` (services/world/search.py REASONS: "per_press",
    "daily", "plan_share", "background"; "timeout" for a late search) and `retry_after_s`
    (when that cap opens again, or None).
    """

    reason: str | None
    retry_after_s: int | None

    def __new__(cls, status: str, reason: str | None = None, retry_after_s: int | None = None):
        value = super().__new__(cls, status)
        value.reason = reason
        value.retry_after_s = retry_after_s
        return value


def _search_retry_after(error: dict, response: Any) -> int | None:
    for value in (error.get("retry_after_s"), getattr(response, "headers", {}).get("Retry-After")):
        try:
            seconds = int(float(value))
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    return None


# How long a BACKGROUND topic search may take (nobody is waiting on it).
BACKGROUND_SEARCH_TIMEOUT_S = 25.0


def search(query: str, *, timeout_s: float | None = None, purpose: str = "claim",
           background: bool = False) -> tuple[dict[str, Any] | None, str]:
    """`POST /v1/search`: one metered web search, and how it went.

    A PRESS search (W4) checks one claim, or (API-native pass) looks up what is new in the
    account's beat when a Create post or an ideas question found nothing stored
    (`purpose="topics"`); it needs the press's request id. A BACKGROUND search
    (`background=True`, always `purpose="topics"`) is the topic research job
    (`topic_research.py`): it is sent with `X-Starling-Bucket: background` and no request id,
    and the service meters it inside the account's background share with its own daily cap.

    Returns `(answer, status)`. `status` is "ok" with the service's answer, or, with None:
    "capped" (a per-press, daily, plan-share or background cap: 429 `search_cap_exceeded`),
    "refused" (switched off, unpriced, a budget or the call cap, or a press search outside a
    press), "late" (no answer inside the timeout: the service's 504 `search_timeout`, or this
    client's own read timeout) or "unavailable" (not configured, network or provider trouble).
    `status` is a `SearchStatus`: a refusal also carries the service's `reason` and
    `retry_after_s`. Never raises, and never marks the request failed.
    """
    if not is_configured() or request_context.cancelled():
        return None, "unavailable"
    if background:
        purpose = "topics"
        read_s = float(timeout_s or BACKGROUND_SEARCH_TIMEOUT_S)
        headers = {"Accept": "application/json", "X-Starling-Bucket": "background"}
        if _token:
            headers["Authorization"] = f"Bearer {_token}"
    else:
        if deadline.expired():
            return None, "unavailable"
        if not request_context.request_id():
            return None, "refused"
        read_s = deadline.cap(float(timeout_s or SEARCH_TIMEOUT_S))
        headers = _headers()
    if read_s < 1.0:
        return None, SearchStatus("late", "timeout")
    body: dict[str, Any] = {"query": str(query), "timeout_s": round(max(1.0, min(read_s, 60.0)), 1)}
    if purpose and purpose != "claim":
        body["purpose"] = str(purpose)
    try:
        response = _client().request(
            "POST", "/v1/search", json=body,
            headers=headers, timeout=_timeout(read_s + 1.0))
    except httpx.TimeoutException:
        return None, SearchStatus("late", "timeout")
    except httpx.HTTPError:
        return None, "unavailable"
    if response.status_code != 200:
        try:
            error = (response.json() or {}).get("error") or {}
            if not isinstance(error, dict):
                error = {}
            code = str(error.get("code") or "")
        except (ValueError, AttributeError):
            error, code = {}, ""
        if code == "search_cap_exceeded":
            reason = str(error.get("reason") or "") or None
            return None, SearchStatus("capped", reason, _search_retry_after(error, response))
        if code == "search_timeout" or (code == "ai_unavailable" and "in time" in str(error.get("message") or "")):
            # The service's own deadline (the one this call sent): `search_timeout`, or, from a
            # services process older than that code, "did not answer in time".
            return None, SearchStatus("late", "timeout")
        if code == "budget_exhausted":
            return None, SearchStatus("refused", "budget", _search_retry_after(error, response))
        return None, ("unavailable" if response.status_code >= 500 and code in ("", "ai_unavailable")
                      else "refused")
    try:
        data = response.json()
    except ValueError:
        return None, "unavailable"
    return (data, "ok") if isinstance(data, dict) else (None, "unavailable")
