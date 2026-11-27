"""Owner admin identity: CLI grant and revoke, TOTP enrollment and 15-minute elevation.

Admin access (`auth.deps.require_admin`) needs all of:

1. `accounts.is_admin = 1`, set only by `python -m starling_server.cli admin-grant --email X`, which
   refuses unless the account exists, is active, has verified its email, and X is listed in
   `STARLING_OWNER_EMAILS`;
2. that email still verified and still listed (checked on every request);
3. an enrolled TOTP authenticator (enrollment needs the account password and a valid code);
4. an elevated session: a TOTP code within the last 15 minutes (the session id rotates).

`admin-revoke` clears the grant, the TOTP enrollment and every elevation. Changing the account email
also clears them. Each change writes `admin_audit`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

import anyio

from ..config import Settings
from ..db import Database
from ..security import clock, ratelimit
from ..security.crypto import signing_keys
from . import accounts, passwords, sessions, totp

log = logging.getLogger(__name__)


class AdminError(Exception):
    """A refused admin operation; the message is safe to show to the operator."""


def audit(
    conn: sqlite3.Connection,
    admin_id: str,
    action: str,
    target_account_id: str | None,
    now: int,
    params: dict | None = None,
    ip_prefix: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO admin_audit (admin_id, action, target_account_id, params_json, ip_prefix, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (admin_id, action, target_account_id, json.dumps(params or {}, sort_keys=True), ip_prefix, now),
    )


def grant_admin(db: Database, settings: Settings, email: str, now: int | None = None) -> accounts.Account:
    email_norm = accounts.normalize_email(email)
    at = clock.now() if now is None else now

    def transaction(conn: sqlite3.Connection) -> accounts.Account:
        account = accounts.get_by_email(conn, email_norm)
        if account is None:
            raise AdminError("no account uses that email address")
        if email_norm not in settings.owner_emails:
            raise AdminError("that email address is not listed in STARLING_OWNER_EMAILS")
        if not account.email_verified:
            raise AdminError("the account has not verified its email address")
        if account.status != "active":
            raise AdminError(f"the account is not active (status {account.status})")
        conn.execute("UPDATE accounts SET is_admin = 1 WHERE id = ?", (account.id,))
        audit(conn, "cli", "admin_grant", account.id, at)
        granted = accounts.get_by_id(conn, account.id)
        assert granted is not None
        return granted

    account = db.write(transaction)
    log.info("admin granted account=%s", account.id)
    return account


def revoke_admin(db: Database, settings: Settings, email: str, now: int | None = None) -> accounts.Account:
    email_norm = accounts.normalize_email(email)
    at = clock.now() if now is None else now

    def transaction(conn: sqlite3.Connection) -> accounts.Account:
        account = accounts.get_by_email(conn, email_norm)
        if account is None:
            raise AdminError("no account uses that email address")
        conn.execute(
            "UPDATE accounts SET is_admin = 0, totp_secret_enc = NULL, totp_enabled_at = NULL WHERE id = ?", (account.id,)
        )
        conn.execute("UPDATE web_sessions SET admin_elevated_until = NULL WHERE account_id = ?", (account.id,))
        audit(conn, "cli", "admin_revoke", account.id, at)
        return account

    account = db.write(transaction)
    log.info("admin revoked account=%s", account.id)
    return account


# --------------------------------------------------------------------------- TOTP enrollment


def pending_totp_secret(db: Database, settings: Settings, account_id: str) -> str:
    """The base32 secret being enrolled; created on first view and kept until confirmed."""
    with db.read() as conn:
        account = accounts.get_by_id(conn, account_id)
    if account is None:
        raise AdminError("account not found")
    if account.totp_enabled_at is not None:
        raise AdminError("two-factor verification is already set up")
    existing, _ = totp.decrypt_with_any(signing_keys(settings), account.id, account.totp_secret_enc)
    if existing:
        return existing
    secret = totp.generate_secret()
    encrypted = totp.encrypt_secret(settings.secret_key, account.id, secret)
    db.write(
        lambda conn: conn.execute(
            "UPDATE accounts SET totp_secret_enc = ? WHERE id = ? AND totp_enabled_at IS NULL", (encrypted, account.id)
        )
    )
    return secret


async def pending_totp_secret_async(db: Database, settings: Settings, account_id: str) -> str:
    return await anyio.to_thread.run_sync(pending_totp_secret, db, settings, account_id)


async def _totp_limit(db: Database, account_id: str) -> str | None:
    decision = await ratelimit.check(db, [(ratelimit.TOTP_PER_ACCOUNT, ratelimit.account_key(account_id))])
    if decision.allowed:
        return None
    return f"Too many attempts. Try again in {max(1, -(-decision.retry_after_s // 60))} minutes."


async def confirm_totp_enrollment(
    db: Database, settings: Settings, *, account_id: str, password: str, code: str, ip_prefix: str
) -> str | None:
    """Enable TOTP after checking the password and a current code; returns an error message or None."""
    limited = await _totp_limit(db, account_id)
    if limited:
        return limited
    with db.read() as conn:
        account = accounts.get_by_id(conn, account_id)
    if account is None or account.totp_enabled_at is not None:
        return "Two-factor verification is already set up."
    checked = await passwords.verify_password_async(settings, account.password_hash, password[: passwords.MAX_LENGTH + 1])
    secret, needs_reencrypt = totp.decrypt_with_any(signing_keys(settings), account.id, account.totp_secret_enc)
    now = clock.now()
    step = totp.matching_step(totp.decode_secret(secret), code, now) if secret else None
    if not checked.ok or step is None or secret is None:
        log.info("admin totp enrollment failed account=%s", account.id)
        return "Your password or code is incorrect."
    stored = totp.encrypt_secret(settings.secret_key, account.id, secret) if needs_reencrypt else account.totp_secret_enc

    def transaction(conn: sqlite3.Connection) -> str | None:
        if not totp.claim_step(conn, account.id, step):
            return "That code was already used. Wait for the next code."
        updated = conn.execute(
            "UPDATE accounts SET totp_enabled_at = ?, totp_secret_enc = ? "
            "WHERE id = ? AND totp_enabled_at IS NULL AND totp_secret_enc = ?",
            (now, stored, account.id, account.totp_secret_enc),
        ).rowcount
        if updated != 1:
            return "The setup key changed. Reload the page and try again."
        audit(conn, account.id, "admin_totp_enrolled", account.id, now, ip_prefix=ip_prefix)
        return None

    return await db.write_async(transaction)


# --------------------------------------------------------------------------- elevation


@dataclass
class ElevationResult:
    error: str | None
    session_id: str | None = None
    session: sessions.WebSession | None = None


async def elevate(
    db: Database,
    settings: Settings,
    *,
    account_id: str,
    session_id_hash: str,
    code: str,
    ip_prefix: str,
    user_agent: str | None,
) -> ElevationResult:
    """Verify a TOTP code and rotate the session with 15 minutes of admin elevation."""
    limited = await _totp_limit(db, account_id)
    if limited:
        return ElevationResult(limited)
    with db.read() as conn:
        account = accounts.get_by_id(conn, account_id)
    if account is None or account.totp_enabled_at is None:
        return ElevationResult("Set up two-factor verification first.")
    secret, needs_reencrypt = totp.decrypt_with_any(signing_keys(settings), account.id, account.totp_secret_enc)
    now = clock.now()
    step = totp.matching_step(totp.decode_secret(secret), code, now) if secret else None
    if step is None or secret is None:
        log.info("admin elevation failed account=%s", account.id)
        return ElevationResult("That code is incorrect.")
    reencrypted = totp.encrypt_secret(settings.secret_key, account.id, secret) if needs_reencrypt else None

    def transaction(conn: sqlite3.Connection):
        live = sessions.still_valid(conn, session_id_hash, now)
        if live is None or live.account_id != account.id:
            return "Your session ended. Sign in again."
        if not totp.claim_step(conn, account.id, step):
            return "That code was already used. Wait for the next code."
        if reencrypted is not None:
            conn.execute(
                "UPDATE accounts SET totp_secret_enc = ? WHERE id = ? AND totp_secret_enc = ?",
                (reencrypted, account.id, account.totp_secret_enc),
            )
        new_id, rotated = sessions.rotate(
            conn, live, now=now, ip_prefix=ip_prefix, user_agent=user_agent,
            admin_elevated_until=now + sessions.ADMIN_ELEVATION_S,
        )
        audit(conn, account.id, "admin_elevate", account.id, now, ip_prefix=ip_prefix)
        return new_id, rotated

    outcome = await db.write_async(transaction)
    if isinstance(outcome, str):
        return ElevationResult(outcome)
    log.info("admin elevated account=%s", account.id)
    return ElevationResult(None, outcome[0], outcome[1])
