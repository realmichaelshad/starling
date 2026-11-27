"""Extension connect (CONTRACTS section 9): authorization requests, codes and redemption.

1. `GET /connect` validates the request (`validate_request`): `client_id` in
   `STARLING_EXTENSION_IDS`, `redirect_uri == https://<client_id>.chromiumapp.org/connect`,
   a 43-character base64url S256 `code_challenge`, a 16 to 128 character `state` and a sanitised
   `device_label`. A signed-in visitor gets a `connect_requests` row (10 minutes) bound to the
   account.
2. `POST /connect/approve` (CSRF) issues a code: 256 bits, stored as SHA-256, single use, valid for
   120 seconds, bound to the challenge, account, extension id and redirect URI.
3. `POST /api/v1/devices/token` redeems it (`redeem`). The first redemption attempt spends the code
   whatever its outcome; a second attempt with the same code is refused and revokes the device
   issued from it (RFC 6749 section 4.1.2). The device id equals the connect request id, which
   links the two without an extra column.
"""
from __future__ import annotations

import hashlib
import logging
import random
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlencode

from ..auth import accounts
from ..config import Settings
from ..security.crypto import b64url, equals, random_token, sha256_hex
from . import tokens as device_tokens

log = logging.getLogger(__name__)

REQUEST_TTL_S = 10 * 60
CODE_TTL_S = 120
PURGE_AFTER_S = 86400
LABEL_MAX = 60
DEFAULT_LABEL = "Chrome"

_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_STATE_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
PARAMETERS = ("client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state", "device_label")

COLUMNS = (
    "id, account_id, code_challenge, state_hash, extension_id, redirect_uri, device_label, created_at, "
    "approved_at, expires_at, redeemed_at"
)


class ConnectError(ValueError):
    """The authorization request is not acceptable; the message is safe to show."""


@dataclass(frozen=True)
class AuthorizationRequest:
    client_id: str
    redirect_uri: str
    code_challenge: str
    state: str
    device_label: str


@dataclass(frozen=True)
class ConnectRequest:
    id: str
    account_id: str | None
    code_challenge: str
    state_hash: str
    extension_id: str
    redirect_uri: str
    device_label: str
    created_at: int
    approved_at: int | None
    expires_at: int
    redeemed_at: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ConnectRequest":
        return cls(**{name: row[name] for name in COLUMNS.replace(" ", "").split(",")})


def expected_redirect_uri(client_id: str) -> str:
    return f"https://{client_id}.chromiumapp.org/connect"


def pkce_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def sanitize_label(raw: str | None) -> str:
    """Printable text only (no control or bidi formatting characters), collapsed, at most 60 characters."""
    text = unicodedata.normalize("NFKC", raw or "")
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    return " ".join(cleaned.split())[:LABEL_MAX].strip() or DEFAULT_LABEL


def validate_request(settings: Settings, params: Mapping[str, str]) -> AuthorizationRequest:
    client_id = params.get("client_id", "")
    if not client_id or client_id not in settings.extension_ids:
        raise ConnectError("This extension is not recognised by Starling.")
    redirect_uri = params.get("redirect_uri", "")
    if redirect_uri != expected_redirect_uri(client_id):
        raise ConnectError("The connection request has an unexpected return address.")
    if params.get("code_challenge_method", "") != "S256" or not _CHALLENGE_RE.match(params.get("code_challenge", "")):
        raise ConnectError("The connection request is incomplete. Start connecting again from the extension.")
    state = params.get("state", "")
    if not _STATE_RE.match(state):
        raise ConnectError("The connection request is incomplete. Start connecting again from the extension.")
    return AuthorizationRequest(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=params["code_challenge"],
        state=state,
        device_label=sanitize_label(params.get("device_label")),
    )


def create_request(conn: sqlite3.Connection, req: AuthorizationRequest, account_id: str, now: int) -> str:
    if random.random() < 0.01:
        conn.execute("DELETE FROM connect_requests WHERE expires_at < ?", (now - PURGE_AFTER_S,))
    request_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO connect_requests (id, account_id, code_challenge, state_hash, extension_id, redirect_uri, "
        "device_label, code_hash, created_at, approved_at, expires_at, redeemed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, NULL)",
        (
            request_id, account_id, req.code_challenge, sha256_hex(req.state), req.client_id, req.redirect_uri,
            req.device_label, now, now + REQUEST_TTL_S,
        ),
    )
    return request_id


def get_request(conn: sqlite3.Connection, request_id: str) -> ConnectRequest | None:
    try:
        uuid.UUID(request_id)
    except (ValueError, AttributeError, TypeError):
        return None
    row = conn.execute(f"SELECT {COLUMNS} FROM connect_requests WHERE id = ?", (request_id,)).fetchone()
    return ConnectRequest.from_row(row) if row else None


