"""Time-based one-time passwords (RFC 6238) for owner admin access.

- HMAC-SHA-1, 30-second steps, 6 digits, accepting one step before and after the current one.
- A matched step is recorded once per account (`claim_step`), so a code cannot be replayed.
- The shared secret is stored encrypted with AES-GCM. The key is derived from
  `STARLING_SECRET_KEY` with HKDF, and the account id is bound as associated data, so a
  ciphertext copied to another account row does not decrypt. During a key rotation a secret
  encrypted under `STARLING_SECRET_KEY_PREVIOUS` still decrypts and is re-encrypted under the
  current key the next time a code is accepted (`auth.admin`).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import sqlite3
import struct
from typing import Callable
from urllib.parse import quote, urlencode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..security import ratelimit
from ..security.crypto import b64url, b64url_decode, derive_key

STEP_S = 30
DIGITS = 6
WINDOW = 1
SECRET_BYTES = 20
ISSUER = "Starling"
ENCRYPTION_VERSION = "v1"
USED_STEP_BUCKET = "totp_used"


def generate_secret() -> str:
    """A new base32 secret (160 bits, no padding) for authenticator apps."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def decode_secret(secret_b32: str) -> bytes:
    cleaned = secret_b32.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest: Callable = hashlib.sha1) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(value).zfill(digits)


def totp(key: bytes, at: int, step: int = STEP_S, digits: int = DIGITS, digest: Callable = hashlib.sha1) -> str:
    return hotp(key, at // step, digits, digest)


def matching_step(key: bytes, code: str, at: int, window: int = WINDOW) -> int | None:
    """The time step the code belongs to (within the window), or None."""
    candidate = (code or "").strip().replace(" ", "")
    if len(candidate) != DIGITS or not candidate.isdigit():
        return None
    counter = at // STEP_S
    matched = None
    for offset in range(-window, window + 1):  # no early exit: same work for every code
        if hmac.compare_digest(hotp(key, counter + offset), candidate):
            matched = counter + offset
    return matched


def claim_step(conn: sqlite3.Connection, account_id: str, step: int) -> bool:
    """Record that `step` was used by the account; False when it was already used."""
    return ratelimit.claim_once(conn, USED_STEP_BUCKET, ratelimit.account_key(account_id), step * STEP_S)


def provisioning_uri(secret_b32: str, account_email: str, issuer: str = ISSUER) -> str:
    label = quote(f"{issuer}:{account_email}", safe="@")
    query = urlencode(
        {"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP_S}
    )
    return f"otpauth://totp/{label}?{query}"


def _cipher(secret_key: bytes) -> AESGCM:
    return AESGCM(derive_key(secret_key, "totp-secret-v1"))


def encrypt_secret(secret_key: bytes, account_id: str, secret_b32: str) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = _cipher(secret_key).encrypt(nonce, secret_b32.encode("ascii"), account_id.encode("utf-8"))
    return f"{ENCRYPTION_VERSION}.{b64url(nonce)}.{b64url(ciphertext)}"


def decrypt_with_any(keys: tuple[bytes, ...], account_id: str, stored: str | None) -> tuple[str | None, bool]:
    """Decrypt with the current key, then the previous one. Returns (secret, needs_reencrypt):
    `needs_reencrypt` is True when only the previous key worked."""
    for index, key in enumerate(keys):
        secret = decrypt_secret(key, account_id, stored)
        if secret is not None:
            return secret, index > 0
    return None, False


def decrypt_secret(secret_key: bytes, account_id: str, stored: str | None) -> str | None:
    if not stored:
        return None
    try:
        version, nonce, ciphertext = stored.split(".")
        if version != ENCRYPTION_VERSION:
            return None
        plain = _cipher(secret_key).decrypt(b64url_decode(nonce), b64url_decode(ciphertext), account_id.encode("utf-8"))
        return plain.decode("ascii")
    except (ValueError, InvalidTag, UnicodeDecodeError, binascii.Error):
        return None
