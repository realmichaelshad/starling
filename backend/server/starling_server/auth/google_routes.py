"""Sign in with Google: the pages of the round trip. Owner: server core workstream.

The protocol is in `auth/google.py` and the account rules in `auth/service.py`; this module only
decides where the browser goes next and what it is told on the way.

- `GET /auth/google?intent=login|signup|reauth&next=...`: off to Google (sets the start cookie).
  `reauth` is for a signed-in account that has no password and wants to make a sensitive change.
- `POST /auth/google/link`: the same, to connect Google to the signed-in account (Security page).
- `GET /auth/google/callback`: back from Google.
- `GET/POST /auth/google/new`: "Create your Starling account?", when someone who came to SIGN IN
  picked a Google account that has no Starling account yet.

Every route is a 404 while Google sign-in is not configured (`Settings.google_sign_in_enabled`).
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from ..config import Settings
from ..security import clock, ratelimit
from ..security.errors import PageError, StarlingRoute
from ..security.http import safe_next
from ..web import flash
from ..web.render import render
from . import accounts, google, service, sessions
from .deps import CurrentUser, forget, require_user, resolve_async, verify_csrf

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

EXPIRED = "That Google sign-in timed out or was opened in another browser. Please try again."
NOT_CONFIRMED = "Google did not confirm the sign-in. Please try again."
CANCELLED = "Google sign-in was cancelled. You can try again, or use your email address."
WELCOME = "Welcome to Starling. Your account is ready."


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if not settings.google_sign_in_enabled:
        raise PageError(404, "Not found.")
    return settings


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303, headers={"Cache-Control": "no-store"})


def _ip_prefix(request: Request) -> str:
    return ratelimit.request_ip_prefix(request, request.app.state.settings)


def _to_google(settings: Settings, url: str, cookie: str) -> RedirectResponse:
    response = _redirect(url)
    flash.set_cookie(response, settings, google.start_cookie_name(settings), cookie, google.START_TTL_S)
    return response


def _login_path(next_path: str) -> str:
    return "/login" if next_path == "/account" else "/login?next=" + quote(next_path, safe="")


def _back(start: google.Start) -> str:
    """Where a failed or cancelled attempt returns: the page the person started from, still going where they were."""
    if start.intent == "signup":
        return "/signup" if start.next == "/account" else "/signup?next=" + quote(start.next, safe="")
    if start.intent == "login":
        return _login_path(start.next)
    return start.next


def _done(response: Response, settings: Settings, level: str | None = None, text: str = "") -> Response:
    flash.clear_cookie(response, settings, google.start_cookie_name(settings))
    if level and text:
        flash.set_flash(response, settings, level, text)
    return response


def _signed_in(response: Response, settings: Settings, result: service.GoogleResult) -> Response:
    assert result.session_id is not None and result.session is not None
    sessions.set_cookie(response, settings, result.session_id, result.session, clock.now())
    flash.clear_cookie(response, settings, google.pending_cookie_name(settings))
    if result.created:
        flash.set_flash(response, settings, "success", WELCOME)
    elif result.claimed:
        flash.set_flash(
            response, settings, "success",
            "Signed in with Google, and your email address is confirmed. The account's old password was "
            "removed; you can set a new one under Security.",
        )
    elif result.linked:
        flash.set_flash(response, settings, "success", "Signed in. Google is now connected, so next time it is one click.")
    return response


# --------------------------------------------------------------------------- off to Google


@router.get("/auth/google")
async def google_start(request: Request) -> Response:
    settings = _settings(request)
    intent = request.query_params.get("intent", "login")
    next_path = safe_next(request.query_params.get("next"), "/account")
    user, _, _ = await resolve_async(request)
    if intent == "reauth":
        if user is None:
            return _redirect(_login_path(next_path))
        identity = service.google_identity(request.app.state.db, user.account_id)
        if identity is None:
            return _redirect(next_path)
        url, cookie = google.begin(
            settings, intent="reauth", next_path=next_path, account_id=user.account_id, login_hint=identity.email
        )
        return _to_google(settings, url, cookie)
    if user is not None:
        return _redirect(next_path)  # already signed in: nothing to do
    url, cookie = google.begin(settings, intent="signup" if intent == "signup" else "login", next_path=next_path)
    return _to_google(settings, url, cookie)


@router.post("/auth/google/link", dependencies=[Depends(verify_csrf)])
async def google_link_start(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    settings = _settings(request)
    url, cookie = google.begin(
        settings, intent="link", next_path="/account/security", account_id=user.account_id, login_hint=user.email
    )
    return _to_google(settings, url, cookie)


# --------------------------------------------------------------------------- back from Google


@router.get("/auth/google/callback")
async def google_callback(request: Request) -> Response:
    settings = _settings(request)
    db = request.app.state.db
    params = request.query_params
    start = google.read_start(settings, request.cookies.get(google.start_cookie_name(settings)), params.get("state"))
    if start is None:
        return _done(_redirect("/login"), settings, "warning", EXPIRED)
    if params.get("error"):
        cancelled = params.get("error") == "access_denied"
        log.info("google sign-in not completed: %s", "cancelled" if cancelled else "provider error")
        return _done(_redirect(_back(start)), settings, "info" if cancelled else "warning", CANCELLED if cancelled else NOT_CONFIRMED)
    limit = await ratelimit.check(db, [(ratelimit.GOOGLE_CALLBACK_PER_IP, ratelimit.ip_key(_ip_prefix(request)))])
    if not limit.allowed:
        return _done(_redirect(_back(start)), settings, "warning", "Too many sign-in attempts from your network. Try again in a few minutes.")
    try:
        person = await google.finish(settings, start, params.get("code"))
    except google.GoogleError as exc:
        log.warning("google sign-in refused: %s", exc.reason)
        return _done(_redirect(_back(start)), settings, "warning", NOT_CONFIRMED)

    user, session, _ = await resolve_async(request)
    if start.intent in ("link", "reauth"):
        if user is None or session is None or user.account_id != start.account_id:
            return _done(_redirect(_login_path(start.next)), settings, "warning", "Sign in again, then try once more.")
        if start.intent == "link":
            error = await service.link_google(db, settings, account_id=user.account_id, session=session, user=person)
            if error:
                return _done(_redirect(start.next), settings, "warning", error)
            return _done(_redirect(start.next), settings, "success", "Google is connected. You can now sign in with one click.")
        rotated = await service.reauthenticate_with_google(
            db, settings, account_id=user.account_id, session=session, user=person,
            ip_prefix=_ip_prefix(request), user_agent=request.headers.get("user-agent"),
        )
        if isinstance(rotated, str):
            return _done(_redirect(start.next), settings, "warning", rotated)
        forget(request)
        response = _redirect(start.next)
        sessions.set_cookie(response, settings, rotated[0], rotated[1], clock.now())
        return _done(response, settings, "success", "Thank you, that is confirmed. You can make the change now.")

    result = await service.google_sign_in(
        db, settings, user=person, ip_prefix=_ip_prefix(request), user_agent=request.headers.get("user-agent"),
        old_session_id=request.cookies.get(settings.session_cookie_name), create_now=start.intent == "signup",
    )
    if result.status == "signed_in":
        forget(request)
        return _done(_signed_in(_redirect(start.next), settings, result), settings)
    if result.status in ("confirm_new", "password_to_link"):
        kind = "new" if result.status == "confirm_new" else "link"
        response = _redirect("/auth/google/new" if kind == "new" else _login_path(start.next))
        flash.set_cookie(
            response, settings, google.pending_cookie_name(settings),
            google.seal_pending(settings, person, kind, start.next), google.PENDING_TTL_S,
        )
        return _done(response, settings)
    return _done(_redirect(_back(start)), settings, "warning", result.message or NOT_CONFIRMED)


# --------------------------------------------------------------------------- a new account, confirmed


def _pending_new(request: Request, settings: Settings) -> google.Pending | None:
    return google.read_pending(settings, request.cookies.get(google.pending_cookie_name(settings)), "new")


@router.get("/auth/google/new")
async def google_new_page(request: Request) -> Response:
    settings = _settings(request)
    if (await resolve_async(request))[0] is not None:
        return _redirect("/account")
    pending = _pending_new(request, settings)
    if pending is None:
        response = _redirect("/signup")
        flash.set_flash(response, settings, "info", EXPIRED)
        return response
    return await render(request, "auth/google_new.html", {"email": pending.user.email, "next": pending.next, "errors": {}})


@router.post("/auth/google/new", dependencies=[Depends(verify_csrf)])
async def google_new_submit(request: Request) -> Response:
    settings = _settings(request)
    pending = _pending_new(request, settings)
    if pending is None:
        response = _redirect("/signup")
        flash.set_flash(response, settings, "info", EXPIRED)
        return response
    result = await service.create_google_account(
        request.app.state.db, settings, user=pending.user, ip_prefix=_ip_prefix(request),
        user_agent=request.headers.get("user-agent"), old_session_id=request.cookies.get(settings.session_cookie_name),
    )
    if result.status == "signed_in":
        forget(request)
        return _signed_in(_redirect(pending.next), settings, result)
    if result.status == "password_to_link":
        # Another tab made an account on this address meanwhile, and Google can't vouch for it alone.
        response = _redirect(_login_path(pending.next))
        flash.set_cookie(
            response, settings, google.pending_cookie_name(settings),
            google.seal_pending(settings, pending.user, "link", pending.next), google.PENDING_TTL_S,
        )
        return response
    return await render(
        request,
        "auth/google_new.html",
        {"email": pending.user.email, "next": pending.next, "errors": {"form": result.message or NOT_CONFIRMED}},
        status_code=429 if result.retry_after_s else 400,
        headers={"Retry-After": str(result.retry_after_s)} if result.retry_after_s else None,
    )


# --------------------------------------------------------------------------- used by the password sign-in


def pending_link(request: Request, settings: Settings) -> google.Pending | None:
    """A Google account waiting for this person's password (`auth.routes`)."""
    if not settings.google_sign_in_enabled:
        return None
    return google.read_pending(settings, request.cookies.get(google.pending_cookie_name(settings)), "link")


def pending_link_context(pending: google.Pending | None) -> dict[str, str] | None:
    if pending is None:
        return None
    return {"email": pending.user.email, "email_masked": accounts.mask_email(pending.user.email)}