def _pending_for(conn: sqlite3.Connection, settings: Settings, request_id: str, account_id: str, state: str, now: int) -> ConnectRequest | None:
    found = get_request(conn, request_id)
    if found is None or found.account_id != account_id or found.approved_at is not None or found.expires_at <= now:
        return None
    if found.extension_id not in settings.extension_ids or not equals(found.state_hash, sha256_hex(state or "")):
        return None
    return found


DEVICE_LIMIT_REASON = "device_limit"


class DeviceLimitReached(Exception):
    """The account already has as many connected browsers as it may have."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"device limit {limit}")
        self.limit = limit


def approve(conn: sqlite3.Connection, settings: Settings, *, request_id: str, account_id: str, state: str, now: int) -> str | None:
    """Issue the code for this account's pending request; returns the redirect location or None.

    Raises `DeviceLimitReached` when the account is at MAX_DEVICES_PER_ACCOUNT. Nothing a person
    does reaches that number; it exists so one account cannot fill the `devices` table or be
    handed round as a shared login without end. The pending request is left as it was, so
    disconnecting a browser and pressing Approve again works.
    """
    found = _pending_for(conn, settings, request_id, account_id, state, now)
    if found is None:
        return None
    if settings.max_devices_per_account > 0:
        account = conn.execute("SELECT auth_epoch FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if account is not None:
            active = device_tokens.list_active(conn, account_id, int(account["auth_epoch"]), now)
            if len(active) >= settings.max_devices_per_account:
                raise DeviceLimitReached(settings.max_devices_per_account)
    code = random_token()
    updated = conn.execute(
        "UPDATE connect_requests SET code_hash = ?, approved_at = ?, expires_at = ? WHERE id = ? AND approved_at IS NULL",
        (sha256_hex(code), now, now + CODE_TTL_S, found.id),
    ).rowcount
    if updated != 1:
        return None
    return found.redirect_uri + "?" + urlencode({"code": code, "state": state})


def deny(conn: sqlite3.Connection, settings: Settings, *, request_id: str, account_id: str, state: str, now: int) -> str | None:
    found = _pending_for(conn, settings, request_id, account_id, state, now)
    if found is None:
        return None
    conn.execute("UPDATE connect_requests SET expires_at = ? WHERE id = ?", (now, found.id))
    return found.redirect_uri + "?" + urlencode({"error": "access_denied", "state": state})


@dataclass(frozen=True)
class RedeemOutcome:
    ok: bool
    reason: str
    token: str | None = None
    device: device_tokens.Device | None = None


def redeem(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    code: object,
    code_verifier: object,
    redirect_uri: object,
    client_id: object,
    extension_version: str | None,
    now: int,
) -> RedeemOutcome:
    """Exchange a code for a device token inside a writer transaction (never raises for bad grants,
    so the spent-code marker and replay revocation are committed)."""
    if not isinstance(code, str) or not _CODE_RE.match(code):
        return RedeemOutcome(False, "malformed_code")
    row = conn.execute(f"SELECT {COLUMNS} FROM connect_requests WHERE code_hash = ?", (sha256_hex(code),)).fetchone()
    if row is None:
        return RedeemOutcome(False, "unknown_code")
    found = ConnectRequest.from_row(row)
    if found.redeemed_at is not None:
        device_tokens.revoke(conn, found.id, now, "code_replay")
        return RedeemOutcome(False, "code_replay")
    conn.execute("UPDATE connect_requests SET redeemed_at = ? WHERE id = ?", (now, found.id))
    if found.approved_at is None or found.expires_at <= now:
        return RedeemOutcome(False, "expired_code")
    if client_id != found.extension_id or found.extension_id not in settings.extension_ids:
        return RedeemOutcome(False, "client_mismatch")
    if redirect_uri != found.redirect_uri:
        return RedeemOutcome(False, "redirect_mismatch")
    if not isinstance(code_verifier, str) or not _VERIFIER_RE.match(code_verifier):
        return RedeemOutcome(False, "pkce_mismatch")
    if not equals(pkce_challenge(code_verifier), found.code_challenge):
        return RedeemOutcome(False, "pkce_mismatch")
    account = accounts.get_by_id(conn, found.account_id or "")
    if account is None or account.status not in ("active", "suspended"):
        return RedeemOutcome(False, "account_unavailable")
    if account.status == "suspended":
        return RedeemOutcome(False, "account_suspended")
    if settings.max_devices_per_account > 0:
        # Checked again HERE, where the device is created. Approval alone is not enough: no device
        # exists until a code is redeemed, so approving thirty requests first and redeeming them
        # afterwards passed thirty checks that each saw an account under its limit.
        active = device_tokens.list_active(conn, account.id, account.auth_epoch, now)
        if len(active) >= settings.max_devices_per_account:
            return RedeemOutcome(False, DEVICE_LIMIT_REASON)
    token, device = device_tokens.issue(
        conn,
        device_id=found.id,
        account_id=account.id,
        auth_epoch=account.auth_epoch,
        label=found.device_label,
        extension_id=found.extension_id,
        extension_version=extension_version,
        now=now,
    )
    return RedeemOutcome(True, "issued", token, device)
