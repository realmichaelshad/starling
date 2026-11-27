"""/admin owner dashboard. Owner: admin workstream.

Every route depends on `auth.deps.require_admin` (CLI grant, owner email, authenticator and a 15-minute
elevation). Every POST also depends on `verify_csrf`, writes exactly one `admin_audit` row through
`admin.actions`, and answers 303 back to its page with a flash message; form problems are flash
messages too, so the `errors` context stays empty. Pages show metadata, counts, costs and error codes
only. `/admin/totp` and `/admin/elevate` live in `auth.routes`.

| Method and path | Template or form fields |
|---|---|
| GET /admin | admin/overview.html |
| GET /admin/customers?q=&page= | admin/customers.html |
| GET /admin/customers/{id} | admin/customer.html |
| POST /admin/customers/{id}/grant | credits (1 to 10,000), reason |
| POST /admin/customers/{id}/suspend, /unsuspend | suspend_reason |
| POST /admin/customers/{id}/revoke-devices | none |
| POST /admin/customers/{id}/resync-billing | resync_confirm=yes |
| GET /admin/failures | admin/failures.html |
| GET /admin/webhooks?status= | admin/webhooks.html |
| GET /admin/audit?page= | admin/audit.html |
| GET /admin/ai | admin/ai.html |
| POST /admin/ai/test | none |
| POST /admin/ai/pause | pause_reason, pause_message (optional, shown to customers) |
| POST /admin/ai/resume | resume_reason (optional) |
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from ..auth.deps import CurrentUser, require_admin, resolve_async, verify_csrf
from ..config import Settings
from ..security import clock, ratelimit
from ..security.errors import PageError, StarlingRoute
from ..security.http import form_str, read_form
from ..services import client as services_client
from ..web import flash
from ..web.render import render
from . import actions, customers, metrics

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

T = TypeVar("T")

FIELD_MAX = 1000
MAX_ACCOUNT_ID_LENGTH = 64
MAX_PAGE = 100_000
NOT_FOUND = "That account no longer exists."
AI_TEST_HINTS = {
    "services_unreachable": "The services process did not answer; check that it is running and STARLING_SERVICES_URL.",
    "services_not_configured": "STARLING_SERVICES_TOKEN_KEY is missing or too short on the gateway.",
    "services_error": "The services process answered with an error.",
    "not_configured": "AI_API_KEY is not set for the services process.",
    "ai_auth": "The AI provider rejected the API key.",
    "ai_model": "The configured model was not found for this key.",
    "ai_quota": "The AI provider reports no remaining quota or a spend limit.",
    "ai_rate_limited": "The AI provider is rate limiting this key; try again in a minute.",
    "ai_unavailable": "The AI provider did not answer in time; try again later.",
}
SUSPEND_MESSAGES = {
    "done": ("success", "The account is suspended. Its website sessions and connected browsers are signed out."),
    "invalid": ("error", "The account was not suspended. Give a reason of up to 200 characters."),
    "refused_self": ("error", "You can't suspend your own account from the dashboard."),
    "not_active": ("warning", "Only an active account can be suspended."),
    "not_found": ("error", NOT_FOUND),
}


# --------------------------------------------------------------------------- helpers


async def _thread(fn: Callable[..., T], *args: Any) -> T:
    return await anyio.to_thread.run_sync(fn, *args)


def _redirect(request: Request, location: str, level: str, message: str) -> RedirectResponse:
    response = RedirectResponse(location, status_code=303, headers={"Cache-Control": "no-store"})
    flash.set_flash(response, request.app.state.settings, level, message)
    return response


def _answer(request: Request, location: str, result: actions.Result, message: tuple[str, str]) -> RedirectResponse:
    target = "/admin/customers" if result.outcome == "not_found" else location
    return _redirect(request, target, *message)


def _actor(request: Request, user: CurrentUser) -> actions.Actor:
    return actions.Actor(user.account_id, user.email, ratelimit.request_ip_prefix(request, request.app.state.settings))


def _page_number(request: Request) -> int:
    try:
        number = int(request.query_params.get("page", "1"))
    except ValueError:
        return 1
    return min(max(number, 1), MAX_PAGE)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


async def _page(request: Request, name: str, context: dict[str, Any]) -> Response:
    _, session, _ = await resolve_async(request)
    elevated_until = session.admin_elevated_until if session is not None else None
    return await render(request, name, {"admin": {"elevated_until": elevated_until}, **context})


async def _existing_customer(request: Request, account_id: str) -> str:
    """The customer page path of an existing account; a 404 page otherwise."""
    if not account_id or len(account_id) > MAX_ACCOUNT_ID_LENGTH:
        raise PageError(404, "Not found.")

    def exists() -> bool:
        with request.app.state.db.read() as conn:
            return conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is not None

    if not await _thread(exists):
        raise PageError(404, "Not found.")
    return "/admin/customers/" + quote(account_id, safe="")


# --------------------------------------------------------------------------- pages


@router.get("/admin")
async def overview_page(request: Request, user: CurrentUser = Depends(require_admin)) -> Response:
    state = request.app.state
    data = await _thread(metrics.overview, state.db, state.settings, clock.now())
    return await _page(request, "admin/overview.html", {"metrics": data})


@router.get("/admin/customers")
async def customers_page(request: Request, user: CurrentUser = Depends(require_admin)) -> Response:
    state = request.app.state
    query = request.query_params.get("q", "")
    data = await _thread(customers.search, state.db, state.settings, query, _page_number(request), clock.now())
    return await _page(request, "admin/customers.html", data)


@router.get("/admin/customers/{account_id}")
async def customer_page(request: Request, account_id: str, user: CurrentUser = Depends(require_admin)) -> Response:
    if len(account_id) > MAX_ACCOUNT_ID_LENGTH:
        raise PageError(404, "Not found.")
    state = request.app.state
    data = await _thread(customers.detail, state.db, state.settings, account_id, user.account_id, clock.now())
    if data is None:
        raise PageError(404, "Not found.")
    return await _page(request, "admin/customer.html", {"errors": {}, **data})


@router.get("/admin/failures")
async def failures_page(request: Request, user: CurrentUser = Depends(require_admin)) -> Response:
    data = await _thread(metrics.failures_page, request.app.state.db, clock.now())
    return await _page(request, "admin/failures.html", data)


@router.get("/admin/webhooks")
async def webhooks_page(request: Request, user: CurrentUser = Depends(require_admin)) -> Response:
    status_filter = request.query_params.get("status", "all")
    data = await _thread(metrics.webhooks_page, request.app.state.db, status_filter, clock.now())
    return await _page(request, "admin/webhooks.html", data)


@router.get("/admin/audit")
async def audit_page(request: Request, user: CurrentUser = Depends(require_admin)) -> Response:
    data = await _thread(metrics.audit_page, request.app.state.db, _page_number(request))
    return await _page(request, "admin/audit.html", data)


@router.get("/admin/ai")
async def ai_page(request: Request, user: CurrentUser = Depends(require_admin)) -> Response:
    state = request.app.state
    try:
        status = await services_client.services_status(state.settings)
    except Exception:  # the client never raises by contract; keep the page usable if it does
        log.exception("services status call failed")
        status = {"ok": False, "error": {"code": "services_error"}}
    data = await _thread(metrics.ai_page, state.db, state.settings, status, clock.now())
    return await _page(request, "admin/ai.html", {"errors": {}, "ai": data})


# --------------------------------------------------------------------------- customer actions


@router.post("/admin/customers/{account_id}/grant")
async def grant_credits(
    request: Request, account_id: str, user: CurrentUser = Depends(require_admin), _csrf: None = Depends(verify_csrf)
) -> Response:
    location = await _existing_customer(request, account_id)
    form = await read_form(request)
    state = request.app.state
    result = await _thread(
        actions.grant_credits, state.db, state.settings, _actor(request, user), account_id,
        form_str(form, "credits", FIELD_MAX), form_str(form, "reason", FIELD_MAX), clock.now(),
    )
    return _answer(request, location, result, _grant_message(state.settings, result))


@router.post("/admin/customers/{account_id}/suspend")
async def suspend_customer(
    request: Request, account_id: str, user: CurrentUser = Depends(require_admin), _csrf: None = Depends(verify_csrf)
) -> Response:
    location = await _existing_customer(request, account_id)
    form = await read_form(request)
    result = await _thread(
        actions.suspend_account, request.app.state.db, _actor(request, user), account_id,
        form_str(form, "suspend_reason", FIELD_MAX), clock.now(),
    )
    return _answer(request, location, result, SUSPEND_MESSAGES[result.outcome])


@router.post("/admin/customers/{account_id}/unsuspend")
async def unsuspend_customer(
    request: Request, account_id: str, user: CurrentUser = Depends(require_admin), _csrf: None = Depends(verify_csrf)
) -> Response:
    location = await _existing_customer(request, account_id)
    form = await read_form(request)
    result = await _thread(
        actions.unsuspend_account, request.app.state.db, _actor(request, user), account_id,
        form_str(form, "suspend_reason", FIELD_MAX), clock.now(),
    )
    return _answer(request, location, result, _unsuspend_message(result))


@router.post("/admin/customers/{account_id}/revoke-devices")
async def revoke_customer_devices(
    request: Request, account_id: str, user: CurrentUser = Depends(require_admin), _csrf: None = Depends(verify_csrf)
) -> Response:
    location = await _existing_customer(request, account_id)
    result = await _thread(actions.revoke_devices, request.app.state.db, _actor(request, user), account_id, clock.now())
    return _answer(request, location, result, _revoke_message(result))


@router.post("/admin/customers/{account_id}/resync-billing")
async def resync_customer_billing(
    request: Request, account_id: str, user: CurrentUser = Depends(require_admin), _csrf: None = Depends(verify_csrf)
) -> Response:
    location = await _existing_customer(request, account_id)
    form = await read_form(request)
    state = request.app.state
    client = getattr(state, "stripe_client", None)  # set by billing.lifecycle when Stripe is configured
    confirmed = form_str(form, "resync_confirm", 16) == "yes"
    result = await _thread(
        actions.resync_billing, state.db, state.settings, client, _actor(request, user), account_id, confirmed, clock.now()
    )
    return _answer(request, location, result, _resync_message(state.settings, result))


# --------------------------------------------------------------------------- AI provider actions


@router.post("/admin/ai/test")
async def ai_test(request: Request, user: CurrentUser = Depends(require_admin), _csrf: None = Depends(verify_csrf)) -> Response:
    state = request.app.state
    try:
        check = await services_client.ai_check(state.settings)
    except Exception:  # the client never raises by contract; record a failed test if it does
        log.exception("AI check call failed")
        check = {"ok": False, "error": {"code": "services_error"}}
    now = clock.now()
    summary = actions.summarize_ai_check(check, now)
    result = await _thread(actions.store_ai_test, state.db, _actor(request, user), summary, now)
    if result.outcome == "passed":
        return _redirect(request, "/admin/ai", "success", "The AI connection test passed.")
    code = result.details.get("error_code") or "check_failed"
    hint = AI_TEST_HINTS.get(code, "The connection test section shows what was checked.")
    return _redirect(request, "/admin/ai", "error", f"The AI connection test failed ({code}). {hint}")


@router.post("/admin/ai/pause")
async def ai_pause(request: Request, user: CurrentUser = Depends(require_admin), _csrf: None = Depends(verify_csrf)) -> Response:
    form = await read_form(request)
    result = await _thread(
        actions.pause_ai, request.app.state.db, _actor(request, user),
        form_str(form, "pause_reason", FIELD_MAX), form_str(form, "pause_message", FIELD_MAX), clock.now(),
    )
    if result.done:
        return _redirect(request, "/admin/ai", "success", "Drafting is paused for all customers until you resume it.")
    return _redirect(
        request, "/admin/ai", "error",
        "Drafting was not paused. Give a reason of up to 200 characters and a customer message of at most 200.",
    )


@router.post("/admin/ai/resume")
async def ai_resume(request: Request, user: CurrentUser = Depends(require_admin), _csrf: None = Depends(verify_csrf)) -> Response:
    form = await read_form(request)
    state = request.app.state
    now = clock.now()
    result = await _thread(actions.resume_ai, state.db, _actor(request, user), form_str(form, "resume_reason", FIELD_MAX), now)
    if result.outcome == "invalid":
        return _redirect(request, "/admin/ai", "error", "Drafting was not resumed. Keep the note to 200 characters.")
    if result.outcome == "not_paused":
        return _redirect(request, "/admin/ai", "info", "Drafting was not paused by the owner.")
    service = await _thread(metrics.current_service_status, state.db, state.settings, now)
    if service["status"] == "paused":
        return _redirect(
            request, "/admin/ai", "warning", "Your pause is lifted, but drafting stays paused because an AI budget is used up."
        )
    return _redirect(request, "/admin/ai", "success", "Drafting is running again.")


# --------------------------------------------------------------------------- flash messages


def _grant_message(settings: Settings, result: actions.Result) -> tuple[str, str]:
    if result.done:
        credits = result.details["credits"]
        unit = settings.plans.unit_name if credits == 1 else settings.plans.unit_label
        return "success", f"Added {credits:,} {unit} to this period; {result.details['remaining']:,} remaining."
    messages = {
        "invalid": "No credits were added. Enter a whole number from 1 to 10,000 and a reason of up to 200 characters.",
        "not_allowed": "No credits were added: this account is being deleted.",
        "not_found": NOT_FOUND,
    }
    return "error", messages.get(result.outcome) or f"No credits were added: {result.details.get('message') or 'the grant failed.'}"


def _unsuspend_message(result: actions.Result) -> tuple[str, str]:
    if result.done:
        if result.details.get("dispute_open"):
            return "warning", "The suspension is lifted, but an open payment dispute still blocks drafting until it closes."
        return "success", "The suspension is lifted. The customer can sign in and connect the extension again."
    messages = {
        "invalid": ("error", "The suspension was not lifted. Give a reason of up to 200 characters."),
        "not_suspended": ("warning", "This account is not suspended."),
    }
    return messages.get(result.outcome, ("error", NOT_FOUND))


def _revoke_message(result: actions.Result) -> tuple[str, str]:
    if result.done:
        count = result.details.get("revoked", 0)
        if not count:
            return "info", "No browsers were connected to this account."
        return "success", f"Disconnected {_plural(count, 'browser')}. The customer must connect the extension again."
    if result.outcome == "refused_self":
        return "error", "You can't disconnect your own browsers from the dashboard. Use your account's Devices page."
    return "error", NOT_FOUND


def _resync_message(settings: Settings, result: actions.Result) -> tuple[str, str]:
    details = result.details
    if result.done:
        message = (
            f"Billing resynced from Stripe: {_plural(details.get('subscriptions', 0), 'subscription')} checked, "
            f"{details.get('mirror_changed', 0):,} updated, {_plural(details.get('periods_granted', 0), 'paid period')} "
            f"granted, {_plural(details.get('canceled_duplicates', 0), 'duplicate subscription')} canceled."
        )
        if details.get("entitlement_changed"):
            message += " The plan or allowance changed."
        return "success", message
    if result.outcome == "not_configured":
        if not settings.stripe_secret_key:
            return "error", "Stripe is not configured on this server (STRIPE_SECRET_KEY is empty), so nothing was resynced."
        return "error", "Billing is not running on this server right now, so nothing was resynced."
    messages = {
        "unconfirmed": ("warning", "Nothing was resynced. Tick the box to confirm that duplicate subscriptions may be canceled."),
        "no_customer": ("warning", "This account has no Stripe customer, so there is nothing to resync."),
        "stripe_error": ("error", f"Stripe did not answer ({details.get('message') or 'no details'}). Try again in a few minutes."),
        "not_found": ("error", NOT_FOUND),
    }
    return messages.get(result.outcome, ("error", "The resync failed. The server log has the details."))
