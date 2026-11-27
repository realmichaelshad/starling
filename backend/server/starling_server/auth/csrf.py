"""CSRF protection for website forms.

Token sources:

- Signed in: an HMAC of the session's random `csrf_secret` (a new secret with every session id).
- Signed out (signup, login, forgot password, reset, verification): a random pre-session cookie
  (HttpOnly, SameSite=Lax, `__Host-` prefixed on https); the form token is an HMAC of that cookie.
  A token copied from another browser does not match this browser's cookie, which also stops
  login CSRF (a foreign site signing the visitor in to the attacker's account).

Every state-changing form POST must also pass the origin check: a present `Origin` must be the
public origin; without a usable `Origin` (absent, or `null`), `Sec-Fetch-Site: same-origin` is
required. Browsers send `Origin: null` with a form POST from a page served with
`Referrer-Policy: no-referrer`, which is how the email verification, password reset and extension
approval pages are served, so `Origin: null` alone is refused but is accepted together with
`Sec-Fetch-Site: same-origin` (a header only the browser can set).
`/webhooks/stripe` (billing) is the only CSRF-exempt POST.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi import Request, Response
from starlette.datastructures import FormData

from ..config import Settings
from ..security.crypto import derive_key, equals, random_token, sign, signing_keys

FORM_FIELD = "csrf_token"
HEADER = "x-csrf-token"
PRESESSION_MAX_AGE_S = 24 * 3600
_COOKIE_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def presession_cookie_name(settings: Settings) -> str:
    return "__Host-starling_csrf" if settings.cookie_secure else "starling_csrf"


def session_tokens(settings: Settings, csrf_secret: str) -> list[str]:
    """Accepted tokens for a session: current key first, then the previous key during rotation."""
    return [sign(derive_key(key, "csrf-session"), csrf_secret) for key in signing_keys(settings)]


def presession_tokens(settings: Settings, cookie_value: str) -> list[str]:
    return [sign(derive_key(key, "csrf-presession"), cookie_value) for key in signing_keys(settings)]


def session_token(settings: Settings, csrf_secret: str) -> str:
    return session_tokens(settings, csrf_secret)[0]


def presession_token(settings: Settings, cookie_value: str) -> str:
    return presession_tokens(settings, cookie_value)[0]


def presession_cookie(request: Request, settings: Settings) -> str | None:
    value = request.cookies.get(presession_cookie_name(settings))
    return value if value and _COOKIE_VALUE_RE.match(value) else None


def ensure_presession(request: Request, settings: Settings) -> tuple[str, str | None]:
    """Return (form token, new cookie value to set or None when the browser already has one)."""
    value = presession_cookie(request, settings)
    if value is not None:
        return presession_token(settings, value), None
    value = random_token()
    return presession_token(settings, value), value


def set_presession_cookie(response: Response, settings: Settings, value: str) -> None:
    response.set_cookie(
        presession_cookie_name(settings),
        value,
        max_age=PRESESSION_MAX_AGE_S,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _origin(scheme: str, host: str, port: int | None) -> str:
    scheme = scheme.lower()
    host = host.lower()
    if port is None or port == {"http": 80, "https": 443}.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def normalize_origin(value: str) -> str | None:
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        return None
    return _origin(parts.scheme, parts.hostname, port)


def public_origin(settings: Settings) -> str:
    parts = urlsplit(settings.public_url)
    try:
        port = parts.port
    except ValueError:
        port = None
    return _origin(parts.scheme, parts.hostname or "", port)


def origin_allowed(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    if origin is not None and origin.strip().lower() != "null":
        return normalize_origin(origin) == public_origin(settings)
    # No usable Origin. A page served with `Referrer-Policy: no-referrer` makes the browser send
    # `Origin: null` with its own form POST; `Sec-Fetch-Site` still says where the form came from,
    # and a page cannot set or change it.
    return request.headers.get("sec-fetch-site", "").strip().lower() == "same-origin"


def submitted_token(request: Request, form: FormData | None) -> str:
    value = form.get(FORM_FIELD) if form is not None else None
    if isinstance(value, str) and value:
        return value
    return request.headers.get(HEADER, "")


def token_matches(expected: list[str], submitted: str) -> bool:
    """True when `submitted` equals any accepted token (every candidate is compared)."""
    if not submitted or len(submitted) > 128:
        return False
    matched = False
    for candidate in expected:
        matched = equals(candidate, submitted) or matched
    return matched
