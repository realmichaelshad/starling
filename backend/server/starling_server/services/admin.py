"""Owner diagnostics: `POST /admin/ai-check` and `GET /admin/status` (gateway admin token only).

Key values never leave this module: status shows whether each key is set and its last 4 characters.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Settings
from .auth import authenticate
from .capabilities import CapabilityTable
from .errors import ServiceError
from .metering import BUCKET_ADMIN, CallContext, Metering
from .proxy import (
    LANES,
    ChatRequest,
    MeteredHooks,
    UnmeteredHooks,
    build_upstream_body,
    json_attempt_record,
    model_for_lane,
)
from .upstream import UpstreamClient

router = APIRouter()

CHECK_TIMEOUT_S = 20.0
CHECK_PROMPT = "Reply with the single word OK."
CHECK_MAX_OUTPUT_TOKENS = 16
CHECK_IDENTITY = "starling-admin-check"


def explain(code: str, model: str, detail: str = "") -> str:
    """An actionable sentence for the owner for each failure code."""
    messages = {
        "not_configured": (
            "AI_API_KEY is not set in the services environment. Add the key from the AI provider "
            "dashboard and restart the services process."
        ),
        "ai_auth": (
            "The AI provider rejected the API key (HTTP 401/403). Check AI_API_KEY (and AI_API_KEY_SECONDARY "
            "if set) in the services environment; a revoked, expired or mistyped key causes this."
        ),
        "ai_model": (
            f"The model {model} was not found for this key. Check AI_MODEL_QUALITY, AI_MODEL_FAST and "
            "AI_MODEL_VISION, and that the provider project is allowed to use this model."
        ),
        "ai_quota": (
            "The AI provider reports insufficient quota or a spend limit. Add credit or raise the "
            "project's spend limit in the provider dashboard."
        ),
        "ai_rate_limited": (
            "The AI provider is rate limiting this key. Wait a minute and try again, or raise the usage "
            "tier with the provider."
        ),
        "ai_unavailable": (
            "The AI provider did not answer in time or returned a server error. Try again later, and check "
            "AI_BASE_URL and outbound network access from the server."
        ),
        "bad_request": (
            f"The AI provider rejected the test request ({detail or 'no detail'}). Check the model name and "
            "AI_REASONING_EFFORT."
        ),
        "input_too_large": "The AI provider rejected the test request as too large.",
    }
    return messages.get(code, f"The check failed ({code}).")


def key_status(settings: Settings) -> dict[str, Any]:
    def mask(key: str) -> dict[str, Any]:
        return {"configured": bool(key), "last4": key[-4:] if len(key) >= 8 else None}

    return {"primary": mask(settings.ai_api_key), "secondary": mask(settings.ai_api_key_secondary)}


def safe_base_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "invalid"
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


async def run_ai_check(
    settings: Settings,
    upstream: UpstreamClient,
    capabilities: CapabilityTable,
    *,
    metering: Metering | None = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Send a minimal request to each distinct configured model; report per model."""
    lanes_by_model: dict[str, list[str]] = {}
    for lane in LANES:
        lanes_by_model.setdefault(model_for_lane(settings, lane), []).append(lane)
    if not settings.ai_api_key and not settings.ai_api_key_secondary:
        results = [
            {
                "model": model,
                "lanes": lanes,
                "ok": False,
                "latency_ms": None,
                "error_code": "not_configured",
                "message": explain("not_configured", model),
            }
            for model, lanes in lanes_by_model.items()
        ]
    else:
        results = list(
            await asyncio.gather(
                *(
                    _check_model(settings, upstream, capabilities, model, lanes, metering, clock, monotonic)
                    for model, lanes in lanes_by_model.items()
                )
            )
        )
    return {
        "ok": all(result["ok"] for result in results),
        "checked_at": int(clock()),
        "provider": settings.ai_provider,
        "base_url": safe_base_url(settings.ai_base_url),
        "keys": key_status(settings),
        "results": results,
    }


