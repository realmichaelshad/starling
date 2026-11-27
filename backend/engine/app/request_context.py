"""Per-request state that has to follow the work into worker threads.

The gateway forwards two headers on every request (CONTRACTS §3.2):
`X-Starling-Request-Id` (the usage reservation id) and `X-Starling-Time-Zone`
(the browser's IANA zone). Every services call made while serving a request
must carry that id, including calls made from threads, and the gateway can stop
a request through `POST /internal/requests/{request_id}/cancel`.

The state is ONE mutable object held in a ContextVar. `asyncio.to_thread`
copies the context; plain threads and executors must be started with
`contextvars.copy_context().run`. A copied context still points at the same
`RequestState`, so a cancel or an AI failure recorded on one thread is visible
on every other thread working for the same request.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
import threading
from dataclasses import dataclass, field
from typing import Iterator

# The reservation id is minted by the gateway; anything outside this alphabet is
# not one of ours and is ignored rather than echoed into outbound headers.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TIME_ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,3}$")


@dataclass
class AIFailure:
    """One failed services call, in words a person can act on."""

    code: str
    message: str
    retry_after_s: int | None = None
    # A fatal failure means further AI calls for this request cannot succeed
    # (limits, auth, an exhausted budget), so the pipeline stops asking.
    fatal: bool = True

    def as_error(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.retry_after_s:
            error["retry_after_s"] = int(self.retry_after_s)
        return error


@dataclass
class RequestState:
    request_id: str | None = None
    time_zone: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    failures: list[AIFailure] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_failure(self, failure: AIFailure) -> None:
        with self.lock:
            self.failures.append(failure)

    def fatal_failure(self) -> AIFailure | None:
        with self.lock:
            return next((item for item in self.failures if item.fatal), None)

    def last_failure(self) -> AIFailure | None:
        with self.lock:
            return self.failures[-1] if self.failures else None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


_STATE: contextvars.ContextVar[RequestState | None] = contextvars.ContextVar(
    "starling_request_state", default=None)

_INFLIGHT: dict[str, list[RequestState]] = {}
_INFLIGHT_LOCK = threading.Lock()


def clean_request_id(raw: object) -> str | None:
    value = str(raw or "").strip()
    return value if value and _REQUEST_ID_RE.match(value) else None


def clean_time_zone(raw: object) -> str | None:
    """A valid IANA zone name, or None. Never raises."""
    value = str(raw or "").strip()
    if not value or len(value) > 64 or not _TIME_ZONE_RE.match(value):
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(value)
    except Exception:                                       # noqa: BLE001
        return None
    return value


def current() -> RequestState | None:
    return _STATE.get()


def request_id() -> str | None:
    state = _STATE.get()
    return state.request_id if state else None


def time_zone() -> str | None:
    state = _STATE.get()
    return state.time_zone if state else None


def cancel_event() -> threading.Event | None:
    state = _STATE.get()
    return state.cancel_event if state else None


def cancelled() -> bool:
    state = _STATE.get()
    return bool(state and state.cancelled)


def fatal_failure() -> AIFailure | None:
    state = _STATE.get()
    return state.fatal_failure() if state else None


def last_failure() -> AIFailure | None:
    state = _STATE.get()
    return state.last_failure() if state else None


def record_failure(failure: AIFailure) -> None:
    """Remember a failed AI call for the request being served, if any."""
    state = _STATE.get()
    if state is not None:
        state.record_failure(failure)


@contextlib.contextmanager
def bind(request_id: object = None, time_zone: object = None,
         cancel: threading.Event | None = None) -> Iterator[RequestState]:
    """Serve everything inside this block as one request."""
    state = RequestState(request_id=clean_request_id(request_id),
                         time_zone=clean_time_zone(time_zone),
                         cancel_event=cancel or threading.Event())
    token = _STATE.set(state)
    if state.request_id:
        with _INFLIGHT_LOCK:
            _INFLIGHT.setdefault(state.request_id, []).append(state)
    try:
        yield state
    finally:
        if state.request_id:
            with _INFLIGHT_LOCK:
                states = _INFLIGHT.get(state.request_id) or []
                if state in states:
                    states.remove(state)
                if not states:
                    _INFLIGHT.pop(state.request_id, None)
        _STATE.reset(token)


def cancel(request_id: str) -> bool:
    """Stop the in-flight pipeline for `request_id`. True if one was running."""
    cleaned = clean_request_id(request_id)
    if not cleaned:
        return False
    with _INFLIGHT_LOCK:
        states = list(_INFLIGHT.get(cleaned) or [])
    for state in states:
        state.cancel_event.set()
    return bool(states)


def cancel_all() -> int:
    """Stop every in-flight request (graceful shutdown). Returns how many."""
    with _INFLIGHT_LOCK:
        states = [state for group in _INFLIGHT.values() for state in group]
    for state in states:
        state.cancel_event.set()
    return len(states)


def inflight_count() -> int:
    with _INFLIGHT_LOCK:
        return sum(len(group) for group in _INFLIGHT.values())


class RequestContextMiddleware:
    """Serve every HTTP request inside `bind(...)` (pure ASGI).

    Pure ASGI rather than BaseHTTPMiddleware so a streamed response stays inside
    the bound context until its last chunk: the stream's worker thread copies the
    context when it starts, and a cancel for the request id reaches it until the
    stream ends.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        found: dict[bytes, bytes] = {}
        for name, value in scope.get("headers") or ():
            lowered = name.lower()
            if lowered in (b"x-starling-request-id", b"x-starling-time-zone"):
                found[lowered] = value
        request_id = found.get(b"x-starling-request-id", b"").decode("latin-1")
        zone = found.get(b"x-starling-time-zone", b"").decode("latin-1")
        with bind(request_id, zone):
            await self.app(scope, receive, send)
