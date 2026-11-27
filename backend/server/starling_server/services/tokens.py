"""Signed services tokens (CONTRACTS 4.3).

Format: `svc1.<subject>.<expires_at>.<nonce>.<signature>`

- `subject`: `t-<tenant_id>` for an engine, `admin` for the gateway's admin calls
- `expires_at`: unix seconds
- `nonce`: 16 random bytes, base64url without padding
- `signature`: base64url (no padding) of HMAC-SHA256(key, "svc1.<subject>.<expires_at>.<nonce>")

The services process authenticates callers from the token alone (no database lookup). The tenant
id used for billing always comes from the signed subject, so a token for tenant A can never bill
tenant B. The key (`STARLING_SERVICES_TOKEN_KEY`) is held only by the gateway and the services
process, never by engines. The engine pool imports `mint_engine_token` from here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass

VERSION = "svc1"
ENGINE_TTL_S = 24 * 3600
ADMIN_TTL_S = 5 * 60
CLOCK_SKEW_S = 60
MIN_KEY_BYTES = 32
MAX_TOKEN_LENGTH = 256

KIND_TENANT = "tenant"
KIND_ADMIN = "admin"

_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{22}")
_EXPIRES_RE = re.compile(r"[0-9]{1,12}")


class TokenError(Exception):
    """The token is malformed, tampered with, expired, or signed with another key.

    The message is a short reason category; it never contains the token.
    """


@dataclass(frozen=True)
class Subject:
    kind: str
    tenant_id: str | None
    expires_at: int

    @property
    def is_admin(self) -> bool:
        return self.kind == KIND_ADMIN

    @property
    def value(self) -> str:
        return "admin" if self.is_admin else f"t-{self.tenant_id}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise TokenError("services token key is missing or shorter than 32 bytes")
    return bytes(key)


def _canonical_tenant(tenant_id: object) -> str:
    if not isinstance(tenant_id, str):
        raise TokenError("tenant id must be a UUID string")
    try:
        parsed = uuid.UUID(tenant_id)
    except ValueError as exc:
        raise TokenError("tenant id must be a UUID string") from exc
    if str(parsed) != tenant_id:
        raise TokenError("tenant id must be a canonical lowercase UUID")
    return tenant_id


def _check_ttl(ttl: int, maximum: int) -> int:
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0 or ttl > maximum:
        raise TokenError(f"token lifetime must be between 1 and {maximum} seconds")
    return ttl


def _sign(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def _mint(key: bytes, subject: str, expires_at: int) -> str:
    payload = f"{VERSION}.{subject}.{expires_at}.{_b64(secrets.token_bytes(16))}"
    return f"{payload}.{_sign(key, payload)}"


def mint_engine_token(key: bytes, tenant_id: str, now: int | float, ttl: int = ENGINE_TTL_S) -> str:
    """Token for one engine process; the engine pool mints a fresh one at every spawn."""
    key = _check_key(key)
    tenant = _canonical_tenant(tenant_id)
    return _mint(key, f"t-{tenant}", int(now) + _check_ttl(ttl, ENGINE_TTL_S))


def mint_admin_token(key: bytes, now: int | float, ttl: int = ADMIN_TTL_S) -> str:
    """Short-lived token for the gateway's calls to the services admin endpoints."""
    key = _check_key(key)
    return _mint(key, "admin", int(now) + _check_ttl(ttl, ADMIN_TTL_S))


def verify(key: bytes, token: str, now: int | float) -> Subject:
    """Return the signed subject or raise `TokenError`.

    The signature is compared in constant time before any field is trusted. Expired tokens,
    unknown subjects and lifetimes longer than the contract allows are rejected.
    """
    key = _check_key(key)
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
        raise TokenError("malformed token")
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != VERSION:
        raise TokenError("malformed token")
    _, subject, expires_raw, nonce, signature = parts
    expected = _sign(key, ".".join(parts[:4]))
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii")):
        raise TokenError("bad signature")
    if not _EXPIRES_RE.fullmatch(expires_raw) or not _NONCE_RE.fullmatch(nonce):
        raise TokenError("malformed token")
    expires_at = int(expires_raw)
    now_s = int(now)
    if now_s >= expires_at:
        raise TokenError("expired token")
    if subject == "admin":
        kind, tenant, max_ttl = KIND_ADMIN, None, ADMIN_TTL_S
    elif subject.startswith("t-"):
        try:
            tenant = _canonical_tenant(subject[2:])
        except TokenError as exc:
            raise TokenError("unknown subject") from exc
        kind, max_ttl = KIND_TENANT, ENGINE_TTL_S
    else:
        raise TokenError("unknown subject")
    if expires_at - now_s > max_ttl + CLOCK_SKEW_S:
        raise TokenError("token lifetime is longer than allowed")
    return Subject(kind=kind, tenant_id=tenant, expires_at=expires_at)


def verify_engine_token(key: bytes, token: str, tenant_id: str, now: int | float) -> Subject:
    """Verify a token and require it to belong to `tenant_id` (spawner validation, CONTRACTS 5)."""
    subject = verify(key, token, now)
    if subject.kind != KIND_TENANT or not hmac.compare_digest(
        str(subject.tenant_id).encode("ascii"), _canonical_tenant(tenant_id).encode("ascii")
    ):
        raise TokenError("token belongs to another subject")
    return subject
