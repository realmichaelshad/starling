"""Stripe webhook signatures (CONTRACTS section 8).

`Stripe-Signature: t=<unix seconds>,v1=<hex>,v1=<hex>,v0=<hex>`. Each `v1` is
HMAC-SHA256 over `"{t}.{raw_body}"` keyed with an endpoint signing secret. While a
secret is being rolled Stripe sends one `v1` per active secret, so every `v1` is checked
against the current and the previous secret. Only `v1` is trusted (older schemes are
ignored to prevent downgrades). The timestamp must be within the tolerance in both
directions; a tolerance of 0 would disable the recency check and is refused.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Sequence

DEFAULT_TOLERANCE_S = 300
MAX_HEADER_LENGTH = 8192
MAX_SIGNATURES = 16
# Plain ASCII digits only: `str.isdigit()` also accepts characters `int()` refuses ("²") and
# strings longer than CPython parses, and either would leave the route with an uncaught error.
_TIMESTAMP_RE = re.compile(r"[0-9]{1,19}")


class SignatureError(ValueError):
    """The request is not a valid, recent Stripe delivery."""


def compute_signature(payload: bytes, secret: str, timestamp: int) -> str:
    message = str(int(timestamp)).encode("ascii") + b"." + payload
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def parse_header(header: str | None) -> tuple[int, list[str]]:
    """Return the timestamp and every `v1` signature of a `Stripe-Signature` header."""
    if not header or len(header) > MAX_HEADER_LENGTH:
        raise SignatureError("missing or oversized Stripe-Signature header")
    timestamp: int | None = None
    signatures: list[str] = []
    for element in header.split(","):
        key, sep, value = element.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            if timestamp is not None or not _TIMESTAMP_RE.fullmatch(value):
                raise SignatureError("invalid timestamp in Stripe-Signature header")
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None:
        raise SignatureError("no timestamp in Stripe-Signature header")
    if not signatures:
        raise SignatureError("no v1 signature in Stripe-Signature header")
    if len(signatures) > MAX_SIGNATURES:
        raise SignatureError("too many signatures in Stripe-Signature header")
    return timestamp, signatures


def verify(
    payload: bytes,
    header: str | None,
    secrets: Sequence[str],
    *,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
    now: int | None = None,
) -> int:
    """Verify a delivery; return its signed timestamp or raise `SignatureError`.

    `secrets` holds the current secret and optionally the previous one. Configuration
    mistakes (no secret, tolerance <= 0) raise `ValueError` instead.
    """
    if tolerance_s is None or int(tolerance_s) <= 0:
        raise ValueError("webhook signature tolerance must be a positive number of seconds")
    keys = [secret for secret in secrets if secret]
    if not keys:
        raise ValueError("no webhook signing secret configured")
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be the raw request body bytes")
    timestamp, signatures = parse_header(header)
    matched = False
    for key in keys:
        expected = compute_signature(bytes(payload), key, timestamp).encode("ascii")
        for candidate in signatures:
            # Compare every pair so the time taken does not depend on which one matches.
            if hmac.compare_digest(expected, candidate.encode("utf-8", "replace")):
                matched = True
    if not matched:
        raise SignatureError("no signature matches the payload")
    current = int(time.time()) if now is None else int(now)
    if current - timestamp > tolerance_s:
        raise SignatureError("signature timestamp is too old")
    if timestamp - current > tolerance_s:
        raise SignatureError("signature timestamp is in the future")
    return timestamp


def sign_header(payload: bytes, secrets: str | Sequence[str], timestamp: int | None = None) -> str:
    """Build a `Stripe-Signature` header the way Stripe does (test helper)."""
    keys = [secrets] if isinstance(secrets, str) else list(secrets)
    ts = int(time.time()) if timestamp is None else int(timestamp)
    parts = [f"t={ts}"] + [f"v1={compute_signature(payload, key, ts)}" for key in keys]
    return ",".join(parts)
