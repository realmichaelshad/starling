"""Render website templates with the common context of CONTRACTS section 10.

Every page receives `site`, `user` (None or {"email", "email_masked", "is_admin"}), `csrf_token`, `flash`
and `request_path`. `REQUIRED_CONTEXT` lists the extra keys each template is guaranteed to receive;
rendering without one of them is a programming error and raises.

Context shapes produced here (the templates can rely on them):

- `plans`: list of {"code", "name", "monthly_credits", "amount_cents", "currency", "interval",
  "is_paid", "price_label", "rate_per_minute", "rate_per_day"} for public plans, in file order.
- `unit`: {"name", "label"}.
- `subprocessors`: list of {"name", "purpose"}.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import anyio
from fastapi import Request, Response
from fastapi.responses import PlainTextResponse

from ..auth import csrf
from ..auth.accounts import mask_email
from ..auth.deps import CurrentUser, resolve_async
from ..config import Plan, Settings
from ..security import clock
from ..security.errors import PageError
from ..usage import service as usage_service
from . import flash

log = logging.getLogger(__name__)

SITE_NAME = "Starling"
LEGAL_PAGES = ("privacy", "terms", "refunds", "subprocessors", "acceptable-use", "contact")
DOC_PAGE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


async def account_state(request: Request, account_id: str) -> dict[str, Any]:
    """The shared account state of CONTRACTS 6.2 (`usage.service.account_state`), off the event loop."""
    return await anyio.to_thread.run_sync(
        usage_service.account_state, request.app.state.db, request.app.state.settings, account_id, clock.now()
    )

REQUIRED_CONTEXT: dict[str, tuple[str, ...]] = {
    "index.html": ("plans", "unit"),
    "errors/404.html": (),
    "errors/500.html": (),
    "errors/problem.html": ("status", "title", "message", "action", "secondary"),
    "pricing.html": ("plans", "unit", "state"),
    "auth/signup.html": ("errors", "form", "next"),
    "auth/login.html": ("errors", "form", "next", "google_link"),
    "auth/verify_sent.html": ("email_masked", "email", "code_form", "confirmed", "mail_link", "errors"),
    "auth/verify.html": ("token_valid", "token", "already_confirmed"),
    "auth/forgot.html": ("sent", "errors", "form", "mail_link"),
    "auth/reset.html": ("token_valid", "token", "errors"),
    "auth/google_new.html": ("email", "next", "errors"),
    "account/overview.html": ("state", "devices_count"),
    "account/billing.html": ("state", "plans", "invoices_url_available", "returned_from", "chosen_plan"),
    "account/devices.html": ("devices",),
    "account/data.html": ("errors", "sign_in", "plan"),
    "goodbye.html": ("email", "plan_name", "pending"),
    "account/security.html": ("sessions", "errors", "sign_in"),
    "connect/approve.html": ("extension_id", "device_label", "connect_request_id", "connect_state"),
    "connect/error.html": ("extension_id", "device_label", "error"),
    "extension_removed.html": (),
    "write/index.html": ("state", "consent", "disclosure", "prefill", "next_path"),
    "write/voice.html": ("state", "consent", "disclosure", "samples", "limits", "errors", "form", "unavailable",
                         "next_path"),
    "offline.html": (),
    "admin/totp.html": ("secret", "otpauth_uri", "errors"),
    "admin/elevate.html": ("errors", "next"),
    "admin/overview.html": ("metrics",),
    "admin/customers.html": ("query", "page", "customers"),
    "admin/customer.html": ("errors", "customer", "state", "cost", "recent_errors", "ledger"),
    "admin/failures.html": ("window_label", "totals", "summary", "failures"),
    "admin/webhooks.html": ("status_filter", "summary", "events"),
    "admin/audit.html": ("page", "entries"),
    "admin/ai.html": ("errors", "ai"),
}
REQUIRED_CONTEXT.update({f"legal/{name}.html": ("subprocessors",) for name in LEGAL_PAGES})

# Rendered by other workstreams; `/readyz` checks that they are installed too.
OTHER_TEMPLATES = ("billing/return.html",)


def required_templates() -> list[str]:
    return sorted(set(REQUIRED_CONTEXT) | set(OTHER_TEMPLATES))


def date_text(ts: int | None) -> str:
    """A day as the site writes it in UTC ("October 20, 2026"), for server-written messages."""
    from datetime import datetime, timezone

    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%B %d, %Y").replace(" 0", " ")


def site_context(settings: Settings) -> dict[str, Any]:
    site: dict[str, Any] = {
        "name": SITE_NAME,
        "public_url": settings.public_url,
        "support_email": settings.support_email,
        "env": settings.env,
        "legal": dict(settings.legal.__dict__),
    }
    install_url = getattr(settings, "extension_install_url", "") or ""
    if install_url:
        site["extension_install_url"] = install_url
    if getattr(settings, "payments_rehearsal", False):
        site["payments_test_mode"] = True  # base.html: a banner on every page, and no indexing
    # "Continue with Google" on the sign-in and sign-up pages, only when it is configured.
    site["google_sign_in"] = bool(getattr(settings, "google_sign_in_enabled", False))
    # The privacy page names web search only while it is on (WEB_SEARCH).
    site["web_search"] = bool(getattr(settings, "web_search_enabled", False))
    # A free-only launch (no paid plan in the plans file): pages that describe payments say so.
    plans = getattr(settings, "plans", None)
    site["paid_plans"] = bool(plans.paid_plans()) if plans is not None else False
    return site


def user_context(user: CurrentUser | None) -> dict[str, Any] | None:
    """The signed-in person. Their own pages show their own address in full; `email_masked` is for
    places a passer-by might see (the home page's button) and for emails and logs."""
    if user is None:
        return None
    return {"email": user.email, "email_masked": mask_email(user.email), "is_admin": user.is_admin}


def _price_label(plan: Plan) -> str:
    if not plan.is_paid or not plan.amount_cents:
        return "Free"
    amount = plan.amount_cents / 100
    text = f"{amount:,.0f}" if plan.amount_cents % 100 == 0 else f"{amount:,.2f}"
    if (plan.currency or "").lower() == "usd":
        return f"${text}"
    return f"{text} {(plan.currency or '').upper()}"


def plan_dict(plan: Plan) -> dict[str, Any]:
    return {
        "code": plan.code,
        "name": plan.name,
        "monthly_credits": plan.monthly_credits,
        "amount_cents": plan.amount_cents or 0,
        "currency": plan.currency,
        "interval": plan.interval,
        "is_paid": plan.is_paid,
        "price_label": _price_label(plan),
        "rate_per_minute": plan.rate_per_minute,
        "rate_per_day": plan.rate_per_day,
    }


def plans_context(settings: Settings) -> list[dict[str, Any]]:
    return [plan_dict(plan) for plan in settings.plans.public_plans()]


def unit_context(settings: Settings) -> dict[str, str]:
    return {"name": settings.plans.unit_name, "label": settings.plans.unit_label}


def subprocessors_context(settings: Settings) -> list[dict[str, str]]:
    legal = settings.legal
    items = [
        {"name": legal.hosting_provider, "purpose": "Hosting and data storage", "data": "All account and drafting data"},
        {"name": legal.email_provider, "purpose": "Transactional email", "data": "Email address and message content"},
        {"name": "Stripe", "purpose": "Payments and billing", "data": "Email address and billing details"},
    ]
    search = bool(getattr(settings, "web_search_enabled", False))
    items.extend(
        {
            "name": name.strip(),
            "purpose": "AI text generation and web search" if search else "AI text generation",
            "data": "Content sent for drafting, and short searches built from public posts or naming the "
                    "subjects an account writes about" if search
            else "Content sent for drafting",
        }
        for name in legal.ai_subprocessors.split(",")
        if name.strip()
    )
    if getattr(settings, "google_sign_in_enabled", False):
        items.append({
            "name": "Google",
            "purpose": "Sign in with Google (only for people who choose it)",
            "data": "Google account ID, email address and whether Google confirmed it",
        })
    return items


async def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    settings: Settings = request.app.state.settings
    templates = request.app.state.templates
    if templates is None:
        log.error("website templates are not installed under STARLING_WEB_DIR")
        raise PageError(503, "The website is not available right now. Please try again later.")
    extra = dict(context or {})
    missing = [key for key in REQUIRED_CONTEXT.get(name, ()) if key not in extra]
    if missing:
        raise RuntimeError(f"template {name} rendered without context keys: {', '.join(missing)}")
    user = (await resolve_async(request))[0]
    new_cookie = None
    if user is not None:
        token = user.csrf_token
    else:
        token, new_cookie = csrf.ensure_presession(request, settings)
    common = {
        "site": site_context(settings),
        "user": user_context(user),
        "csrf_token": token,
        "flash": flash.read_flash(request, settings),
        "request_path": request.url.path,
    }
    merged_headers = {"Cache-Control": "no-store"}
    merged_headers.update(headers or {})
    response = templates.TemplateResponse(request, name, {**extra, **common}, status_code=status_code, headers=merged_headers)
    if new_cookie is not None:
        csrf.set_presession_cookie(response, settings, new_cookie)
    if flash.has_flash_cookie(request, settings):
        flash.clear_flash(response, settings)
    return response


ERROR_TITLES = {404: "Page not found", 500: "Something went wrong"}


def plain_error(status_code: int, message: str | None = None, headers: dict[str, str] | None = None) -> Response:
    """Last-resort error page when the website templates are missing or cannot render."""
    text = message or ERROR_TITLES.get(status_code, "Something went wrong")
    merged = {"Cache-Control": "no-store"}
    merged.update(headers or {})
    return PlainTextResponse(f"{status_code}: {text}\n", status_code=status_code, headers=merged)


async def render_error(
    request: Request, status_code: int, message: str | None = None, headers: dict[str, str] | None = None
) -> Response:
    """`errors/404.html` for 404 and `errors/500.html` for 5xx (with `request_id`), else plain text."""
    name = "errors/404.html" if status_code == 404 else "errors/500.html"
    if template_exists(request, name):
        context: dict[str, Any] = {}
        if status_code >= 500:
            context["request_id"] = getattr(request.state, "request_id", None)
        try:
            return await render(request, name, context, status_code=status_code, headers=headers)
        except Exception as exc:  # never fail while reporting a failure
            log.error("error page %s could not render: %s", name, type(exc).__name__)
    return plain_error(status_code, message, headers)


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """Optional app-level handler: HTML 404 page for browsers, FastAPI's JSON default otherwise."""
    from fastapi.exception_handlers import http_exception_handler as default_handler

    from ..security.http import wants_json

    status_code = getattr(exc, "status_code", 500)
    if status_code == 404 and request.method in ("GET", "HEAD") and not wants_json(request):
        return await render_error(request, 404, headers=getattr(exc, "headers", None))
    return await default_handler(request, exc)  # type: ignore[arg-type]


def template_exists(request: Request, name: str) -> bool:
    templates = request.app.state.templates
    if templates is None:
        return False
    try:
        templates.get_template(name)
    except Exception:  # jinja2.TemplateNotFound and loader errors
        return False
    return True
