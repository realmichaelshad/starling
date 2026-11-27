"""Small cryptographic helpers shared by sessions, CSRF, email tokens and devices.

Rules:
- Every bearer secret (session id, email token, connect code, device token) is 256 bits from
  `secrets` and is stored only as its SHA-256 hex digest.
- Keys for distinct purposes are derived from `STARLING_SECRET_KEY` with HKDF-SHA256 and a
  purpose label, so a value signed for one purpose never verifies for another.
- Comparisons of secrets use `hmac.compare_digest`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_BYTES = 32  # 256 bits


def random_token(nbytes: int = TOKEN_BYTES) -> str:
    """URL-safe random secret (43 characters for 32 bytes)."""
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def signing_keys(settings) -> tuple[bytes, ...]:
    """Secrets to verify with, current first: `STARLING_SECRET_KEY`, then
    `STARLING_SECRET_KEY_PREVIOUS` while a rotation is in progress. New values always use the
    current key."""
    current = settings.secret_key
    previous = getattr(settings, "secret_key_previous", b"") or b""
    if previous and previous != current:
        return (current, previous)
    return (current,)


@lru_cache(maxsize=64)
def derive_key(secret_key: bytes, purpose: str, length: int = 32) -> bytes:
    """Purpose-bound key derived from the server secret (HKDF-SHA256)."""
    if not secret_key:
        raise RuntimeError("STARLING_SECRET_KEY is not available")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=b"starling/" + purpose.encode("ascii"))
    return hkdf.derive(secret_key)


def sign(key: bytes, message: str | bytes) -> str:
    data = message.encode("utf-8") if isinstance(message, str) else message
    return b64url(hmac.new(key, data, hashlib.sha256).digest())


def equals(a: str | bytes, b: str | bytes) -> bool:
    """Constant-time comparison that tolerates mixed str/bytes input."""
    a_bytes = a.encode("utf-8") if isinstance(a, str) else a
    b_bytes = b.encode("utf-8") if isinstance(b, str) else b
    return hmac.compare_digest(a_bytes, b_bytes)
