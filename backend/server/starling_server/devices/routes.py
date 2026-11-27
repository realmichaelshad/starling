"""/connect pages and /api/v1/devices/*. Owner: server core workstream.

Approval form fields (template `connect/approve.html`): `csrf_token`, `connect_request_id` and
`connect_state`, posted to `/connect/approve` or `/connect/deny`. Approval is never possible by GET.
The approval page carries its own Content-Security-Policy whose `form-action` also allows
`https://<extension id>.chromiumapp.org`, because browsers apply `form-action` to the redirect
that follows the form submission.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ..auth.deps import CurrentUser, DeviceAuth, current_user_async, login_redirect, require_device, require_user, verify_csrf
from ..security import clock, ratelimit
from ..security.errors import ApiError, PageError, StarlingRoute
from ..security.http import form_str, read_form, read_json_object
from ..security.middleware import CONTENT_SECURITY_POLICY
from ..web.render import render
from . import connect
from . import tokens as device_tokens

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

NO_STORE = {"Cache-Control": "no-store"}
EXPIRED_MESSAGE = "This connection request expired or was already used. Start connecting again from the extension."


def approval_csp(client_id: str) -> str:
    return re.sub(r"form-action [^;]*", f"form-action 'self' https://{client_id}.chromiumapp.org", CONTENT_SECURITY_POLICY)


async def _error_page(request: Request, message: str, extension_id: str | None = None, device_label: str | None = None) -> Response:
    return await render(
        request,
        "connect/error.html",
        {"extension_id": extension_id, "device_label": device_label, "error": message},
        status_code=400,
    )


@router.get("/connect")
async def connect_page(request: Request) -> Response:
    settings = request.app.state.settings
    db = request.app.state.db
    prefix = ratelimit.request_ip_prefix(request, settings)
    decision = await ratelimit.check(db, [(ratelimit.CONNECT_PER_IP, ratelimit.ip_key(prefix))])
    if not decision.allowed:
        raise PageError(429, "Too many connection attempts. Try again later.", headers={"Retry-After": str(decision.retry_after_s)})
    params = request.query_params
    known_client = params.get("client_id", "") in settings.extension_ids
    try:
        if any(len(params.getlist(name)) > 1 for name in connect.PARAMETERS):
            raise connect.ConnectError("The connection request is malformed. Start connecting again from the extension.")
        auth_request = connect.validate_request(settings, params)
    except connect.ConnectError as exc:
        return await _error_page(
            request,
            str(exc),
            extension_id=params.get("client_id") if known_client else None,
            device_label=connect.sanitize_label(params.get("device_label")) if known_client else None,
        )
    user = await current_user_async(request)
    if user is None:
        raise login_redirect(request)
    if user.status != "active":
        return await _error_page(request, "This account can't connect new devices. Contact support for help.")
    now = clock.now()
    request_id = await db.write_async(lambda conn: connect.create_request(conn, auth_request, user.account_id, now))
    return await render(
        request,
        "connect/approve.html",
        {
            "extension_id": auth_request.client_id,
            "device_label": auth_request.device_label,
            "connect_request_id": request_id,
            "connect_state": auth_request.state,
        },
        headers={"Content-Security-Policy": approval_csp(auth_request.client_id), "Referrer-Policy": "no-referrer"},
    )


async def _decide(request: Request, user: CurrentUser, approve: bool) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    request_id = form_str(form, "connect_request_id", 64)
    state = form_str(form, "connect_state", 128)
    now = clock.now()
    action = connect.approve if approve else connect.deny
    if approve and user.status != "active":
        return await _error_page(request, "This account can't connect new devices. Contact support for help.")
    try:
        location = await request.app.state.db.write_async(
            lambda conn: action(conn, settings, request_id=request_id, account_id=user.account_id, state=state, now=now)
        )
    except connect.DeviceLimitReached as exc:
        log.info("connect refused account=%s: %d devices already connected", user.account_id, exc.limit)
        return await _error_page(
            request,
            f"This account already has {exc.limit} connected browsers, which is the most it can have. "
            "Disconnect one you no longer use on the Devices page of your account, then connect this browser again.",
        )
    if location is None:
        return await _error_page(request, EXPIRED_MESSAGE)
    log.info("connect %s account=%s", "approved" if approve else "denied", user.account_id)
    return RedirectResponse(location, status_code=303, headers={**NO_STORE, "Referrer-Policy": "no-referrer"})


@router.post("/connect/approve", dependencies=[Depends(verify_csrf)])
async def connect_approve(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    return await _decide(request, user, approve=True)


@router.post("/connect/deny", dependencies=[Depends(verify_csrf)])
async def connect_deny(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    return await _decide(request, user, approve=False)


@router.post("/api/v1/devices/token")
async def device_token(request: Request) -> Response:
    settings = request.app.state.settings
    db = request.app.state.db
    prefix = ratelimit.request_ip_prefix(request, settings)
    decision = await ratelimit.check(db, [(ratelimit.DEVICE_TOKEN_PER_IP, ratelimit.ip_key(prefix))])
    if not decision.allowed:
        raise ApiError("rate_limited", 429, "Too many requests. Try again shortly.", retry_after_s=decision.retry_after_s)
    body = await read_json_object(request)
    if body.get("grant_type") != "authorization_code":
        raise ApiError("bad_request", 400, "Unsupported grant type.")
    raw_version = body.get("extension_version")
    version = raw_version.strip()[:32] if isinstance(raw_version, str) else None
    if not device_tokens.version_ok(version, settings.extension_min_version):
        raise ApiError("extension_outdated", 426, "Update the Starling extension to connect it.")
    now = clock.now()
    outcome = await db.write_async(
        lambda conn: connect.redeem(
            conn,
            settings,
            code=body.get("code"),
            code_verifier=body.get("code_verifier"),
            redirect_uri=body.get("redirect_uri"),
            client_id=body.get("client_id"),
            extension_version=version,
            now=now,
        )
    )
    if not outcome.ok or outcome.token is None or outcome.device is None:
        log.warning("device token refused reason=%s ip_prefix=%s", outcome.reason, prefix)
        if outcome.reason == "account_suspended":
            raise ApiError("account_suspended", 403, "This account is suspended. Contact support for help.")
        if outcome.reason == connect.DEVICE_LIMIT_REASON:
            raise ApiError(
                "bad_request", 400,
                "This account already has the most browsers it can connect. Disconnect one on the Devices page, then connect again.")
        raise ApiError("bad_request", 400, "This sign-in expired or was already used. Connect the extension again.")
    log.info("device connected account=%s device=%s", outcome.device.account_id, outcome.device.id)
    return JSONResponse(
        {
            "token": outcome.token,
            "device_id": outcome.device.id,
            "idle_expires_at": outcome.device.idle_expires_at,
            "expires_at": outcome.device.abs_expires_at,
        },
        headers={**NO_STORE, "Pragma": "no-cache"},
    )


@router.post("/api/v1/devices/current/revoke")
async def revoke_current_device(request: Request, device: DeviceAuth = Depends(require_device)) -> Response:
    await request.app.state.db.write_async(
        lambda conn: device_tokens.revoke(conn, device.device_id, clock.now(), "signed_out")
    )
    log.info("device signed out account=%s device=%s", device.account_id, device.device_id)
    return JSONResponse({"ok": True}, headers=NO_STORE)
