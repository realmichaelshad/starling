"""Public marketing, docs and legal pages. Owner: server core workstream.

| Route | Template | Extra context |
|---|---|---|
| `GET /` | `index.html` | `plans` |
| `GET /pricing` | `pricing.html` | `plans`, `unit`, `state` (None when signed out) |
| `GET /docs/<page>` | `docs/<page>.html` (404 when absent) | - |
| `GET /privacy`, `/terms`, `/refunds`, `/subprocessors`, `/acceptable-use`, `/contact` | `legal/<name>.html` | `subprocessors` |
| `GET /extension/removed` | `extension_removed.html` | - |
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from ..auth.deps import current_user_async
from ..security.errors import PageError, StarlingRoute
from .render import (
    DOC_PAGE_RE,
    LEGAL_PAGES,
    account_state,
    plans_context,
    render,
    subprocessors_context,
    template_exists,
    unit_context,
)

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)


@router.get("/")
async def index(request: Request) -> Response:
    settings = request.app.state.settings
    return await render(request, "index.html", {"plans": plans_context(settings), "unit": unit_context(settings)})


@router.get("/pricing")
async def pricing(request: Request) -> Response:
    settings = request.app.state.settings
    user = await current_user_async(request)
    state = None
    if user is not None:
        try:
            state = await account_state(request, user.account_id)
        except Exception as exc:  # the public price list must render even if usage data is unavailable
            log.error("pricing: account state unavailable: %s", type(exc).__name__)
    return await render(request, "pricing.html", {
        "plans": plans_context(settings), "unit": unit_context(settings), "state": state,
        # Back from Stripe Checkout without paying (billing.service.create_checkout cancel_url).
        "checkout_cancelled": request.query_params.get("checkout") == "cancelled",
    })


@router.get("/docs")
@router.get("/docs/")
async def docs_home(request: Request) -> Response:
    """The docs have no index page of their own: send people to the first guide."""
    return RedirectResponse("/docs/getting-started", status_code=303)


@router.get("/docs/{page}")
async def docs_page(request: Request, page: str) -> Response:
    name = f"docs/{page}.html"
    if len(page) > 64 or not DOC_PAGE_RE.match(page) or not template_exists(request, name):
        raise PageError(404, "This page does not exist.")
    return await render(request, name, {})


def _legal_page(name: str) -> Callable[[Request], Awaitable[Response]]:
    async def page(request: Request) -> Response:
        return await render(request, f"legal/{name}.html", {"subprocessors": subprocessors_context(request.app.state.settings)})

    page.__name__ = "legal_" + name.replace("-", "_")
    return page


for _name in LEGAL_PAGES:
    router.add_api_route(f"/{_name}", _legal_page(_name), methods=["GET"], name=f"legal_{_name.replace('-', '_')}")


@router.get("/extension/removed")
async def extension_removed(request: Request) -> Response:
    return await render(request, "extension_removed.html", {})
