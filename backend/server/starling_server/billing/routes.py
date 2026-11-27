"""/billing/* actions and /webhooks/stripe. Owner: billing workstream.

* Website actions are POST forms from `account/billing.html` and `pricing.html`: signed-in
  (`auth.deps`), CSRF-checked, answered with 303 redirects. Fields: `plan_code` for
  checkout, upgrade and downgrade; `flow` (`payment_method_update`, `subscription_cancel`
  or empty) for the portal. Checkout also needs a verified email.
* `GET /billing/return` renders `billing/return.html` (`session_status`, `state`) and
  grants nothing.
* `GET /billing/status` returns `{"state": <account state of CONTRACTS 6.2>}` for polling.
* `POST /webhooks/stripe` is unauthenticated and CSRF-exempt; its signature is the check.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ..auth import deps
from ..auth.deps import CurrentUser
from ..security import clock
from ..security.errors import StarlingRoute
from ..security.http import form_str, read_form
from ..web import render as web_render
from ..web import flash
from . import service, webhooks
from .service import BillingError
from .stripe_client import StripeError

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

RETURN_TEMPLATE = "billing/return.html"
NO_STORE = {"Cache-Control": "no-store"}


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303, headers=NO_STORE)


async def _action(request: Request, fn: Callable[..., Any], *args: Any) -> Any:
    client = getattr(request.app.state, "stripe_client", None)
    if client is None:
        raise BillingError("billing_unavailable", "Billing is not available right now.", 503)
    return await run_in_threadpool(fn, request.app.state.db, request.app.state.settings, client, *args, clock.now())


async def _perform(request: Request, error_path: str, fn: Callable[..., Any], *args: Any) -> tuple[Any, Response | None]:
    try:
        return await _action(request, fn, *args), None
    except BillingError as exc:
        response = _redirect(f"{error_path}?billing_error={exc.code}")
        flash.set_flash(response, request.app.state.settings, "error", exc.message)
        return None, response
    except StripeError as exc:
        log.warning("billing action %s failed at Stripe: %s", fn.__name__, type(exc).__name__)
        response = _redirect(f"{error_path}?billing_error=stripe_unavailable")
        flash.set_flash(response, request.app.state.settings, "error",
                        "Billing is not available right now. Try again shortly.")
        return None, response


def _done(request: Request, location: str, message: str) -> Response:
    """Back to the page with a line saying what happened: an action that changes nothing visible
    straight away (a change scheduled for weeks ahead, or one taken back) still gets an answer."""
    response = _redirect(location)
    flash.set_flash(response, request.app.state.settings, "success", message)
    return response


async def _field(request: Request, name: str) -> str:
    return form_str(await read_form(request), name, 64)


@router.post("/billing/checkout")
async def billing_checkout(
    request: Request,
    _csrf: None = Depends(deps.verify_csrf),
    user: CurrentUser = Depends(deps.require_verified_user),
) -> Response:
    plan_code = await _field(request, "plan_code")
    result, failure = await _perform(request, "/pricing", service.create_checkout, user.account_id, plan_code)
    return failure or _redirect(result.url)


@router.post("/billing/portal")
async def billing_portal(
    request: Request,
    _csrf: None = Depends(deps.verify_csrf),
    user: CurrentUser = Depends(deps.require_user),
) -> Response:
    flow = await _field(request, "flow")
    url, failure = await _perform(request, "/account/billing", service.create_portal, user.account_id, flow)
    return failure or _redirect(url)


@router.post("/billing/upgrade")
async def billing_upgrade(
    request: Request,
    _csrf: None = Depends(deps.verify_csrf),
    user: CurrentUser = Depends(deps.require_user),
) -> Response:
    plan_code = await _field(request, "plan_code")
    url, failure = await _perform(request, "/account/billing", service.start_upgrade, user.account_id, plan_code)
    return failure or _redirect(url)


@router.post("/billing/downgrade")
async def billing_downgrade(
    request: Request,
    _csrf: None = Depends(deps.verify_csrf),
    user: CurrentUser = Depends(deps.require_user),
) -> Response:
    plan_code = await _field(request, "plan_code")
    _, failure = await _perform(request, "/account/billing", service.schedule_downgrade, user.account_id, plan_code)
    return failure or _done(
        request, "/account/billing?billing=downgrade_scheduled",
        "Your downgrade is scheduled. You keep your current plan until the end of this period.",
    )


@router.post("/billing/resume")
async def billing_resume(
    request: Request,
    _csrf: None = Depends(deps.verify_csrf),
    user: CurrentUser = Depends(deps.require_user),
) -> Response:
    _, failure = await _perform(request, "/account/billing", service.resume_subscription, user.account_id)
    if failure:
        return failure
    state = await web_render.account_state(request, user.account_id)
    plan = state.get("plan") or {}
    renews = web_render.date_text(plan.get("period_end")) if plan.get("period_end") else None
    message = f"{plan.get('name') or 'Your plan'} is back on." + (f" It renews on {renews}." if renews else "")
    return _done(request, "/account/billing?billing=resumed", message)


@router.post("/billing/downgrade/undo")
async def billing_downgrade_undo(
    request: Request,
    _csrf: None = Depends(deps.verify_csrf),
    user: CurrentUser = Depends(deps.require_user),
) -> Response:
    _, failure = await _perform(request, "/account/billing", service.undo_downgrade, user.account_id)
    return failure or _done(
        request, "/account/billing?billing=downgrade_undone",
        "The scheduled change is cancelled. Your plan stays as it is.",
    )


@router.get("/billing/return")
async def billing_return(request: Request, user: CurrentUser = Depends(deps.require_user)) -> Response:
    db, settings = request.app.state.db, request.app.state.settings
    now = clock.now()
    session_status = await run_in_threadpool(
        service.return_session_status, db, user.account_id, request.query_params.get("session_id"), now
    )
    state = (await run_in_threadpool(service.billing_status, db, settings, user.account_id, now))["state"]
    headers = {"Referrer-Policy": "no-referrer"}
    context = {"session_status": session_status, "state": state}
    if web_render.template_exists(request, RETURN_TEMPLATE):
        return await web_render.render(request, RETURN_TEMPLATE, context, headers=headers)
    return _fallback_return_page(session_status, state, headers)


def _fallback_return_page(session_status: str, state: dict[str, Any], headers: dict[str, str]) -> Response:
    """Used only when the website templates are not installed."""
    plan = state.get("plan") or {}
    waiting = session_status in ("open", "pending") or (session_status == "complete" and plan.get("code") == "free")
    if waiting:
        message = "We are waiting for Stripe to confirm your subscription."
    elif session_status == "complete":
        message = f"You are on {plan.get('name')}."
    elif session_status == "expired":
        message = "The checkout session ended before payment was completed. You have not been charged."
    else:
        message = "We could not find that checkout."
    refresh = '<meta http-equiv="refresh" content="5">' if waiting else ""
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>Your subscription</title>{refresh}</head><body><h1>Your subscription</h1>"
        f'<p id="billing-status">{escape(message)}</p><p><a href="/account/billing">Go to Billing</a></p></body></html>'
    )
    return HTMLResponse(body, headers={**NO_STORE, **headers})


@router.get("/billing/status")
async def billing_status(request: Request, user: CurrentUser = Depends(deps.require_user)) -> Response:
    body = await run_in_threadpool(
        service.billing_status, request.app.state.db, request.app.state.settings, user.account_id, clock.now()
    )
    return JSONResponse(body, headers=NO_STORE)


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> Response:
    too_large = JSONResponse(
        {"error": {"code": "payload_too_large", "message": "Webhook payload is too large."}}, status_code=413, headers=NO_STORE
    )
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > webhooks.MAX_BODY_BYTES):
        return too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > webhooks.MAX_BODY_BYTES:
            return too_large
    result = await webhooks.intake_async(
        request.app.state.db, request.app.state.settings, bytes(body), request.headers.get("stripe-signature"), clock.now()
    )
    if result.enqueue and result.event_id:
        queue = getattr(request.app.state, "billing_queue", None)
        if queue is not None:
            queue.enqueue(result.event_id)
    return JSONResponse(result.body, status_code=result.status_code, headers=NO_STORE)
