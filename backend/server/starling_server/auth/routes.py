"""Sign-up, login, logout, email verification, password reset and owner admin identity pages. Owner: server core workstream.

Every state-changing POST depends on `verify_csrf` (token + origin). Links from email open a
confirmation form: GET never consumes a token.

Owner admin identity pages live here (not in `admin.routes`): `GET/POST /admin/totp` (authenticator
enrollment) and `GET/POST /admin/elevate` (15-minute elevation). `auth.deps.require_admin`
redirects to them.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ..security import clock, ratelimit
from ..security.errors import PageError, StarlingRoute
from ..security.http import form_str, read_form, safe_next
from ..web import flash
from ..web.render import render
from . import accounts, admin, email_tokens, google, google_routes, service, sessions, totp
from .deps import CurrentUser, current_user_async, forget, is_owner_admin, require_user, resolve_async, verify_csrf

log = logging.getLogger(__name__)

router = APIRouter(route_class=StarlingRoute)

EMAIL_FIELD_MAX = 320
PASSWORD_FIELD_MAX = 4096
TOKEN_FIELD_MAX = 256


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303, headers={"Cache-Control": "no-store"})


def _retry_headers(retry_after_s: int | None) -> dict[str, str] | None:
    return {"Retry-After": str(retry_after_s)} if retry_after_s else None


def _ip_prefix(request: Request) -> str:
    return ratelimit.request_ip_prefix(request, request.app.state.settings)


async def _token_form_limit(request: Request) -> None:
    decision = await ratelimit.check(request.app.state.db, [(ratelimit.TOKEN_FORM_PER_IP, ratelimit.ip_key(_ip_prefix(request)))])
    if not decision.allowed:
        raise PageError(429, "Too many attempts. Try again later.", headers=_retry_headers(decision.retry_after_s))


# --------------------------------------------------------------------------- sign-up


@router.get("/signup")
async def signup_page(request: Request) -> Response:
    next_path = safe_next(request.query_params.get("next"), "/account")
    if await current_user_async(request) is not None:
        return _redirect(next_path)
    return await render(request, "auth/signup.html", {"errors": {}, "form": {"email": ""}, "next": next_path})


@router.post("/signup", dependencies=[Depends(verify_csrf)])
async def signup_submit(request: Request) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    email_raw = form_str(form, "email", EMAIL_FIELD_MAX)
    next_path = safe_next(form_str(form, "next", 2048), "/account")
    result = await service.signup(
        request.app.state.db,
        settings,
        email_raw=email_raw,
        password=form_str(form, "password", PASSWORD_FIELD_MAX),
        ip_prefix=_ip_prefix(request),
    )
    if result.errors:
        return await render(
            request,
            "auth/signup.html",
            {"errors": result.errors, "form": {"email": email_raw[: accounts.MAX_EMAIL_LENGTH]}, "next": next_path},
            status_code=429 if result.retry_after_s else 400,
            headers=_retry_headers(result.retry_after_s),
        )
    # Same response whether or not the address already had an account. The notice remembers the
    # address as typed (so a typo can be seen), where the person was going, and the random value that
    # ties the email's code and link to this browser. It lives as long as the code does.
    response = _redirect("/verify-email/sent")
    flash.set_notice(
        response, settings,
        {"email": result.email, "email_masked": accounts.mask_email(result.email), "next": next_path, "b": result.browser},
        max_age=email_tokens.VERIFY_TTL_S,
    )
    return response


async def expired_form(request: Request) -> Response | None:
    """The sign-in, sign-up and reset-request forms, sent back fresh after their token went stale.

    A form left open overnight, or one whose tab outlived a sign-out elsewhere, is the commonest
    way to meet the CSRF check, and nothing about it is the person's fault. Called by
    `security.errors.page_error_response`; None for every other form.
    """
    path = request.url.path
    if path not in ("/login", "/signup", "/forgot-password"):
        return None
    try:
        form = await read_form(request)
    except PageError:
        return None
    email = form_str(form, "email", EMAIL_FIELD_MAX)[: accounts.MAX_EMAIL_LENGTH]
    errors = {"form": "This page was open for a while, so we refreshed it. Please try again."}
    if path == "/login":
        next_path = safe_next(form_str(form, "next", 2048), "/account")
        return await render(request, "auth/login.html", _login_context(request, errors, email, next_path), status_code=403)
    if path == "/signup":
        next_path = safe_next(form_str(form, "next", 2048), "/account")
        return await render(request, "auth/signup.html", {"errors": errors, "form": {"email": email}, "next": next_path},
                            status_code=403)
    return await render(request, "auth/forgot.html",
                        {"sent": False, "errors": errors, "form": {"email": email}, "mail_link": None}, status_code=403)


# --------------------------------------------------------------------------- sign-in and sign-out


def _login_context(request: Request, errors: dict[str, str], email: str, next_path: str) -> dict:
    """The sign-in page, with the Google account waiting for this password when there is one."""
    pending = google_routes.pending_link(request, request.app.state.settings)
    if pending is not None and not email:
        email = pending.user.email
    return {
        "errors": errors,
        "form": {"email": email[: accounts.MAX_EMAIL_LENGTH]},
        "next": next_path,
        "google_link": google_routes.pending_link_context(pending),
    }


@router.get("/login")
async def login_page(request: Request) -> Response:
    next_path = safe_next(request.query_params.get("next"), "/account")
    if await current_user_async(request) is not None:
        return _redirect(next_path)
    return await render(request, "auth/login.html", _login_context(request, {}, "", next_path))


@router.post("/login", dependencies=[Depends(verify_csrf)])
async def login_submit(request: Request) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    email_raw = form_str(form, "email", EMAIL_FIELD_MAX)
    next_path = safe_next(form_str(form, "next", 2048), "/account")
    result = await service.login(
        request.app.state.db,
        settings,
        email_raw=email_raw,
        password=form_str(form, "password", PASSWORD_FIELD_MAX),
        ip_prefix=_ip_prefix(request),
        user_agent=request.headers.get("user-agent"),
        old_session_id=request.cookies.get(settings.session_cookie_name),
    )
    if result.status == "delayed":
        wait = (f"{result.retry_after_s} seconds" if result.retry_after_s < 60
                else f"{service._minutes(result.retry_after_s)} minute{'s' if result.retry_after_s > 60 else ''}")
        return await render(
            request,
            "auth/login.html",
            _login_context(
                request, {"form": f"Too many sign-in attempts. Try again in {wait}, or reset your password."},
                email_raw, next_path,
            ),
            status_code=429,
            headers=_retry_headers(result.retry_after_s),
        )
    if result.status != "ok" or result.session_id is None or result.session is None:
        # One message for every failure, so nobody learns which addresses have accounts. Where Google
        # sign-in is offered it names it: an account made with Google has no password to type.
        message = "That email and password do not match. Try again, or reset your password."
        if settings.google_sign_in_enabled:
            message += " If you signed up with Google, use Continue with Google."
        return await render(
            request, "auth/login.html", _login_context(request, {"form": message}, email_raw, next_path), status_code=400
        )
    forget(request)
    response = _redirect(next_path)
    sessions.set_cookie(response, settings, result.session_id, result.session, clock.now())
    pending = google_routes.pending_link(request, settings)
    if pending is not None:
        flash.clear_cookie(response, settings, google.pending_cookie_name(settings))
        if result.account is not None and await service.link_google_after_password(
            request.app.state.db, settings, account=result.account, user=pending.user
        ):
            flash.set_flash(response, settings, "success", "Google is connected. Next time, Continue with Google signs you straight in.")
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    settings = request.app.state.settings
    _, session, _ = await resolve_async(request)
    if session is not None:
        await verify_csrf(request)  # only a live session has anything to protect
        await service.logout(request.app.state.db, session.id_hash)
    forget(request)
    response = _redirect("/")
    sessions.clear_cookie(response, settings)
    flash.set_flash(response, settings, "success", "You are signed out.")
    return response


# --------------------------------------------------------------------------- email verification


# Webmail a person is likely to have open, by the address's domain. Opening it saves a hunt.
MAIL_LINKS = (
    (("gmail.com", "googlemail.com"), "Open Gmail", "https://mail.google.com/mail/u/0/#inbox"),
    (("outlook.com", "hotmail.com", "live.com", "msn.com"), "Open Outlook", "https://outlook.live.com/mail/0/"),
    (("yahoo.com", "ymail.com"), "Open Yahoo Mail", "https://mail.yahoo.com/"),
    (("icloud.com", "me.com", "mac.com"), "Open iCloud Mail", "https://www.icloud.com/mail"),
    (("proton.me", "protonmail.com", "pm.me"), "Open Proton Mail", "https://mail.proton.me/"),
)

CODE_ERRORS = {
    "format": "Enter the 6-digit code from the email.",
    "wrong": (
        "That code didn't work here. It works only in the browser where you signed up, from the newest "
        "email. Or open the link in the email instead: it works in any browser."
    ),
    "used_up": "That code stopped working after too many tries. Use the link in the email, or send a new email.",
}


def _signup_notice(request: Request) -> dict:
    notice = flash.read_notice(request, request.app.state.settings) or {}
    return notice if isinstance(notice.get("b"), str) else {}


def mail_link(email: str | None) -> dict[str, str] | None:
    domain = (email or "").rpartition("@")[2].lower()
    for domains, label, url in MAIL_LINKS:
        if domain in domains:
            return {"label": label, "url": url}
    return None


async def _verify_sent(request: Request, errors: dict[str, str] | None = None, status_code: int = 200,
                       headers: dict[str, str] | None = None) -> Response:
    """Check your email: the code field, the address as typed, the resend button and a webmail link.

    Once the address is confirmed somewhere else, the page says so and carries on instead.
    """
    user = await current_user_async(request)
    notice = flash.read_notice(request, request.app.state.settings) or {}
    if user is not None:
        email = user.email
        code_form = not user.email_verified
        confirmed = user.email_verified
    else:
        email = notice.get("email") if isinstance(notice.get("email"), str) else None
        code_form = isinstance(notice.get("b"), str)
        # The link opened in another browser (a phone, usually): this one carries on by itself.
        confirmed = code_form and service.signup_state(request.app.state.db, browser=notice["b"]) == "confirmed"
    masked = accounts.mask_email(email) if email else notice.get("email_masked")
    return await render(
        request,
        "auth/verify_sent.html",
        {
            "email_masked": masked if isinstance(masked, str) else None,
            "email": email,
            "code_form": code_form,
            "confirmed": confirmed,
            "mail_link": mail_link(email),
            "errors": errors or {},
        },
        status_code=status_code,
        headers=headers,
    )


@router.get("/verify-email/sent")
async def verify_sent_page(request: Request) -> Response:
    return await _verify_sent(request)


@router.post("/verify-email/code", dependencies=[Depends(verify_csrf)])
async def verify_code_submit(request: Request) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    user = await current_user_async(request)
    notice = _signup_notice(request)
    result = await service.confirm_signup_code(
        request.app.state.db,
        settings,
        browser=notice.get("b") if user is None else None,
        account_id=user.account_id if user is not None else None,
        code_raw=form_str(form, "code", 32),
        ip_prefix=_ip_prefix(request),
        user_agent=request.headers.get("user-agent"),
        old_session_id=request.cookies.get(settings.session_cookie_name),
    )
    if result.status == "limited":
        return await _verify_sent(
            request, {"code": "Too many attempts. Try again in a few minutes."}, 429, _retry_headers(result.retry_after_s)
        )
    if result.status != "ok":
        return await _verify_sent(request, {"code": CODE_ERRORS.get(result.status, CODE_ERRORS["wrong"])}, 400)
    going = safe_next(notice.get("next") if isinstance(notice.get("next"), str) else None, "/account")
    response = _redirect(going)
    if result.session_id is not None and result.session is not None:
        forget(request)
        sessions.set_cookie(response, settings, result.session_id, result.session, clock.now())
    flash.clear_cookie(response, settings, flash.notice_cookie_name(settings))
    flash.set_flash(response, settings, "success", "Your email is confirmed. Welcome to Starling.")
    return response


@router.post("/verify-email/resend", dependencies=[Depends(verify_csrf)])
async def verify_resend_submit(request: Request) -> Response:
    """"Send it again" on the check-your-email page, signed in or not."""
    settings = request.app.state.settings
    db = request.app.state.db
    user = await current_user_async(request)
    if user is not None:
        outcome = await service.resend_verification(db, settings, user.account_id)
        email = user.email
    else:
        notice = _signup_notice(request)
        if not notice:
            return _redirect("/signup")
        outcome = await service.resend_signup_email(db, settings, browser=notice["b"])
        email = notice.get("email") if isinstance(notice.get("email"), str) else ""
    response = _redirect("/account" if outcome == "already_verified" else "/verify-email/sent")
    if outcome == "already_verified":
        flash.set_flash(response, settings, "info", "Your email address is already confirmed.")
    elif outcome == "limited":
        flash.set_flash(response, settings, "warning", "We sent an email a moment ago. You can send another in a minute.")
    else:
        where = f" to {email}" if email else ""
        flash.set_flash(response, settings, "success", f"We sent a new email{where}. It can take a minute to arrive.")
    return response


@router.get("/verify-email/status")
async def verify_status(request: Request) -> Response:
    """Asked every few seconds by the check-your-email page (site.js): is the address confirmed yet?

    Signed out, only the browser that signed up learns anything, and only about its own sign-up.
    """
    user = await current_user_async(request)
    if user is not None:
        state = "confirmed" if user.email_verified else "waiting"
    else:
        notice = _signup_notice(request)
        state = service.signup_state(request.app.state.db, browser=notice["b"]) if notice else "waiting"
    return JSONResponse({"state": state}, headers={"Cache-Control": "no-store"})


@router.post("/verify-email/continue", dependencies=[Depends(verify_csrf)])
async def verify_continue_submit(request: Request) -> Response:
    """The check-your-email page carries on by itself once the address was confirmed in another browser."""
    settings = request.app.state.settings
    user = await current_user_async(request)
    notice = _signup_notice(request)
    going = safe_next(notice.get("next") if isinstance(notice.get("next"), str) else None, "/account")
    if user is not None and user.email_verified:
        response = _redirect(going)
        flash.clear_cookie(response, settings, flash.notice_cookie_name(settings))
        flash.set_flash(response, settings, "success", "Your email address is confirmed.")
        return response
    if user is None and not notice:
        return _redirect("/login")
    if user is None:
        result = await service.continue_signup(
            request.app.state.db,
            browser=notice["b"],
            ip_prefix=_ip_prefix(request),
            user_agent=request.headers.get("user-agent"),
            old_session_id=request.cookies.get(settings.session_cookie_name),
        )
        if result.status == "ok" and result.session_id is not None and result.session is not None:
            forget(request)
            response = _redirect(going)
            sessions.set_cookie(response, settings, result.session_id, result.session, clock.now())
            flash.clear_cookie(response, settings, flash.notice_cookie_name(settings))
            flash.set_flash(response, settings, "success", "Your email is confirmed. Welcome to Starling.")
            return response
    response = _redirect("/verify-email/sent")
    flash.set_flash(response, settings, "info",
                    "Your email is not confirmed yet. Enter the code from the email, or open its link.")
    return response


@router.get("/verify-email")
async def verify_page(request: Request) -> Response:
    token = request.query_params.get("token", "")[:TOKEN_FIELD_MAX]
    valid = service.token_is_valid(request.app.state.db, token, ("verify", "email_change"))
    user = await current_user_async(request)
    return await render(
        request,
        "auth/verify.html",
        {"token_valid": valid, "token": token if valid else "", "already_confirmed": bool(user and user.email_verified)},
        status_code=200 if valid else 400,
        headers={"Referrer-Policy": "no-referrer"},
    )


@router.post("/verify-email", dependencies=[Depends(verify_csrf)])
async def verify_submit(request: Request) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    await _token_form_limit(request)
    notice = _signup_notice(request)
    result = await service.confirm_email(
        request.app.state.db,
        settings,
        form_str(form, "token", TOKEN_FIELD_MAX),
        browser=notice.get("b"),
        ip_prefix=_ip_prefix(request),
        user_agent=request.headers.get("user-agent"),
        old_session_id=request.cookies.get(settings.session_cookie_name),
    )
    if not result.ok:
        user = await current_user_async(request)
        return await render(
            request, "auth/verify.html",
            {"token_valid": False, "token": "", "already_confirmed": bool(user and user.email_verified)},
            status_code=400,
        )
    going = safe_next(notice.get("next") if isinstance(notice.get("next"), str) else None, "/account")
    if result.session_id is not None and result.session is not None:
        # The link, opened in the browser that signed up: signed in, and on to where they were going.
        forget(request)
        response = _redirect(going)
        sessions.set_cookie(response, settings, result.session_id, result.session, clock.now())
        flash.clear_cookie(response, settings, flash.notice_cookie_name(settings))
        flash.set_flash(response, settings, "success", "Your email is confirmed. Welcome to Starling.")
        return response
    user = await current_user_async(request)
    if user is not None:
        response = _redirect(going)
    else:
        response = _redirect("/login" if going == "/account" else "/login?next=" + quote(going, safe=""))
    if result.purpose == "email_change":
        text = "Your new email address is confirmed. Use it to sign in from now on."
    elif user is not None:
        text = "Your email address is confirmed."
    elif result.code_needed:
        text = "Your email address is confirmed. To finish, type the code from the email in the browser where you signed up."
    else:
        # The page where they signed up notices within seconds (GET /verify-email/status).
        text = "Your email address is confirmed. Your other browser continues by itself, or you can sign in here."
    flash.set_flash(response, settings, "success", text)
    return response


# --------------------------------------------------------------------------- password reset


@router.get("/forgot-password")
async def forgot_page(request: Request) -> Response:
    return await render(request, "auth/forgot.html", {"sent": False, "errors": {}, "form": {"email": ""}, "mail_link": None})


@router.post("/forgot-password", dependencies=[Depends(verify_csrf)])
async def forgot_submit(request: Request) -> Response:
    form = await read_form(request)
    email_raw = form_str(form, "email", EMAIL_FIELD_MAX)
    result = await service.request_password_reset(
        request.app.state.db, request.app.state.settings, email_raw=email_raw, ip_prefix=_ip_prefix(request)
    )
    typed = email_raw.strip()[: accounts.MAX_EMAIL_LENGTH]
    if result.errors:
        return await render(
            request,
            "auth/forgot.html",
            {"sent": False, "errors": result.errors, "form": {"email": typed}, "mail_link": None},
            status_code=429 if result.retry_after_s else 400,
            headers=_retry_headers(result.retry_after_s),
        )
    # Same page whether or not the address has an account. It repeats the address the visitor typed
    # (theirs to see), so a typo shows, and "Send the link again" needs no retyping.
    return await render(
        request, "auth/forgot.html", {"sent": True, "errors": {}, "form": {"email": typed}, "mail_link": mail_link(typed)}
    )


@router.get("/reset-password")
async def reset_page(request: Request) -> Response:
    token = request.query_params.get("token", "")[:TOKEN_FIELD_MAX]
    valid = service.token_is_valid(request.app.state.db, token, ("reset",))
    return await render(
        request,
        "auth/reset.html",
        {"token_valid": valid, "token": token if valid else "", "errors": {}},
        status_code=200 if valid else 400,
        headers={"Referrer-Policy": "no-referrer"},
    )


@router.post("/reset-password", dependencies=[Depends(verify_csrf)])
async def reset_submit(request: Request) -> Response:
    settings = request.app.state.settings
    form = await read_form(request)
    await _token_form_limit(request)
    token = form_str(form, "token", TOKEN_FIELD_MAX)
    password = form_str(form, "password", PASSWORD_FIELD_MAX)
    if "password_confirm" in form and form_str(form, "password_confirm", PASSWORD_FIELD_MAX) != password:
        valid = service.token_is_valid(request.app.state.db, token, ("reset",))
        return await render(
            request,
            "auth/reset.html",
            {"token_valid": valid, "token": token if valid else "", "errors": {"password_confirm": "The passwords don't match."}},
            status_code=400,
        )
    result = await service.reset_password(
        request.app.state.db, settings, token=token, password=password,
        ip_prefix=_ip_prefix(request), user_agent=request.headers.get("user-agent"),
    )
    if not result.ok:
        return await render(
            request,
            "auth/reset.html",
            {"token_valid": result.token_valid, "token": token if result.token_valid else "", "errors": result.errors},
            status_code=400,
        )
    forget(request)
    if result.session_id is not None and result.session is not None:
        response = _redirect("/account")
        sessions.set_cookie(response, settings, result.session_id, result.session, clock.now())
    else:
        response = _redirect("/login")
        sessions.clear_cookie(response, settings)
    if result.revert_blocked:
        flash.set_flash(
            response, settings, "error",
            "Your password changed and every other browser was signed out, but your old email address could NOT be "
            "restored: another account is using it. Your account is still on the new address. Contact support now, "
            "from the old address.")
        return response
    if result.restored_email:
        text = ("Your email address is restored and your new password is saved. You are signed in here; every other "
                "browser was signed out.")
    else:
        text = ("Your new password is saved and you are signed in. Other browsers were signed out, and the Starling "
                "extension was disconnected everywhere: in its panel, choose Sign in to reconnect.")
    flash.set_flash(response, settings, "success", text)
    return response


# --------------------------------------------------------------------------- owner admin identity


async def _owner_admin(request: Request):
    settings = request.app.state.settings
    user, session, account = await resolve_async(request)
    if user is None or session is None or account is None or not is_owner_admin(settings, account):
        raise PageError(404, "Not found.")
    return user, session, account


async def _totp_page(request: Request, account: accounts.Account, errors: dict[str, str], status_code: int = 200) -> Response:
    settings = request.app.state.settings
    secret = await admin.pending_totp_secret_async(request.app.state.db, settings, account.id)
    return await render(
        request,
        "admin/totp.html",
        {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, account.email), "errors": errors},
        status_code=status_code,
    )


@router.get("/admin/totp")
async def totp_page(request: Request, _: CurrentUser = Depends(require_user)) -> Response:
    _, _, account = await _owner_admin(request)
    if account.totp_enabled_at is not None:
        return _redirect("/admin/elevate")
    return await _totp_page(request, account, {})


@router.post("/admin/totp", dependencies=[Depends(verify_csrf)])
async def totp_submit(request: Request, _: CurrentUser = Depends(require_user)) -> Response:
    _, _, account = await _owner_admin(request)
    if account.totp_enabled_at is not None:
        return _redirect("/admin/elevate")
    form = await read_form(request)
    if not account.has_password:
        # The owner area is guarded by a password AND an authenticator; a Google-only owner adds one first.
        return await _totp_page(
            request, account,
            {"form": "The owner area needs a password as well as your authenticator. Add one under Security first."}, 400,
        )
    error = await admin.confirm_totp_enrollment(
        request.app.state.db,
        request.app.state.settings,
        account_id=account.id,
        password=form_str(form, "password", PASSWORD_FIELD_MAX),
        code=form_str(form, "code", 16),
        ip_prefix=_ip_prefix(request),
    )
    if error:
        return await _totp_page(request, account, {"form": error}, status_code=400)
    response = _redirect("/admin/elevate")
    flash.set_flash(response, request.app.state.settings, "success", "Two-factor verification is set up.")
    return response


@router.get("/admin/elevate")
async def elevate_page(request: Request, _: CurrentUser = Depends(require_user)) -> Response:
    _, _, account = await _owner_admin(request)
    if account.totp_enabled_at is None:
        return _redirect("/admin/totp")
    next_path = safe_next(request.query_params.get("next"), "/admin")
    return await render(request, "admin/elevate.html", {"errors": {}, "next": next_path})


@router.post("/admin/elevate", dependencies=[Depends(verify_csrf)])
async def elevate_submit(request: Request, _: CurrentUser = Depends(require_user)) -> Response:
    settings = request.app.state.settings
    _, session, account = await _owner_admin(request)
    if account.totp_enabled_at is None:
        return _redirect("/admin/totp")
    form = await read_form(request)
    next_path = safe_next(form_str(form, "next", 2048), "/admin")
    result = await admin.elevate(
        request.app.state.db,
        settings,
        account_id=account.id,
        session_id_hash=session.id_hash,
        code=form_str(form, "code", 16),
        ip_prefix=_ip_prefix(request),
        user_agent=request.headers.get("user-agent"),
    )
    if result.error or result.session_id is None or result.session is None:
        return await render(
            request, "admin/elevate.html", {"errors": {"form": result.error or "That code is incorrect."}, "next": next_path}, status_code=400
        )
    forget(request)
    response = _redirect(next_path)
    sessions.set_cookie(response, settings, result.session_id, result.session, clock.now())
    return response
