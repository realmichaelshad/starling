"""/api/v1/plans, /session, /consent. Owner: server core workstream.

- `GET /api/v1/plans` (public): {"unit": {"name", "label"}, "plans": [plan, ...]} with the plan
  shape of `web.render.plan_dict` (public plans only, no Stripe identifiers or cost ceilings).
- `GET /api/v1/session`: `usage.service.account_state(...)` for the calling device's account.
- `POST /api/v1/consent` {"version", "passive_learning"}: `version` must equal
  STARLING_CONSENT_VERSION; records `consents` kind `extension_disclosure`.
- `POST /api/v1/consent/withdraw`: revokes that consent and signs the calling device out.
"""
from __future__ import annotations

import logging
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from ..auth.deps import DeviceAuth, require_device
from ..devices import tokens as device_tokens
from ..security import clock
from ..security.errors import ApiError, StarlingRoute
from ..security.http import read_json_object
from ..usage import service as usage_service
from ..web.render import plans_context, unit_context
from .engine_routes import prewarm as prewarm_engine

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

CONSENT_KIND = "extension_disclosure"
NO_STORE = {"Cache-Control": "no-store"}


async def load_account_state(request: Request, account_id: str) -> dict[str, Any]:
    """The shared account state of CONTRACTS 6.2 (usage workstream), run off the event loop."""
    settings = request.app.state.settings
    db = request.app.state.db
    try:
        return await anyio.to_thread.run_sync(usage_service.account_state, db, settings, account_id, clock.now())
    except usage_service.UsageError as exc:
        raise ApiError(exc.code, exc.http_status, exc.message, retry_after_s=exc.retry_after_s, usage=exc.usage) from exc


@router.get("/api/v1/plans")
async def plans(request: Request) -> Response:
    settings = request.app.state.settings
    return JSONResponse({"unit": unit_context(settings), "plans": plans_context(settings)}, headers={"Cache-Control": "public, max-age=300"})


@router.get("/api/v1/session")
async def session_state(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    state = await load_account_state(request, device.account_id)
    if _worth_prewarming(state):
        # Engine pool: schedule a background start of this customer's engine when a slot is free.
        # Fire and forget: never awaited, never evicts another customer's engine, never fails this response.
        try:
            prewarm_engine(request, device)
        except Exception:
            log.exception("engine prewarm could not be scheduled")
    return JSONResponse(state, headers=NO_STORE)


def _worth_prewarming(state: dict[str, Any]) -> bool:
    """Only accounts that can use their engine now: active, confirmed, disclosure accepted, service running.

    An unconfirmed account is refused on every draft, so warming its engine would only hold a slot
    that a customer who can draft may need.
    """
    try:
        consent = state["consent"]
        return (
            state["account"]["status"] == "active"
            and state["account"].get("email_verified") is True
            and consent["accepted_version"] is not None
            and consent["accepted_version"] == consent["required_version"]
            and state["service"]["status"] == "ok"
        )
    except (KeyError, TypeError):
        return False


@router.post("/api/v1/consent")
async def record_consent(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    settings = request.app.state.settings
    body = await read_json_object(request)
    version = body.get("version")
    passive_learning = body.get("passive_learning")
    if not isinstance(version, str) or not isinstance(passive_learning, bool):
        raise ApiError("bad_request", 400, "Send the disclosure version and your passive learning choice.")
    if version != settings.consent_version:
        raise ApiError("bad_request", 400, "The disclosure has changed. Review it again before continuing.")
    now = clock.now()
    await request.app.state.db.write_async(
        lambda conn: conn.execute(
            "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT (account_id, kind, version) DO UPDATE SET "
            "passive_learning = excluded.passive_learning, granted_at = excluded.granted_at, revoked_at = NULL",
            (device.account_id, CONSENT_KIND, version, 1 if passive_learning else 0, now),
        )
    )
    log.info("consent recorded account=%s passive_learning=%s", device.account_id, passive_learning)
    return JSONResponse(
        {
            "consent": {
                "required_version": settings.consent_version,
                "accepted_version": version,
                "passive_learning": passive_learning,
            }
        },
        headers=NO_STORE,
    )


@router.post("/api/v1/consent/withdraw")
async def withdraw_consent(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    now = clock.now()

    def transaction(conn):
        conn.execute(
            "UPDATE consents SET revoked_at = ? WHERE account_id = ? AND kind = ? AND revoked_at IS NULL",
            (now, device.account_id, CONSENT_KIND),
        )
        device_tokens.revoke(conn, device.device_id, now, "consent_withdrawn")

    await request.app.state.db.write_async(transaction)
    log.info("consent withdrawn account=%s device=%s", device.account_id, device.device_id)
    return JSONResponse({"ok": True}, headers=NO_STORE)
