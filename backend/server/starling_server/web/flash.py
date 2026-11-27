"""Short-lived HMAC-signed cookies: flash messages and one-time notices.

Flash messages survive exactly one redirect (read and cleared by the next rendered page, or
expired after 5 minutes). Notices carry small non-secret values between pages, such as the
masked address shown on "check your email". Both are HttpOnly, SameSite=Lax, Secure on https.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import Request, Response

from ..config import Settings
from ..security import clock
from ..security.crypto import b64url, b64url_decode, derive_key, equals, sign, signing_keys

FLASH_MAX_AGE_S = 300
NOTICE_MAX_AGE_S = 3600
LEVELS = ("info", "success", "warning", "error")
MAX_MESSAGES = 5
MAX_TEXT = 300
MAX_COOKIE = 4000


def flash_cookie_name(settings: Settings) -> str:
    return "__Host-starling_flash" if settings.cookie_secure else "starling_flash"


def notice_cookie_name(settings: Settings) -> str:
    return "__Host-starling_notice" if settings.cookie_secure else "starling_notice"


def _encode(settings: Settings, purpose: str, payload: Any, max_age: int) -> str:
    data = b64url(json.dumps({"p": payload, "exp": clock.now() + max_age}, separators=(",", ":")).encode("utf-8"))
    return data + "." + sign(derive_key(settings.secret_key, purpose), data)


def _decode(settings: Settings, purpose: str, value: str | None) -> Any:
    if not value or "." not in value or len(value) > MAX_COOKIE:
        return None
    data, _, signature = value.partition(".")
    if not any(equals(sign(derive_key(key, purpose), data), signature) for key in signing_keys(settings)):
        return None
    try:
        obj = json.loads(b64url_decode(data))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("exp"), int) or obj["exp"] < clock.now():
        return None
    return obj.get("p")


def _set(response: Response, settings: Settings, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )


def _clear(response: Response, settings: Settings, name: str) -> None:
    response.delete_cookie(name, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")


def seal(settings: Settings, purpose: str, payload: Any, max_age: int) -> str:
    """A signed, expiring cookie value for other short round trips (auth/google.py)."""
    return _encode(settings, purpose, payload, max_age)


def unseal(settings: Settings, purpose: str, value: str | None) -> Any:
    """The payload of `seal`, or None when the value is missing, altered, for another purpose or expired."""
    return _decode(settings, purpose, value)


def set_cookie(response: Response, settings: Settings, name: str, value: str, max_age: int) -> None:
    _set(response, settings, name, value, max_age)


def clear_cookie(response: Response, settings: Settings, name: str) -> None:
    _clear(response, settings, name)


def set_flash(response: Response, settings: Settings, level: str, text: str) -> None:
    message = {"level": level if level in LEVELS else "info", "text": text[:MAX_TEXT]}
    _set(response, settings, flash_cookie_name(settings), _encode(settings, "flash", [message], FLASH_MAX_AGE_S), FLASH_MAX_AGE_S)


def read_flash(request: Request, settings: Settings) -> list[dict[str, str]]:
    payload = _decode(settings, "flash", request.cookies.get(flash_cookie_name(settings)))
    if not isinstance(payload, list):
        return []
    messages = []
    for item in payload[:MAX_MESSAGES]:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            level = item.get("level") if item.get("level") in LEVELS else "info"
            messages.append({"level": level, "text": item["text"][:MAX_TEXT]})
    return messages


def has_flash_cookie(request: Request, settings: Settings) -> bool:
    return flash_cookie_name(settings) in request.cookies


def clear_flash(response: Response, settings: Settings) -> None:
    _clear(response, settings, flash_cookie_name(settings))


def set_notice(response: Response, settings: Settings, payload: dict[str, Any], max_age: int = NOTICE_MAX_AGE_S) -> None:
    _set(response, settings, notice_cookie_name(settings), _encode(settings, "notice", payload, max_age), max_age)


def read_notice(request: Request, settings: Settings) -> dict[str, Any] | None:
    payload = _decode(settings, "notice", request.cookies.get(notice_cookie_name(settings)))
    return payload if isinstance(payload, dict) else None