async def _check_model(
    settings: Settings,
    upstream: UpstreamClient,
    capabilities: CapabilityTable,
    model: str,
    lanes: list[str],
    metering: Metering | None,
    clock: Callable[[], float],
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    lane = lanes[0]
    chat = ChatRequest(
        lane=lane,
        messages=[{"role": "user", "content": CHECK_PROMPT}],
        max_output_tokens=min(CHECK_MAX_OUTPUT_TOKENS, settings.ai_max_output_tokens),
        input_chars=len(CHECK_PROMPT),
    )
    body = build_upstream_body(chat, settings, capabilities, CHECK_IDENTITY)
    hooks = (
        MeteredHooks(metering, CallContext(bucket=BUCKET_ADMIN, lane=lane, model=model), chat)
        if metering is not None
        else UnmeteredHooks()
    )
    started = monotonic()
    try:
        success = await upstream.complete(
            body,
            stream=False,
            deadline=started + min(settings.ai_timeout_s, CHECK_TIMEOUT_S),
            hooks=hooks,
            max_retries=0,
        )
    except ServiceError as exc:
        return {
            "model": model,
            "lanes": lanes,
            "ok": False,
            "latency_ms": int(max(0.0, monotonic() - started) * 1000),
            "error_code": exc.code,
            "message": explain(exc.code, model, exc.message),
        }
    await hooks.succeeded(success.admission, json_attempt_record(success.payload or {}, chat))
    notes = ["The model answered."]
    dropped = sorted(set(body) - set(success.body))
    if dropped:
        notes.append(f"It does not accept {', '.join(dropped)}; the proxy omits it from now on.")
    today = datetime.fromtimestamp(int(clock()), tz=timezone.utc).date()
    if settings.ai_pricing.price_for(model, today) is None:
        notes.append("No price is configured for this model in AI_PRICING_FILE, so the proxy refuses it until one is added.")
    return {
        "model": model,
        "lanes": lanes,
        "ok": True,
        "latency_ms": success.latency_ms,
        "error_code": None,
        "message": " ".join(notes),
    }


async def build_status(app: FastAPI) -> dict[str, Any]:
    state = app.state
    settings: Settings = state.settings
    snapshot = await state.metering.snapshot()
    models = {lane: model_for_lane(settings, lane) for lane in LANES}
    return {
        "ok": True,
        "generated_at": int(state.clock()),
        "started_at": state.started_at,
        "paused": snapshot["paused"],
        "budgets": snapshot["budgets"],
        "ai_calls_today": snapshot["calls_today"],
        "ai_calls_in_flight": snapshot["in_flight"],
        "alerts": snapshot["alerts"],
        "keys": {**key_status(settings), "active": state.upstream.active_slot()},
        "upstream": {
            "provider": settings.ai_provider,
            "base_url": safe_base_url(settings.ai_base_url),
            **state.upstream.health.as_dict(),
        },
        "models": models,
        "reasoning_effort": settings.ai_reasoning_effort or None,
        "prices_configured": {model: state.metering.has_price(model) for model in sorted(set(models.values()))},
        "capabilities": state.capabilities.snapshot(models.values()),
        "world": {"news": state.world.pool.freshness(), "summaries": state.world.summaries.status()},
        "config_problems": list(state.config_problems),
    }


@router.post("/admin/ai-check")
async def ai_check_route(request: Request) -> JSONResponse:
    authenticate(request, tenant=False, admin=True)
    state = request.app.state
    result = await run_ai_check(
        state.settings,
        state.upstream,
        state.capabilities,
        metering=state.metering,
        clock=state.clock,
        monotonic=state.monotonic,
    )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/admin/status")
async def status_route(request: Request) -> JSONResponse:
    authenticate(request, tenant=False, admin=True)
    return JSONResponse(await build_status(request.app), headers={"Cache-Control": "no-store"})
