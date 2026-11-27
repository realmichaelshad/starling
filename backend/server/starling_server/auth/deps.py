"""Authentication dependencies shared by every router.

Interface fixed by docs/architecture/CONTRACTS.md section 14; implemented by the server
core workstream. Other workstreams import these names and must not re-implement them.

- Website identity comes only from the session cookie; `/api/...` paths never read it.
- Extension identity comes only from `Authorization: Bearer stl_dev_...` (`require_device`).
- Refusals are the shaped exceptions of `security.errors` (they subclass `HTTPException`):
  HTML requests are redirected to `/login?next=...`; JSON and API requests get the error body of
  CONTRACTS section 6.3.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import anyio
from fastapi import Request

from ..devices import tokens as device_tokens
from ..security import clock
from ..security.errors import ApiError, PageError, WebRedirect
from ..security.http import read_form, safe_next, wants_json
from . import accounts, csrf, sessions


@dataclass(frozen=True)
class CurrentUser:
    account_id: str
    tenant_seq: int
    email: str
    email_verified: bool
    is_admin: bool
    status: str
    session_id_hash: str
    csrf_token: str
    admin_elevated: bool


@dataclass(frozen=True)
class DeviceAuth:
    account_id: str
    tenant_seq: int
    device_id: str
    email_verified: bool
    status: str
    extension_version: str | None


_CACHE_ATTR = "starling_auth"
_MISSING = object()
_NOBODY: tuple[None, None, None] = (None, None, None)


def is_owner_admin(settings, account: accounts.Account) -> bool:
    """Admin identity: CLI grant, verified email listed in STARLING_OWNER_EMAILS, active account."""
    return (
        account.is_admin
        and account.email_verified
        and account.status == "active"
        and account.email_norm in settings.owner_emails
    )


def _load(request: Request):
    if request.url.path.startswith("/api/"):
        return _NOBODY  # session cookies never authenticate the extension API
    settings = request.app.state.settings
    session_id = request.cookies.get(settings.session_cookie_name)
    if not session_id:
        return _NOBODY
    db = request.app.state.db
    now = clock.now()
    with db.read() as conn:
        session = sessions.get(conn, session_id)
        account = accounts.get_by_id(conn, session.account_id) if session is not None else None
    if session is None or account is None or not session.is_active(now):
        return _NOBODY
    if session.auth_epoch != account.auth_epoch or not account.can_sign_in:
        return _NOBODY
    if now - session.last_seen_at >= sessions.TOUCH_INTERVAL_S:
        db.write(lambda conn: sessions.touch(conn, session.id_hash, now))
    admin = is_owner_admin(settings, account)
    user = CurrentUser(
        account_id=account.id,
        tenant_seq=account.tenant_seq,
        email=account.email,
        email_verified=account.email_verified,
        is_admin=admin,
        status=account.status,
        session_id_hash=session.id_hash,
        csrf_token=csrf.session_token(settings, session.csrf_secret),
        admin_elevated=admin and account.totp_enabled_at is not None and session.is_elevated(now),
    )
    return user, session, account


def resolve(request: Request):
    """(CurrentUser, WebSession, Account) for the request, or three Nones; cached per request."""
    cached = getattr(request.state, _CACHE_ATTR, _MISSING)
    if cached is _MISSING:
        cached = _load(request)
        setattr(request.state, _CACHE_ATTR, cached)
    return cached


async def resolve_async(request: Request):
    if getattr(request.state, _CACHE_ATTR, _MISSING) is not _MISSING:
        return resolve(request)
    return await anyio.to_thread.run_sync(resolve, request)


def forget(request: Request) -> None:
    """Drop the cached identity after sign-in, sign-out or rotation within a request."""
    if hasattr(request.state, _CACHE_ATTR):
        delattr(request.state, _CACHE_ATTR)


def get_current_user(request: Request) -> CurrentUser | None:
    """The signed-in website user, or None. Never reads device tokens."""
    return resolve(request)[0]


async def current_user_async(request: Request) -> CurrentUser | None:
    return (await resolve_async(request))[0]


def _return_path(request: Request, default: str) -> str:
    if request.method not in ("GET", "HEAD"):
        return default
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return safe_next(target, default)


def login_redirect(request: Request) -> WebRedirect:
    return WebRedirect("/login?next=" + quote(_return_path(request, "/account"), safe=""))


def require_user(request: Request) -> CurrentUser:
    """Signed-in user or redirect/401 (HTML routes redirect to /login?next=...)."""
    user = get_current_user(request)
    if user is not None:
        return user
    if wants_json(request):
        raise ApiError("signed_out", 401, "Sign in to continue.")
    raise login_redirect(request)


def require_verified_user(request: Request) -> CurrentUser:
    user = require_user(request)
    if not user.email_verified:
        message = f"Confirm your email address before you continue. We sent a 6-digit code and a link to {user.email}."
        if wants_json(request):
            raise ApiError("email_unverified", 403, message)
        raise PageError(403, message, kind="unverified")
    return user


def require_admin(request: Request) -> CurrentUser:
    """Admin granted by CLI, listed in STARLING_OWNER_EMAILS, TOTP-verified and elevated."""
    user = require_user(request)
    settings = request.app.state.settings
    _, session, account = resolve(request)
    if session is None or account is None or not is_owner_admin(settings, account):
        if wants_json(request):
            raise ApiError("not_found", 404, "Not found.")
        raise PageError(404, "Not found.")
    if account.totp_enabled_at is None or not account.totp_secret_enc:
        if wants_json(request):
            raise ApiError("admin_totp_required", 403, "Set up two-factor verification first.")
        raise WebRedirect("/admin/totp")
    if not session.is_elevated(clock.now()):
        if wants_json(request):
            raise ApiError("admin_elevation_required", 403, "Confirm your authenticator code first.")
        raise WebRedirect("/admin/elevate?next=" + quote(_return_path(request, "/admin"), safe=""))
    return user


def _signed_out() -> ApiError:
    return ApiError(
        "signed_out", 401, "Sign in again to continue.", headers={"WWW-Authenticate": 'Bearer realm="starling"'}
    )


def require_device(request: Request) -> DeviceAuth:
    """Bearer device token for /api/v1 routes; raises the 401 `signed_out` API error."""
    settings = request.app.state.settings
    token = device_tokens.parse_bearer(request.headers.get("authorization"))
    if token is None:
        raise _signed_out()
    db = request.app.state.db
    now = clock.now()
    with db.read() as conn:
        device = device_tokens.find_by_token(conn, token)
        account = accounts.get_by_id(conn, device.account_id) if device is not None else None
    if device is None or account is None or not device.is_active(now) or device.auth_epoch != account.auth_epoch:
        raise _signed_out()
    request.state.account_id = account.id  # attributes later API refusals (security.errors.record_api_error)
    if account.status == "suspended":
        raise ApiError("account_suspended", 403, "This account is suspended. Contact support for help.")
    if account.status != "active":
        raise _signed_out()
    version = request.headers.get("x-starling-extension", "").strip()[:32] or None
    if not device_tokens.version_ok(version, settings.extension_min_version):
        raise ApiError("extension_outdated", 426, "Update the Starling extension to keep using it.")
    if device.last_used_at is None or now - device.last_used_at >= device_tokens.LAST_USED_INTERVAL_S:
        db.write(lambda conn: device_tokens.mark_used(conn, device.id, now, version))
    return DeviceAuth(
        account_id=account.id,
        tenant_seq=account.tenant_seq,
        device_id=device.id,
        email_verified=account.email_verified,
        status=account.status,
        extension_version=version,
    )


def _csrf_failure(request: Request) -> Exception:
    message = "This form expired or came from another site. Go back, reload the page and try again."
    if wants_json(request):
        return ApiError("csrf_failed", 403, message)
    return PageError(403, message, kind="csrf")


async def verify_csrf(request: Request) -> None:
    """Check the form `csrf_token` and the Origin / Sec-Fetch-Site headers for POSTs."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    settings = request.app.state.settings
    if not csrf.origin_allowed(request, settings):
        raise _csrf_failure(request)
    form = None
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await read_form(request)
    submitted = csrf.submitted_token(request, form)
    user, session, _ = await resolve_async(request)
    if user is not None and session is not None:
        expected = csrf.session_tokens(settings, session.csrf_secret)
    else:
        cookie = csrf.presession_cookie(request, settings)
        if cookie is None:
            raise _csrf_failure(request)
        expected = csrf.presession_tokens(settings, cookie)
    if not csrf.token_matches(expected, submitted):
        raise _csrf_failure(request)
