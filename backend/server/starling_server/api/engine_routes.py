"""/api/v1 routes forwarded to the customer's engine (CONTRACTS 6.1, 6.4, 6.5 and 7).

Owner: engine pool workstream.

| Route | Metering and consent | Engine path |
|---|---|---|
| `POST /api/v1/assist` mode `context` | metered, disclosure consent | `/browser/assist` |
| `POST /api/v1/assist` mode `schedule` | not metered | `/browser/assist` |
| `POST /api/v1/assist/stream` modes `chat`, `enhance_composer` | metered, disclosure consent | `/browser/assist/stream` |
| `GET /api/v1/requests/{key}` | stored result (replay) | - |
| `POST /api/v1/requests/{key}/cancel` | - | `/internal/requests/{id}/cancel` |
| `POST /api/v1/snapshots/profile` | capture, disclosure + passive learning | `/snapshots/profile` |
| `POST /api/v1/snapshots/metrics` | capture, disclosure (+ passive learning when passive) | `/snapshots/metrics` |
| `POST /api/v1/outcomes`, `/images/feedback`, `/preferences/confirm` | capture, disclosure | `/outcomes/record`, ... |
| `GET/POST /api/v1/own-material`, `DELETE /api/v1/own-material/{id}` | not metered | `/own-material/*` |

Captures never spawn an engine: they are spooled while the customer's engine is not running
and replayed right after its next start. Server core's `GET /api/v1/session` may call
`prewarm(request, device)`; account data export may call `export_profile(app, ...)`.

The website's writing page (`web/write.py`) drafts through the same machinery with a signed-in
person instead of a device: `metered_assist` (reserve, the account's slot, `/browser/assist`,
settle), `forward_unmetered` (writing samples) and `stored_result` (replay after a dropped
connection). Every function here only reads `account_id`, `tenant_seq`, `status` and
`email_verified` from its caller, which `DeviceAuth` and `auth.deps.CurrentUser` both carry. The
web passes its own disclosure as `consent=(kind, version)` (`web_disclosure`); the extension routes
keep `extension_disclosure` at STARLING_CONSENT_VERSION (`consent=None`).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from ..auth.deps import DeviceAuth, require_device
from ..engines.pool import (
    EngineMismatch,
    EnginePool,
    EngineTimeout,
    InflightRequest,
    PoolError,
    clean_time_zone,
)
from ..engines.streams import (
    ERROR_STATUS,
    MESSAGES,
    STREAM_HEADERS,
    STREAM_MEDIA_TYPE,
    MeteredStream,
    client_error,
    is_usable,
    replay_body,
    usage_concurrency,
    usage_service,
)
from ..security import clock, ratelimit
from ..security.errors import ApiError, StarlingRoute

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", route_class=StarlingRoute)

ASSIST_MAX_BYTES = 256 * 1024
CAPTURE_MAX_BYTES = 64 * 1024
OWN_MATERIAL_MAX_BYTES = 256 * 1024
CHAT_MESSAGE_MAX_CHARS = 2000
ASSIST_MODES = ("context", "schedule")
STREAM_MODES = ("chat", "enhance_composer")
OWN_MATERIAL_KINDS = ("voice", "background")
CONSENT_KIND = "extension_disclosure"
UPGRADE_PATH = "/pricing?from=extension&reason=limit"
NO_STORE = {"Cache-Control": "no-store"}
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


# --------------------------------------------------------------------------- helpers


def _pool(request: Request) -> EnginePool:
    pool = getattr(request.app.state, "engine_pool", None)
    if pool is None:
        raise ApiError("ai_unavailable", 503, MESSAGES["ai_unavailable"], retry_after_s=5)
    return pool


async def _read_body(request: Request, max_bytes: int) -> bytes:
    """The raw body with a streaming size bound (the exact bytes are hashed and forwarded)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_large = int(declared) > max_bytes
        except ValueError as exc:
            raise ApiError("bad_request", 400, "The request body could not be read.") from exc
        if too_large:
            raise ApiError("payload_too_large", 413, "The request body is too large.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise ApiError("payload_too_large", 413, "The request body is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError("bad_request", 400, "The request body must be JSON.") from exc
    if not isinstance(data, dict):
        raise ApiError("bad_request", 400, "The request body must be a JSON object.")
    return data


def _uuid_value(raw: str | None, message: str) -> str:
    value = (raw or "").strip()
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ApiError("bad_request", 400, message) from exc
    if str(parsed) != value.lower():
        raise ApiError("bad_request", 400, message)
    return str(parsed)


def _idempotency_key(request: Request) -> str:
    raw = request.headers.get("idempotency-key")
    if not raw or not raw.strip():
        raise ApiError("bad_request", 400, "An Idempotency-Key header is required.")
    return _uuid_value(raw, "The Idempotency-Key header must be a UUID.")


def _time_zone(request: Request) -> str | None:
    return clean_time_zone(request.headers.get("x-starling-time-zone"))


def _require_active(device: DeviceAuth) -> None:
    if device.status != "active":
        raise ApiError("account_suspended", 403, "This account is suspended. Contact support for help.")


def _require_verified(device: DeviceAuth) -> None:
    if not device.email_verified:
        raise ApiError("email_unverified", 403, "Verify your email address to start drafting.")


def _consent(request: Request, account_id: str, consent: tuple[str, str] | None = None) -> tuple[bool, bool]:
    """(current disclosure consent, passive learning allowed) under the current version.

    `consent` is a (kind, version) pair; None is the extension's disclosure at STARLING_CONSENT_VERSION."""
    settings = request.app.state.settings
    kind, version = consent or (CONSENT_KIND, settings.consent_version)
    with request.app.state.db.read() as conn:
        row = conn.execute(
            "SELECT passive_learning FROM consents WHERE account_id = ? AND kind = ? AND version = ? "
            "AND revoked_at IS NULL",
            (account_id, kind, version),
        ).fetchone()
    return row is not None, bool(row is not None and row["passive_learning"])


CONSENT_MESSAGES = {
    CONSENT_KIND: "Review and accept the extension disclosure to continue.",
    "web_disclosure": "Read what Starling sends and agree to it before your first draft.",
}


def _require_consent(request: Request, device: DeviceAuth, *, passive: bool,
                     consent: tuple[str, str] | None = None) -> None:
    disclosure, passive_allowed = _consent(request, device.account_id, consent)
    if not disclosure:
        kind = consent[0] if consent else CONSENT_KIND
        raise ApiError("consent_required", 403, CONSENT_MESSAGES.get(kind, CONSENT_MESSAGES[CONSENT_KIND]))
    if passive and not passive_allowed:
        raise ApiError("consent_required", 403, "Turn on learning from your visible X activity to send this.")


def _is_passive_metrics(payload: dict[str, Any]) -> bool:
    """A metrics capture is passive unless it names a non-passive capture mode."""
    metadata = payload.get("metadata")
    mode = metadata.get("capture_mode") if isinstance(metadata, dict) else None
    return not isinstance(mode, str) or mode.startswith("passive")


def _usage_error(exc: Exception) -> ApiError:
    status = int(getattr(exc, "http_status", 500))
    upgrade = getattr(exc, "upgrade_path", None) or (UPGRADE_PATH if status == 402 else None)
    return ApiError(
        str(getattr(exc, "code", "server_error")), status, str(getattr(exc, "message", MESSAGES["server_error"])),
        retry_after_s=getattr(exc, "retry_after_s", None), usage=getattr(exc, "usage", None), upgrade_path=upgrade,
    )


def _pool_error(exc: PoolError) -> ApiError:
    return ApiError(exc.code, exc.http_status, MESSAGES["ai_unavailable"], retry_after_s=exc.retry_after_s)


def _breaker_check(pool: EnginePool, account_id: str) -> None:
    retry_after = pool.breaker_retry_after(account_id)
    if retry_after is not None:
        raise ApiError("ai_unavailable", 503, MESSAGES["ai_unavailable"], retry_after_s=retry_after)


def _engine_detail(content: bytes) -> str | None:
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail.strip()[:300] if isinstance(detail, str) and detail.strip() else None


def _engine_response(status: int, content: bytes) -> Response:
    if 200 <= status < 300:
        try:
            json.loads(content or b"null")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError("server_error", 500, MESSAGES["server_error"]) from exc
        return Response(content=content or b"null", media_type="application/json", headers=NO_STORE)
    detail = _engine_detail(content)
    if status == 404:
        raise ApiError("not_found", 404, detail or "Not found.")
    if status == 413:
        raise ApiError("payload_too_large", 413, "The request body is too large.")
    if 400 <= status < 500:
        raise ApiError("bad_request", 400, detail or MESSAGES["bad_request"])
    raise ApiError("server_error", 500, MESSAGES["server_error"])


async def _forward(pool: EnginePool, device: DeviceAuth, method: str, path: str, body: bytes,
                   headers: dict[str, str], timeout_s: float | None = None) -> tuple[int, bytes]:
    try:
        return await pool.request(device.account_id, device.tenant_seq, method, path, body, headers,
                                  timeout_s or pool.options.request_timeout_s)
    except PoolError as exc:
        raise _pool_error(exc) from exc
    except EngineMismatch as exc:
        log.error("engine registry mismatch for tenant %s", device.tenant_seq)
        raise ApiError("server_error", 500, MESSAGES["server_error"]) from exc


async def _forward_unmetered(request: Request, device: DeviceAuth, path: str, body: bytes,
                             headers: dict[str, str], *, consent: tuple[str, str] | None = None) -> tuple[int, bytes]:
    """The requests that reach an engine without using a draft: the posting-time helper and the
    customer's own saved material.

    They start the customer's engine, and engines are the scarcest thing on the machine
    (`ENGINE_MAX_LIVE`, six by default). These routes used to check only that the account was not
    suspended: no confirmed email, no consent, no rate limit, no limit on requests in flight. A
    handful of throwaway accounts that never confirmed an address could each keep an engine busy
    for ever (an engine with a request in flight is never evicted), and every paying customer then
    got "Starling's AI isn't responding". The metered routes were never affected: `usage.reserve`
    checks all of this. Now these pass the same gate, and share the account's one request slot.
    """
    _require_active(device)
    _require_verified(device)
    _require_consent(request, device, passive=False, consent=consent)
    decision = await ratelimit.check(
        request.app.state.db, [(ratelimit.ENGINE_UNMETERED_PER_ACCOUNT, ratelimit.account_key(device.account_id))])
    if not decision.allowed:
        raise ApiError("rate_limited", 429, "Too many requests. Please wait a moment.", retry_after_s=decision.retry_after_s)
    usage = usage_service()
    try:
        async with usage_concurrency().account_slot(device.account_id):
            return await _forward(_pool(request), device, "POST", path, body, headers)
    except usage.UsageError as exc:
        raise _usage_error(exc) from exc


async def _reserve(request: Request, device: DeviceAuth, key: str, body: bytes, route: str, mode: str,
                   consent: tuple[str, str] | None = None):
    usage = usage_service()
    # The extension's routes call `reserve` exactly as before; only the web names its own consent.
    extra = {} if consent is None else {"consent": consent}
    try:
        return await asyncio.to_thread(
            usage.reserve, request.app.state.db, request.app.state.settings, device.account_id, key,
            usage.payload_hash(body), route, mode, clock.now(), **extra)
    except usage.UsageError as exc:
        raise _usage_error(exc) from exc


async def _release_quietly(request: Request, reservation_id: str, reason: str) -> dict[str, Any] | None:
    usage = usage_service()
    try:
        return await asyncio.to_thread(
            usage.release, request.app.state.db, request.app.state.settings, reservation_id, reason, clock.now())
    except Exception:
        log.exception("could not release reservation %s", reservation_id)
        return None


def _headers(time_zone: str | None, request_id: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if request_id:
        headers["X-Starling-Request-Id"] = request_id
    if time_zone:
        headers["X-Starling-Time-Zone"] = time_zone
    return headers


# --------------------------------------------------------------------------- assist


@router.post("/assist")
async def assist(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    body = await _read_body(request, ASSIST_MAX_BYTES)
    payload = _json_object(body)
    mode = payload.get("mode")
    if mode not in ASSIST_MODES:
        raise ApiError("bad_request", 400, "This route accepts the context and schedule modes only.")
    request.state.assist_mode = mode  # read by the app-wide API error recorder
    _require_active(device)
    pool = _pool(request)
    time_zone = _time_zone(request)
    if mode == "schedule":
        status, content = await _forward_unmetered(request, device, "/browser/assist", body, _headers(time_zone))
        return _engine_response(status, content)

    key = _idempotency_key(request)
    answer = await _metered_assist(request, pool, device, body, mode=mode, key=key, time_zone=time_zone,
                                   route="/assist", consent=None)
    return JSONResponse(answer, headers=NO_STORE)


async def metered_assist(request: Request, caller: Any, body: bytes, *, mode: str, key: str,
                         time_zone: str | None, route: str, consent: tuple[str, str] | None) -> Any:
    """One metered request to the caller's engine (`/browser/assist`), exactly as `POST /api/v1/assist`
    runs it: consent, circuit breaker, reservation (plan allowance, rate limits, fair use, pause),
    the account's single request slot, the engine call, settlement and the stored result for replay.

    Returns the engine's response with the `usage` block added, or a stored replay for a committed
    `key`. Refusals raise `ApiError` with the codes of CONTRACTS 6.3. `caller` is a `DeviceAuth` or
    a signed-in `CurrentUser`.
    """
    request.state.assist_mode = mode
    _require_active(caller)
    pool = _pool(request)
    return await _metered_assist(request, pool, caller, body, mode=mode, key=key, time_zone=time_zone,
                                 route=route, consent=consent)


async def _metered_assist(request: Request, pool: EnginePool, device: DeviceAuth, body: bytes, *, mode: str,
                          key: str, time_zone: str | None, route: str, consent: tuple[str, str] | None) -> Any:
    _require_consent(request, device, passive=False, consent=consent)
    _breaker_check(pool, device.account_id)
    result = await _reserve(request, device, key, body, route, mode, consent)
    usage = usage_service()
    if isinstance(result, usage.Replay):
        try:
            stored = json.loads(result.result_json)
        except ValueError as exc:
            raise ApiError("server_error", 500, MESSAGES["server_error"]) from exc
        if isinstance(stored, dict):
            stored = {**stored, "usage": result.usage}
        return stored

    request.state.reservation_id = result.id  # read by the app-wide API error recorder
    inflight = InflightRequest(device.account_id, device.tenant_seq, key, result.id, "assist")
    pool.inflight.add(inflight)
    try:
        try:
            slot = usage_concurrency().account_slot(device.account_id)
            await slot.__aenter__()
        except usage.UsageError as exc:
            await _release_quietly(request, result.id, "busy")
            raise _usage_error(exc) from exc
        try:
            return await _assist_in_slot(request, pool, device, inflight, body, time_zone)
        finally:
            # The engine answered (or never received the request): the request is finished.
            await slot.__aexit__(None, None, None)
    finally:
        pool.inflight.remove(inflight)


async def _assist_in_slot(request: Request, pool: EnginePool, device: DeviceAuth, inflight: InflightRequest,
                          body: bytes, time_zone: str | None) -> dict[str, Any]:
    usage = usage_service()
    db, settings, rid = request.app.state.db, request.app.state.settings, inflight.reservation_id
    try:
        await asyncio.to_thread(usage.activate, db, rid, clock.now())
    except usage.UsageError as exc:
        raise ApiError("ai_unavailable", 503, MESSAGES["expired"], retry_after_s=1) from exc
    if inflight.cancel_requested:
        block = await _release_quietly(request, rid, "cancelled")
        return {"ok": False, "cancelled": True, "usage": block}
    try:
        status, content = await pool.request(device.account_id, device.tenant_seq, "POST", "/browser/assist", body,
                                             _headers(time_zone, rid), pool.options.assist_total_s)
    except EngineTimeout as exc:
        with contextlib.suppress(Exception):
            await pool.cancel(device.account_id, device.tenant_seq, rid)
        await _release_quietly(request, rid, "timeout")
        raise ApiError("ai_unavailable", 503, MESSAGES["timeout"], retry_after_s=exc.retry_after_s) from exc
    except PoolError as exc:
        await _release_quietly(request, rid, "engine_unavailable")
        raise _pool_error(exc) from exc
    except EngineMismatch as exc:
        await _release_quietly(request, rid, "server_error")
        raise ApiError("server_error", 500, MESSAGES["server_error"]) from exc
    if inflight.cancel_requested:
        # Cancelled while the engine was still working (the extension's Cancel, or its own timeout):
        # the person was told "No draft was used", so the answer that arrives now is not charged.
        block = await _release_quietly(request, rid, "cancelled")
        return {"ok": False, "cancelled": True, "usage": block}
    response: Any = None
    if status == 200:
        with contextlib.suppress(ValueError, UnicodeDecodeError):
            response = json.loads(content)
    if not isinstance(response, dict):
        await _release_quietly(request, rid, "engine_error")
        if status in (400, 413, 422):
            raise ApiError("bad_request", 400, _engine_detail(content) or MESSAGES["bad_request"])
        raise ApiError("ai_unavailable", 503, MESSAGES["ai_unavailable"], retry_after_s=5)
    usable = is_usable(response)
    result_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    try:
        block = await asyncio.to_thread(usage.finish, db, settings, rid, usable, result_json, clock.now())
    except usage.UsageError as exc:
        raise _usage_error(exc) from exc
    engine_code = response.get("error_code")
    if response.get("ok") is False and isinstance(engine_code, str) and engine_code and not response.get("cancelled"):
        # The engine's own refusal (a services error it passed on, CONTRACTS 4.1), mapped as the stream
        # route maps it, so the extension shows its paused or fair-use card and "No draft was used".
        if engine_code == "bad_request":  # the engine could not use the request (its own validation)
            detail = response.get("error")
            code, message, retry_after = "bad_request", detail if isinstance(detail, str) and detail else MESSAGES["bad_request"], None
        else:
            code, message, retry_after = client_error({"code": engine_code, "retry_after_s": response.get("retry_after_s")})
        raise ApiError(code, ERROR_STATUS.get(code, 503), message,
                       retry_after_s=retry_after if retry_after is not None else (5 if code == "ai_unavailable" else None),
                       usage=block)
    return {**response, "usage": block}


@router.post("/assist/stream")
async def assist_stream(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    body = await _read_body(request, ASSIST_MAX_BYTES)
    payload = _json_object(body)
    mode = payload.get("mode")
    if mode not in STREAM_MODES:
        raise ApiError("bad_request", 400, "This route accepts the chat and enhance_composer modes only.")
    request.state.assist_mode = mode
    if mode == "chat":
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ApiError("bad_request", 400, "Write a message first.")
        if len(message) > CHAT_MESSAGE_MAX_CHARS:
            raise ApiError("bad_request", 400, f"Chat messages are limited to {CHAT_MESSAGE_MAX_CHARS} characters.")
    key = _idempotency_key(request)
    _require_active(device)
    pool = _pool(request)
    _require_consent(request, device, passive=False)
    _breaker_check(pool, device.account_id)
    result = await _reserve(request, device, key, body, "/assist/stream", mode)
    usage = usage_service()
    if isinstance(result, usage.Replay):
        return StreamingResponse(replay_body(result.result_json, result.usage), media_type=STREAM_MEDIA_TYPE,
                                 headers=dict(STREAM_HEADERS))
    request.state.reservation_id = result.id
    stream = MeteredStream(
        pool=pool,
        mode=mode,
        db=request.app.state.db,
        settings=request.app.state.settings,
        account_id=device.account_id,
        tenant_seq=device.tenant_seq,
        reservation_id=result.id,
        idempotency_key=key,
        body=body,
        time_zone=_time_zone(request),
    )
    return StreamingResponse(stream.body(), media_type=STREAM_MEDIA_TYPE, headers=dict(STREAM_HEADERS),
                             background=BackgroundTask(stream.cleanup_if_unstarted))


# --------------------------------------------------------------------------- stored results and cancel


@router.get("/requests/{idempotency_key}")
async def request_result(idempotency_key: str, request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    key = _uuid_value(idempotency_key, "The request id must be a UUID.")
    status, payload = await stored_result(request, device.account_id, key)
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


async def stored_result(request: Request, account_id: str, key: str) -> tuple[int, dict[str, Any]]:
    """(202, pending) while the request still runs for this account, (200, done with the stored result
    and usage) while one is kept, else `ApiError` 404. Never charges (CONTRACTS 6.5)."""
    pool = getattr(request.app.state, "engine_pool", None)
    if pool is not None and pool.inflight.get(account_id, key) is not None:
        return 202, {"request_id": key, "state": "pending"}
    usage = usage_service()
    replay = await asyncio.to_thread(usage.replay, request.app.state.db, account_id, key, clock.now())
    if replay is None:
        raise ApiError("not_found", 404, "There is no stored result for this request.")
    try:
        response = json.loads(replay.result_json)
    except ValueError as exc:
        raise ApiError("server_error", 500, MESSAGES["server_error"]) from exc
    return 200, {"request_id": key, "state": "done", "response": response, "usage": replay.usage}


@router.post("/requests/{idempotency_key}/cancel")
async def cancel_request(idempotency_key: str, request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    key = _uuid_value(idempotency_key, "The request id must be a UUID.")
    pool = _pool(request)
    item = pool.inflight.get(device.account_id, key)  # scoped to the calling account
    if item is None:
        raise ApiError("not_found", 404, "This request is not running.")
    item.cancel_requested = True
    item.control.set()
    if item.kind == "assist":
        with contextlib.suppress(ValueError, EngineMismatch):
            await pool.cancel(device.account_id, device.tenant_seq, item.reservation_id)
    return JSONResponse({"ok": True, "cancelled": True, "request_id": key}, headers=NO_STORE)


# --------------------------------------------------------------------------- captures


async def _capture(request: Request, device: DeviceAuth, engine_path: str, passive: bool | None) -> Response:
    body = await _read_body(request, CAPTURE_MAX_BYTES)
    payload = _json_object(body)
    _require_active(device)
    pool = _pool(request)
    _require_consent(request, device, passive=_is_passive_metrics(payload) if passive is None else passive)
    try:
        result = await pool.capture(device.account_id, device.tenant_seq, engine_path, body,
                                    _headers(_time_zone(request)))
    except PoolError as exc:
        raise _pool_error(exc) from exc
    except EngineMismatch as exc:
        log.error("engine registry mismatch for tenant %s", device.tenant_seq)
        raise ApiError("server_error", 500, MESSAGES["server_error"]) from exc
    if result.spooled:
        return Response(content=result.body, status_code=202, media_type="application/json", headers=NO_STORE)
    return _engine_response(result.status, result.body)


@router.post("/snapshots/profile")
async def snapshot_profile(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    return await _capture(request, device, "/snapshots/profile", passive=True)


@router.post("/snapshots/metrics")
async def snapshot_metrics(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    return await _capture(request, device, "/snapshots/metrics", passive=None)


@router.post("/outcomes")
async def outcomes(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    return await _capture(request, device, "/outcomes/record", passive=False)


@router.post("/images/feedback")
async def images_feedback(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    return await _capture(request, device, "/images/feedback", passive=False)


@router.post("/preferences/confirm")
async def preferences_confirm(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    return await _capture(request, device, "/preferences/confirm", passive=False)


# --------------------------------------------------------------------------- own material


@router.get("/own-material")
async def own_material_list(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    kind = request.query_params.get("kind")
    if kind is not None and kind not in OWN_MATERIAL_KINDS:
        raise ApiError("bad_request", 400, "Unknown material kind.")
    body = json.dumps({"kind": kind} if kind else {}).encode("utf-8")
    status, content = await _forward_unmetered(request, device, "/own-material/list", body, {})
    return _engine_response(status, content)


@router.post("/own-material")
async def own_material_add(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    body = await _read_body(request, OWN_MATERIAL_MAX_BYTES)
    _json_object(body)
    status, content = await _forward_unmetered(request, device, "/own-material/add", body, {})
    return _engine_response(status, content)


@router.delete("/own-material/{item_id}")
async def own_material_remove(item_id: str, request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    if not _ITEM_ID_RE.match(item_id):
        raise ApiError("bad_request", 400, "Unknown material id.")
    body = json.dumps({"id": item_id}).encode("utf-8")
    status, content = await _forward_unmetered(request, device, "/own-material/remove", body, {})
    return _engine_response(status, content)


# --------------------------------------------------------------------------- helpers for other routes


async def forward_unmetered(request: Request, caller: Any, path: str, payload: dict[str, Any], *,
                            consent: tuple[str, str] | None) -> dict[str, Any]:
    """One unmetered engine request (the writing samples) through the same gate as the extension's
    `/own-material` routes; returns the engine's JSON object or raises `ApiError`."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    status, content = await _forward_unmetered(request, caller, path, body, {}, consent=consent)
    response = _engine_response(status, content)
    try:
        data = json.loads(response.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError("server_error", 500, MESSAGES["server_error"]) from exc
    if not isinstance(data, dict):
        raise ApiError("server_error", 500, MESSAGES["server_error"])
    return data


def prewarm(request_or_app: Any, device: Any) -> None:
    """Start the account's engine in the background when a slot is free. Never evicts another
    customer's engine, never raises; meant for `GET /api/v1/session`."""
    app = getattr(request_or_app, "app", request_or_app)
    pool = getattr(getattr(app, "state", None), "engine_pool", None)
    if pool is None or getattr(device, "status", "active") != "active":
        return
    try:
        pool.prewarm(device.account_id, device.tenant_seq)
    except Exception:
        log.exception("engine prewarm could not be scheduled")


async def export_profile(app: Any, account_id: str, tenant_seq: int, timeout_s: float = 120.0) -> dict[str, Any]:
    """The customer's engine data export (`GET /profile/export`) for account data export."""
    pool: EnginePool = app.state.engine_pool
    status, content = await pool.request(account_id, tenant_seq, "GET", "/profile/export", b"", {}, timeout_s)
    if status != 200:
        raise RuntimeError(f"engine export failed with status {status}")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise RuntimeError("engine export is not a JSON object")
    return data
