"""Metered stream translation for `POST /api/v1/assist/stream` (CONTRACTS 6.4, 6.5 and 7).

The route authenticates, validates and reserves credits first. Then `MeteredStream.body()`:

1. sends `{"event": "status", "stage": "accepted", "request_id": <idempotency key>}` at once
   and only then starts the pump task, so the line always precedes any engine work;
2. relays what the pump produces, adding `heartbeat` after `heartbeat_s` without output.

The pump, a task of its own that a client disconnect cannot interrupt:

* waits for the account slot (`usage.concurrency.account_slot`), emitting `queued`;
* activates the reservation and opens the engine stream with `X-Starling-Request-Id`;
* passes engine `status` events; records exposure (`mark_exposed`) right before the first
  `token` is handed to the client; passes tokens;
* on engine `final`: `finish(usable, result)`, then `usage`, then `final`; a customer Stop after
  text was shown is the one unusable ending that uses a draft, and its `final` says
  `used_after_stop: true`;
* on engine `error` or a broken engine connection: `release` (never charged), then `usage`,
  then `error`;
* after `stream_total_s` (240 s): cancels the engine request, releases, emits
  `ai_unavailable`, and keeps draining;
* on client disconnect or `POST /requests/{key}/cancel`: cancels the engine request (again
  once the engine has started it) and keeps draining;
* holds the slot until the engine stream ends (final, error, end of stream) or
  `stream_hard_wall_s` (300 s) has passed; a client disconnect alone never frees it.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Callable

from ..config import Settings
from ..db import Database, request_errors
from .pool import EngineMismatch, EnginePool, InflightRequest, PoolError

log = logging.getLogger(__name__)

STREAM_MEDIA_TYPE = "application/x-ndjson"
STREAM_HEADERS = {"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"}
ENGINE_STREAM_PATH = "/browser/assist/stream"
MAX_CANCEL_ATTEMPTS = 3
_STAGE_RE = re.compile(r"^[A-Za-z0-9 _.:-]{1,64}$")
_END = object()

MESSAGES = {
    "ai_unavailable": "Starling could not finish this draft. Please try again.",
    "timeout": "Starling took too long to finish this draft. Please try again.",
    "service_paused": "Drafting is paused for a short while. Please try again later.",
    "fair_use_exceeded": "This account reached its fair-use limit for the period.",
    "bad_request": "Starling could not read this request.",
    "server_error": "Something went wrong on our side. Please try again.",
    "expired": "This request expired before it could start. Please try again.",
}
# Engine error codes (services errors of CONTRACTS 4.1 passed on by the engine) that keep their
# meaning for the client; every other engine code becomes `ai_unavailable`.
ENGINE_ERROR_CODES = {
    # An engine refusal of the request itself is the request's fault, not the AI's: saying "Starling's
    # AI isn't responding" for it sent people to retry something that could never work.
    "bad_request": "bad_request",
    "fair_use_exceeded": "fair_use_exceeded",
    "budget_exhausted": "service_paused",
    "service_paused": "service_paused",
}
STREAM_ROUTE = "/api/v1/assist/stream"
# HTTP status of each client error code (CONTRACTS 6.3), recorded for the owner dashboard.
ERROR_STATUS = {
    "bad_request": 400,
    "fair_use_exceeded": 402,
    "busy": 409,
    "server_error": 500,
    "ai_unavailable": 503,
    "service_paused": 503,
}


def usage_service():
    return importlib.import_module("starling_server.usage.service")


def usage_concurrency():
    return importlib.import_module("starling_server.usage.concurrency")


def _now() -> int:
    return importlib.import_module("starling_server.security.clock").now()


def ndjson(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def is_usable(response: object) -> bool:
    """CONTRACTS 3.4: `ok` is true, the result is neither `withheld` nor `unchanged`, and
    `decision.draft` or `answer` is a non-empty string.

    `unchanged` is an Enhance that found nothing to improve and hands the customer's own words
    back: the draft field is full, and nothing in it is the service's work.
    """
    if not isinstance(response, dict) or response.get("ok") is not True:
        return False
    if response.get("withheld") or response.get("unchanged"):
        return False
    decision = response.get("decision")
    draft = decision.get("draft") if isinstance(decision, dict) else None
    return any(isinstance(value, str) and value.strip() for value in (draft, response.get("answer")))


def client_error(engine_error: object) -> tuple[str, str, int | None]:
    """(code, message, retry_after_s) for the client from an engine `error` payload."""
    code, retry_after = "ai_unavailable", None
    if isinstance(engine_error, dict):
        raw = engine_error.get("code")
        if isinstance(raw, str):
            code = ENGINE_ERROR_CODES.get(raw, "ai_unavailable")
        status = engine_error.get("status")
        if raw == "engine_http_error" and isinstance(status, int) and status in (400, 413, 422):
            code = "bad_request"
        value = engine_error.get("retry_after_s")
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 86_400:
            retry_after = value
    return code, MESSAGES[code], retry_after


def replay_body(result_json: str, usage: dict[str, Any]) -> AsyncIterator[bytes]:
    """A committed request replayed on the stream route: `usage` and `final` only."""

    async def body() -> AsyncIterator[bytes]:
        try:
            response = json.loads(result_json)
        except ValueError:
            response = {"ok": False, "error": "The stored result could not be read."}
        yield ndjson({"event": "usage", "usage": usage})
        yield ndjson({"event": "final", "response": response})

    return body()


class MeteredStream:
    def __init__(
        self,
        *,
        pool: EnginePool,
        db: Database,
        settings: Settings,
        account_id: str,
        tenant_seq: int,
        reservation_id: str,
        idempotency_key: str,
        body: bytes,
        time_zone: str | None,
        mode: str = "",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._db = db
        self._settings = settings
        self._account_id = account_id
        self._tenant_seq = tenant_seq
        self._key = idempotency_key
        self._body = body
        self._time_zone = time_zone
        self._mode = mode
        self._monotonic = monotonic
        self.reservation_id = reservation_id
        self.inflight = InflightRequest(account_id, tenant_seq, idempotency_key, reservation_id, "stream")
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._client_gone = False
        self._delivered_end = False
        self._terminal_sent = False
        self._settled = False
        self._cancel_attempts = 0
        self._cancel_acknowledged = False
        # Observable outcome (logs and tests).
        self.outcome: str | None = None
        self.exposed = False
        self.engine_ended_at: float | None = None
        self.slot_released_at: float | None = None

    # ------------------------------------------------------------------ client side

    async def body(self) -> AsyncIterator[bytes]:
        yield ndjson({"event": "status", "stage": "accepted", "request_id": self._key})
        self._pool.inflight.add(self.inflight)
        self._task = self._pool._track(asyncio.create_task(self._run_guarded(), name="assist-stream"))
        heartbeat_s = self._pool.options.heartbeat_s
        last_output = self._monotonic()
        try:
            while True:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    wait = max(0.001, heartbeat_s - (self._monotonic() - last_output))
                    try:
                        item = await asyncio.wait_for(self._queue.get(), timeout=wait)
                    except asyncio.TimeoutError:
                        yield ndjson({"event": "heartbeat"})
                        last_output = self._monotonic()
                        continue
                if item is _END:
                    self._delivered_end = True
                    return
                yield item
                last_output = self._monotonic()
        finally:
            # No awaits here: this runs inside a cancelled scope when the client disconnects.
            if not self._delivered_end:
                self._client_gone = True
                self.inflight.control.set()

    async def cleanup_if_unstarted(self) -> None:
        """Response background task: release the reservation when the client left before the
        first line was even sent (the pump never started)."""
        if self._task is None and not self._settled:
            self._client_gone = True
            await self._release("client_disconnected")
            self.outcome = "client_gone_before_start"

    def _emit(self, event: dict[str, Any]) -> None:
        if not self._client_gone:
            self._queue.put_nowait(ndjson(event))

    def _emit_usage(self, usage: dict[str, Any] | None) -> None:
        if usage is not None:
            self._emit({"event": "usage", "usage": usage})

    def _emit_error(self, code: str, message: str | None = None, retry_after_s: int | None = None,
                    http_status: int | None = None) -> None:
        if self._terminal_sent:
            return
        error: dict[str, Any] = {"code": code, "message": message or MESSAGES.get(code, MESSAGES["ai_unavailable"])}
        if retry_after_s:
            error["retry_after_s"] = int(retry_after_s)
        self._terminal_sent = True
        if self._client_gone:
            return  # nothing is emitted to a client that already left
        self._emit({"event": "error", "error": error})
        # The 200 response has already started, so the API error handler never sees this failure.
        request_errors.record(
            self._db, route=STREAM_ROUTE, code=code, http_status=http_status or ERROR_STATUS.get(code, 503),
            now=_now(), account_id=self._account_id, mode=self._mode, reserved=True)

    # ------------------------------------------------------------------ pump

    async def _run_guarded(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            if not self._settled:
                with contextlib.suppress(BaseException):
                    await self._release("shutdown")
            self.outcome = self.outcome or "cancelled"
            raise
        except Exception:
            log.exception("assist stream for reservation %s failed", self.reservation_id)
            if not self._settled:
                self._emit_usage(await self._release("server_error"))
            self._emit_error("server_error")
            self.outcome = self.outcome or "server_error"
        finally:
            self._pool.inflight.remove(self.inflight)
            self._queue.put_nowait(_END)

    async def _run(self) -> None:
        usage = usage_service()
        concurrency = usage_concurrency()
        options = self._pool.options
        started = self._monotonic()
        queued = False
        status = getattr(concurrency, "slot_status", None)
        if status is not None:
            with contextlib.suppress(Exception):
                queued = bool(status(self._account_id)[0])
        if queued:
            self._emit({"event": "queued", "position": 1})
        notice = None if queued else asyncio.get_running_loop().call_later(
            options.queued_notice_s, lambda: self._emit({"event": "queued", "position": 1}))
        slot = concurrency.account_slot(self._account_id)
        try:
            await slot.__aenter__()
        except usage.UsageError as exc:
            self._emit_usage(await self._release("busy"))
            self._emit_error(exc.code, exc.message, exc.retry_after_s, http_status=exc.http_status)
            self.outcome = "busy"
            return
        finally:
            if notice is not None:
                notice.cancel()
        try:
            await self._serve(started, usage)
        finally:
            await slot.__aexit__(None, None, None)
            self.slot_released_at = self._monotonic()

    async def _serve(self, started: float, usage: Any) -> None:
        options = self._pool.options
        if self._client_gone:
            await self._release("client_disconnected")
            self.outcome = "client_gone_before_start"
            return
        try:
            await asyncio.to_thread(usage.activate, self._db, self.reservation_id, _now())
        except usage.UsageError:
            self._settled = True  # settled elsewhere (for example expired while queued)
            self._emit_error("ai_unavailable", MESSAGES["expired"])
            self.outcome = "inactive"
            return
        headers = {"X-Starling-Request-Id": self.reservation_id}
        if self._time_zone:
            headers["X-Starling-Time-Zone"] = self._time_zone
        events = self._pool.stream(self._account_id, self._tenant_seq, ENGINE_STREAM_PATH, self._body, headers)
        pending: asyncio.Future | None = None
        timed_out = False
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(events.__anext__())
                elapsed = self._monotonic() - started
                if not timed_out and not self._settled:
                    remaining = options.stream_total_s - elapsed
                    if remaining <= 0:
                        timed_out = True
                        log.warning("assist stream for reservation %s hit the time limit", self.reservation_id)
                        await self._cancel_engine()
                        self._emit_usage(await self._release("timeout"))
                        self._emit_error("ai_unavailable", MESSAGES["timeout"])
                        self.outcome = "timeout"
                        continue
                else:
                    remaining = options.stream_hard_wall_s - elapsed
                    if remaining <= 0:
                        log.warning("engine stream for reservation %s passed the hard wall", self.reservation_id)
                        self.outcome = self.outcome or "hard_wall"
                        break
                signal = await self._wait(pending, remaining)
                if signal == "control":
                    if self._client_gone or self.inflight.cancel_requested:
                        await self._cancel_engine()
                    continue
                if signal == "timeout":
                    continue
                future, pending = pending, None
                try:
                    event = future.result()
                except StopAsyncIteration:
                    self.engine_ended_at = self._monotonic()
                    await self._settle_failure("engine_stream_ended", "ai_unavailable", None)
                    break
                except (PoolError, EngineMismatch) as exc:
                    self.engine_ended_at = self._monotonic()
                    retry_after = exc.retry_after_s if isinstance(exc, PoolError) else None
                    await self._settle_failure("engine_unavailable", "ai_unavailable", retry_after)
                    break
                if (self._client_gone or self.inflight.cancel_requested) and not self._cancel_acknowledged:
                    await self._cancel_engine()  # the engine knows the request id once it emits
                if await self._handle_event(event, usage, timed_out):
                    break
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(BaseException):
                    await pending
            with contextlib.suppress(BaseException):
                await events.aclose()

    async def _handle_event(self, event: dict[str, Any], usage: Any, timed_out: bool) -> bool:
        """Apply one engine event; True when the engine stream is finished."""
        kind = event.get("event")
        stopping = self._client_gone or self.inflight.cancel_requested or timed_out or self._settled
        if kind == "status":
            stage = event.get("stage")
            if not stopping and isinstance(stage, str) and _STAGE_RE.match(stage) and stage != "accepted":
                self._emit({"event": "status", "stage": stage})
            return False
        if kind == "token":
            text = event.get("text")
            if stopping or not isinstance(text, str) or not text:
                return False  # never shown to the client, so never exposure
            if not self.exposed:
                await asyncio.to_thread(usage.mark_exposed, self._db, self.reservation_id, _now())
                self.exposed = True
            self._emit({"event": "token", "text": text})
            return False
        if kind == "final":
            self.engine_ended_at = self._monotonic()
            response = event.get("response")
            if self._settled:
                return True
            if self._client_gone:
                await self._release("client_disconnected")
                self.outcome = "client_gone"
                return True
            if not isinstance(response, dict):
                response = {"ok": False, "error": MESSAGES["ai_unavailable"]}
            usable = is_usable(response)
            result_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            stopped = self.inflight.cancel_requested
            try:
                block = await asyncio.to_thread(
                    usage.finish, self._db, self._settings, self.reservation_id, usable, result_json, _now(),
                    customer_stopped=stopped)
            except Exception:
                log.exception("could not settle reservation %s", self.reservation_id)
                block = None
            if block is not None and stopped and not usable and self.exposed:
                with contextlib.suppress(Exception):
                    if await asyncio.to_thread(usage.settled_as, self._db, self.reservation_id) == "committed":
                        response = {**response, "used_after_stop": True}
            self._settled = True
            self.outcome = "cancelled" if stopped else ("final" if usable else "unusable")
            self._emit_usage(block)
            self._terminal_sent = True
            self._emit({"event": "final", "response": response})
            return True
        if kind == "error":
            self.engine_ended_at = self._monotonic()
            code, message, retry_after = client_error(event.get("error"))
            await self._settle_failure("engine_error", code, retry_after, message)
            return True
        return False

    async def _settle_failure(self, reason: str, code: str, retry_after: int | None, message: str | None = None) -> None:
        if self._settled:
            return
        block = await self._release("client_disconnected" if self._client_gone else reason)
        self.outcome = self.outcome or ("client_gone" if self._client_gone else reason)
        self._emit_usage(block)
        self._emit_error(code, message, retry_after)

    async def _wait(self, pending: asyncio.Future, timeout: float) -> str:
        control = self.inflight.control
        if control.is_set():
            control.clear()
            return "control"
        waiter = asyncio.ensure_future(control.wait())
        try:
            done, _ = await asyncio.wait({pending, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if pending in done:
            return "event"
        if waiter in done:
            control.clear()
            return "control"
        return "timeout"

    async def _cancel_engine(self) -> None:
        if self._cancel_acknowledged or self._cancel_attempts >= MAX_CANCEL_ATTEMPTS:
            return
        self._cancel_attempts += 1
        try:
            acknowledged = await self._pool.cancel(self._account_id, self._tenant_seq, self.reservation_id)
        except (ValueError, EngineMismatch):
            acknowledged = False
        self._cancel_acknowledged = self._cancel_acknowledged or acknowledged

    async def _release(self, reason: str) -> dict[str, Any] | None:
        self._settled = True
        usage = usage_service()
        try:
            return await asyncio.to_thread(
                usage.release, self._db, self._settings, self.reservation_id, reason, _now())
        except Exception:
            log.exception("could not release reservation %s", self.reservation_id)
            return None
