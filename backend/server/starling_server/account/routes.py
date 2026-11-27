"""/account overview, billing view, devices and data pages. Owner: server core workstream (billing actions live in billing.routes).

Context shapes:

- `devices`: list of {"id", "label", "extension_id", "extension_version", "created_at", "last_used_at",
  "idle_expires_at", "expires_at"} (active devices only, newest first).
- `sessions`: list of {"id" (short, display only), "current", "created_at", "last_seen_at",
  "user_agent_summary", "ip_prefix"} (active sessions, most recently used first).

- `sign_in` (Security and Data pages): {"has_password", "google_available", "google" (None or
  {"email_masked", "connected_at"}), "recent" (signed in within `auth.service.RECENT_SIGN_IN_S`),
  "reauth_url"}. An account without a password confirms sensitive changes with a recent sign-in
  instead: the pages hide the password fields and, once the sign-in is older, offer "Confirm with
  Google" (`reauth_url`).

Form fields and error keys (general key per form in brackets):
password change `current_password` (absent for an account without one), `new_password`, optional
`new_password_confirm` ["password"]; email change `new_email`, `email_password` ["email"]; other
sessions ["sessions"]; Google disconnect `google_password` ["google"]; export ["export"]; deletion
`delete_password`, `delete_confirmation` (must be `DELETE`) ["delete"].
Every POST requires `csrf_token`.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from ..auth import accounts, service, sessions
from ..auth.deps import CurrentUser, forget, require_user, resolve, resolve_async, verify_csrf
from ..devices import tokens as device_tokens
from ..security import clock, ratelimit
from ..security.errors import PageError, StarlingRoute
from ..security.http import form_str, read_form
from ..usage import service as usage_service
from ..web import flash
from ..web.render import account_state, plans_context, render
from . import deletion, export

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

PASSWORD_FIELD_MAX = 4096
UNAVAILABLE_RETRY_S = 300

_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Version/", "Safari"))
_SYSTEMS = (
    ("Windows", "Windows"), ("CrOS", "ChromeOS"), ("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iPadOS"),
    ("Mac OS X", "macOS"), ("Linux", "Linux"),
)


def user_agent_summary(user_agent: str | None) -> str:
    """A short, human description such as "Chrome 128 on macOS" (never the raw header)."""
    text = user_agent or ""
    browser = None
    for token, name in _BROWSERS:
        index = text.find(token)
        if index < 0 or (name == "Safari" and "Safari/" not in text):
            continue
        version = re.match(r"\d+", text[index + len(token):])
        browser = f"{name} {version.group(0)}" if version else name
        break
    system = next((name for token, name in _SYSTEMS if token in text), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown browser"


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303, headers={"Cache-Control": "no-store"})


async def _state(request: Request, user: CurrentUser) -> dict[str, Any]:
    try:
        return await account_state(request, user.account_id)
    except usage_service.UsageError as exc:
        raise PageError(exc.http_status if 400 <= exc.http_status < 500 else 503, exc.message) from exc


@router.get("/account")
async def overview(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    state = await _state(request, user)
    with request.app.state.db.read() as conn:
        account = accounts.get_by_id(conn, user.account_id)
        epoch = account.auth_epoch if account is not None else 0
        devices_count = len(device_tokens.list_active(conn, user.account_id, epoch, clock.now()))
    # `devices_count` drives the "Get started" checklist: installed and connected is one fact here.
    return await render(request, "account/overview.html", {"state": state, "devices_count": devices_count})


# Where a return from Stripe's pages says the customer went (billing.service.create_portal, start_upgrade).
RETURNS = {"home", "card", "cancel", "upgrade"}


@router.get("/account/billing")
async def billing_page(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    returned = request.query_params.get("portal") or ("upgrade" if request.query_params.get("upgrade") == "confirmed" else None)
    returned = returned if returned in RETURNS else None
    client = getattr(request.app.state, "stripe_client", None)
    refresh = returned is not None and client is not None
    if refresh:
        limit = await ratelimit.check(
            request.app.state.db, [(ratelimit.BILLING_REFRESH_PER_ACCOUNT, ratelimit.account_key(user.account_id))]
        )
        refresh = limit.allowed
    if refresh:
        # Back from Stripe: read the subscription now, so a cancellation, an upgrade or a new card
        # shows on this very page instead of after the webhook. Never fatal: the webhook still comes.
        from ..billing import service as billing_service

        try:
            await anyio.to_thread.run_sync(
                billing_service.refresh_after_stripe, request.app.state.db, request.app.state.settings, client,
                user.account_id,
            )
        except Exception as exc:
            log.info("billing refresh after Stripe skipped account=%s: %s", user.account_id, type(exc).__name__)
    with request.app.state.db.read() as conn:
        customer = conn.execute(
            "SELECT 1 FROM stripe_customers WHERE account_id = ? AND deleted_at IS NULL", (user.account_id,)
        ).fetchone()
    plans = plans_context(request.app.state.settings)
    choose = request.query_params.get("choose")
    chosen = next((p for p in plans if p["code"] == choose and p["amount_cents"] > 0), None)
    return await render(
        request,
        "account/billing.html",
        {
            "state": await _state(request, user),
            "plans": plans,
            "invoices_url_available": customer is not None,
            "returned_from": returned,
            "chosen_plan": chosen,
        },
    )


# --------------------------------------------------------------------------- devices


@router.get("/account/devices")
async def devices_page(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    _, _, account = await resolve_async(request)
    with request.app.state.db.read() as conn:
        devices = device_tokens.list_active(conn, user.account_id, account.auth_epoch, clock.now())
    views = [
        {
            "id": device.id,
            "label": device.label,
            "extension_id": device.extension_id,
            "extension_version": device.extension_version,
            "created_at": device.created_at,
            "last_used_at": device.last_used_at,
            "idle_expires_at": device.idle_expires_at,
            "expires_at": device.abs_expires_at,
        }
        for device in devices
    ]
    return await render(request, "account/devices.html", {"devices": views})


@router.post("/account/devices/{device_id}/revoke", dependencies=[Depends(verify_csrf)])
async def revoke_device(request: Request, device_id: str, user: CurrentUser = Depends(require_user)) -> Response:
    with request.app.state.db.read() as conn:
        row = conn.execute(
            "SELECT label FROM devices WHERE id = ? AND account_id = ?", (device_id[:64], user.account_id)
        ).fetchone()
    label = row["label"] if row is not None else "That browser"
    revoked = await request.app.state.db.write_async(
        lambda conn: device_tokens.revoke(conn, device_id[:64], clock.now(), "revoked_by_user", account_id=user.account_id)
    )
    if revoked:
        log.info("device revoked from website account=%s device=%s", user.account_id, device_id[:64])
    response = _redirect("/account/devices")
    flash.set_flash(
        response,
        request.app.state.settings,
        "success" if revoked else "info",
        f"{label} is disconnected. It can no longer draft with your account." if revoked
        else "That browser was already disconnected.",
    )
    return response


# --------------------------------------------------------------------------- security


def sign_in_context(request: Request, user: CurrentUser, back: str) -> dict[str, Any]:
    """How this account signs in, for the pages that guard a change with the password."""
    settings = request.app.state.settings
    _, session, account = resolve(request)
    identity = service.google_identity(request.app.state.db, user.account_id)
    return {
        "email": user.email,  # for the password manager's username field when a password is added
        "has_password": bool(account is not None and account.has_password),
        "google_available": settings.google_sign_in_enabled,
        "google": {"email_masked": accounts.mask_email(identity.email), "connected_at": identity.created_at} if identity else None,
        "recent": service.signed_in_recently(session),
        "reauth_url": "/auth/google?intent=reauth&next=" + back,
    }


async def _security_page(request: Request, user: CurrentUser, errors: dict[str, str], status_code: int = 200) -> Response:
    with request.app.state.db.read() as conn:
        active = sessions.list_active(conn, user.account_id, clock.now())
    views = [
        {
            "id": session.id_hash[:12],
            "current": session.id_hash == user.session_id_hash,
            "created_at": session.created_at,
            "last_seen_at": session.last_seen_at,
            "user_agent_summary": user_agent_summary(session.user_agent),
            "ip_prefix": session.ip_prefix,
        }
        for session in active
    ]
    return await render(
        request,
        "account/security.html",
        {"sessions": views, "errors": errors, "sign_in": sign_in_context(request, user, "/account/security")},
        status_code=status_code,
    )


@router.get("/account/security")
async def security_page(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    return await _security_page(request, user, {})


@router.post("/account/security/password", dependencies=[Depends(verify_csrf)])
async def change_password(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    new_password = form_str(form, "new_password", PASSWORD_FIELD_MAX)
    if "new_password_confirm" in form and form_str(form, "new_password_confirm", PASSWORD_FIELD_MAX) != new_password:
        return await _security_page(request, user, {"new_password_confirm": "The new passwords don't match."}, 400)
    _, session, _ = await resolve_async(request)
    result = await service.change_password(
        request.app.state.db,
        settings,
        account_id=user.account_id,
        session=session,
        current_password=form_str(form, "current_password", PASSWORD_FIELD_MAX),
        new_password=new_password,
        ip_prefix=ratelimit.request_ip_prefix(request, settings),
        user_agent=request.headers.get("user-agent"),
    )
    if result.errors or result.session_id is None or result.session is None:
        return await _security_page(request, user, result.errors or {"password": "Your password was not changed."}, 400)
    forget(request)
    response = _redirect("/account/security")
    sessions.set_cookie(response, settings, result.session_id, result.session, clock.now())
    if result.added:
        flash.set_flash(response, settings, "success", "Password added. You can now sign in with your email and password too.")
    else:
        flash.set_flash(response, settings, "success", "Your password changed. Other browsers and extensions were signed out.")
    return response


@router.post("/account/security/email", dependencies=[Depends(verify_csrf)])
async def change_email(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    new_email = form_str(form, "new_email", 320)
    _, session, _ = await resolve_async(request)
    errors = await service.request_email_change(
        request.app.state.db,
        settings,
        account_id=user.account_id,
        password=form_str(form, "email_password", PASSWORD_FIELD_MAX),
        new_email_raw=new_email,
        session=session,
    )
    if errors:
        return await _security_page(request, user, errors, 400)
    response = _redirect("/account/security")
    flash.set_flash(
        response,
        settings,
        "info",
        f"Check {accounts.mask_email(new_email.strip())} for a confirmation link. Your email stays the same until you confirm.",
    )
    return response


@router.post("/account/security/reset-link", dependencies=[Depends(verify_csrf)])
async def email_reset_link(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    """"Forgot your current password?" while signed in: the same reset link, sent to this account."""
    settings = request.app.state.settings
    result = await service.request_password_reset(
        request.app.state.db, settings, email_raw=user.email, ip_prefix=ratelimit.request_ip_prefix(request, settings)
    )
    response = _redirect("/account/security")
    if result.errors:
        flash.set_flash(response, settings, "warning", "We sent a link a moment ago. Try again in a few minutes.")
    else:
        flash.set_flash(response, settings, "success", f"We sent a reset link to {user.email}. It works for 60 minutes.")
    return response


@router.post("/account/security/google/disconnect", dependencies=[Depends(verify_csrf)])
async def disconnect_google(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    errors = await service.unlink_google(
        request.app.state.db, settings, account_id=user.account_id,
        password=form_str(form, "google_password", PASSWORD_FIELD_MAX),
    )
    if errors:
        return await _security_page(request, user, errors, 400)
    response = _redirect("/account/security")
    flash.set_flash(response, settings, "success", "Google is disconnected. Sign in with your email and password from now on.")
    return response


@router.post("/account/security/sessions", dependencies=[Depends(verify_csrf)])
async def sign_out_other_sessions(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    count = await service.sign_out_other_sessions(request.app.state.db, user.account_id, user.session_id_hash)
    response = _redirect("/account/security")
    if count == 0:
        text = "No other browsers were signed in."
    elif count == 1:
        text = "Signed out of 1 other browser."
    else:
        text = f"Signed out of {count} other browsers."
    flash.set_flash(response, request.app.state.settings, "success", text)
    return response


@router.post("/account/verify/resend", dependencies=[Depends(verify_csrf)])
async def resend_verification(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    settings = request.app.state.settings
    outcome = await service.resend_verification(request.app.state.db, settings, user.account_id)
    if outcome == "already_verified":
        response = _redirect("/account")
        flash.set_flash(response, settings, "info", "Your email address is already confirmed.")
        return response
    # Either way, the page with the code field: that is where the next step happens.
    response = _redirect("/verify-email/sent")
    if outcome == "sent":
        flash.set_flash(response, settings, "success", f"We sent a new email to {user.email}. It can take a minute to arrive.")
    else:
        flash.set_flash(response, settings, "warning", "We sent an email a moment ago. You can send another in a minute.")
    return response


# --------------------------------------------------------------------------- data


async def _plan_summary(request: Request, user: CurrentUser) -> dict[str, Any] | None:
    """What the deletion warning needs to know about the plan: nothing, for Free."""
    try:
        state = await account_state(request, user.account_id)
    except Exception:  # the page still works without it; the warning then covers every case
        return None
    plan = state.get("plan") or {}
    paid = plan.get("code") not in (None, "free") and plan.get("status") in ("active", "past_due")
    return {"name": plan.get("name"), "is_paid": bool(paid), "period_end": plan.get("period_end"),
            "cancel_at": plan.get("cancel_at")}


@router.get("/account/data")
async def data_page(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    return await render(request, "account/data.html", {
        "errors": {}, "sign_in": sign_in_context(request, user, "/account/data"), "plan": await _plan_summary(request, user),
    })


async def _data_error(request: Request, errors: dict[str, str], status_code: int, retry_after_s: int | None = None) -> Response:
    headers = {"Retry-After": str(retry_after_s)} if retry_after_s else None
    user = (await resolve_async(request))[0]
    sign_in = sign_in_context(request, user, "/account/data") if user is not None else {}
    plan = await _plan_summary(request, user) if user is not None else None
    return await render(
        request, "account/data.html", {"errors": errors, "sign_in": sign_in, "plan": plan},
        status_code=status_code, headers=headers,
    )


@router.post("/account/export", dependencies=[Depends(verify_csrf)])
async def export_data(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    pool = getattr(request.app.state, "engine_pool", None)
    if pool is None:
        return await _data_error(
            request, {"export": "Data export is temporarily unavailable. Please try again later."}, 503, UNAVAILABLE_RETRY_S
        )
    limit = await ratelimit.check(request.app.state.db, [(ratelimit.EXPORT_PER_ACCOUNT, ratelimit.account_key(user.account_id))])
    if not limit.allowed:
        return await _data_error(request, {"export": "You exported recently. Try again later."}, 429, limit.retry_after_s)
    try:
        body = await deletion.export_data(pool, user.account_id, user.tenant_seq)
    except Exception as exc:  # engine unavailable, busy or failed: never a partial download
        log.error("export failed account=%s: %s", user.account_id, type(exc).__name__)
        retry = getattr(exc, "retry_after_s", None) or 60
        return await _data_error(
            request, {"export": "We couldn't prepare your export right now. Please try again in a few minutes."}, 503, int(retry)
        )
    try:
        drafting = json.loads(body)
    except ValueError:
        log.error("the engine's export was not valid JSON account=%s", user.account_id)
        return await _data_error(
            request, {"export": "We couldn't prepare your export right now. Please try again in a few minutes."}, 503, 60
        )
    database = request.app.state.db

    def control_records() -> dict[str, Any]:
        with database.read() as conn:
            return export.control_records(conn, user.account_id)

    # The download carries both halves: what the gateway holds, and the engine's drafting data.
    payload = {"exported_at": clock.now(), **await anyio.to_thread.run_sync(control_records), "drafting": drafting}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    filename = f"starling-export-{datetime.fromtimestamp(clock.now(), tz=timezone.utc):%Y%m%d}.json"
    log.info("export downloaded account=%s", user.account_id)
    return Response(
        body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/account/delete", dependencies=[Depends(verify_csrf)])
async def delete_account(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    settings = request.app.state.settings
    db = request.app.state.db
    form = await read_form(request)
    if form_str(form, "delete_confirmation", 32).strip().upper() != "DELETE":
        return await _data_error(request, {"delete_confirmation": "Type DELETE to confirm."}, 400)
    pool = getattr(request.app.state, "engine_pool", None)
    if pool is None:
        return await _data_error(
            request, {"delete": "Account deletion is temporarily unavailable. Please try again later."}, 503, UNAVAILABLE_RETRY_S
        )
    _, session, _ = await resolve_async(request)
    account, problem = await service.confirm_identity(
        db, settings, user.account_id, session, form_str(form, "delete_password", PASSWORD_FIELD_MAX)
    )
    if account is None:
        key = "delete" if problem == service.REAUTH_NEEDED else "delete_password"
        return await _data_error(request, {key: problem or "That is not your current password."}, 400)
    plan = (await _plan_summary(request, user)) or {}
    plan_name = plan.get("name") if plan.get("is_paid") else None
    forget(request)
    response = _redirect("/goodbye")
    sessions.clear_cookie(response, settings)
    goodbye = {"email": user.email, "plan_name": plan_name, "pending": False}
    try:
        await deletion.delete_account(db, settings, pool, user.account_id, plan_name)
    except deletion.DeletionError:
        goodbye["pending"] = True
    flash.set_notice(response, settings, {"goodbye": goodbye}, max_age=600)
    return response


@router.get("/goodbye")
async def goodbye_page(request: Request) -> Response:
    """Right after a deletion: what happened, what was sent, what is left to do (remove the extension)."""
    notice = flash.read_notice(request, request.app.state.settings) or {}
    goodbye = notice.get("goodbye")
    if not isinstance(goodbye, dict):
        return _redirect("/")
    return await render(request, "goodbye.html", {
        "email": goodbye.get("email") if isinstance(goodbye.get("email"), str) else None,
        "plan_name": goodbye.get("plan_name") if isinstance(goodbye.get("plan_name"), str) else None,
        "pending": goodbye.get("pending") is True,
    })
