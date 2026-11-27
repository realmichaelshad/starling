"""Customer data export and account deletion.

Deletion order (each step is safe to repeat, so a failed deletion can be resumed):

1. mark the account `deleting`, raise `auth_epoch`, revoke every session, device and email token;
2. `billing.service.cancel_for_deletion(db, settings, account_id)` when the billing workstream
   provides it (sync or async);
3. `engine_pool.delete_data(account_id, tenant_seq)` removes the customer's engine data;
4. anonymise the account row (`deleted-<id>@invalid`, status `deleted`, unusable password), delete
   sessions, devices, email tokens, connect requests and outbox mail to the old address, and write a
   `tombstones` row: HMAC of the allowance key (keyed from STARLING_SECRET_KEY) plus the Stripe
   customer id.

If step 2 or 3 fails the account stays `deleting` (already signed out everywhere) and
`resume_pending_deletions` finishes it later.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import inspect
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import anyio

from ..auth import accounts, identities, sessions
from ..config import Settings
from ..db import Database
from ..devices import tokens as device_tokens
from ..email import sender
from ..email import templates as mail
from ..security import clock
from ..security.crypto import derive_key

log = logging.getLogger(__name__)

EXPORT_PATH = "/profile/export"
EXPORT_TIMEOUT_S = 60.0
BILLING_SERVICE = "starling_server.billing.service"


class DeletionError(Exception):
    """A deletion step failed; the account stays `deleting` and can be resumed."""


class ExportUnavailable(Exception):
    """The engine could not produce the export."""


async def export_data(pool: Any, account_id: str, tenant_seq: int) -> bytes:
    status, body = await pool.request(account_id, tenant_seq, "GET", EXPORT_PATH, b"", {}, EXPORT_TIMEOUT_S)
    if status != 200:
        raise ExportUnavailable(f"engine answered {status}")
    return body


def tombstone_hash(settings: Settings, allowance_key: str) -> str:
    return hmac.new(derive_key(settings.secret_key, "tombstone-v1"), allowance_key.encode("utf-8"), hashlib.sha256).hexdigest()


def begin_deletion(conn: sqlite3.Connection, account_id: str, now: int) -> accounts.Account | None:
    """Step 1 (writer transaction): mark `deleting` and sign out everything."""
    account = accounts.get_by_id(conn, account_id)
    if account is None or account.status == "deleted":
        return None
    conn.execute("UPDATE accounts SET status = 'deleting', is_admin = 0 WHERE id = ?", (account_id,))
    accounts.bump_auth_epoch(conn, account_id)
    sessions.revoke_all(conn, account_id, now)
    device_tokens.revoke_all(conn, account_id, now, "account_deleted")
    conn.execute("UPDATE email_tokens SET used_at = ? WHERE account_id = ? AND used_at IS NULL", (now, account_id))
    return accounts.get_by_id(conn, account_id)


async def cancel_billing(db: Database, settings: Settings, account_id: str) -> bool:
    """Step 2: call the billing workstream's cancellation; False when billing does not provide it."""
    try:
        module = importlib.import_module(BILLING_SERVICE)
    except ModuleNotFoundError as exc:
        if exc.name != BILLING_SERVICE:
            raise
        log.warning("billing.service is not installed; no subscription cancellation for account=%s", account_id)
        return False
    cancel = getattr(module, "cancel_for_deletion", None)
    if cancel is None:
        log.warning("billing.service.cancel_for_deletion is missing; no cancellation for account=%s", account_id)
        return False
    if inspect.iscoroutinefunction(cancel):
        await cancel(db, settings, account_id)
    else:
        await anyio.to_thread.run_sync(cancel, db, settings, account_id)
    return True


def anonymize(conn: sqlite3.Connection, settings: Settings, account_id: str, now: int,
              plan_name: str | None = None) -> list:
    """Step 4 (writer transaction). Returns the goodbye email to hand to delivery after the commit.

    The goodbye is queued AFTER the address's other outbox rows are removed, and its own row is
    removed as soon as it is sent (`sender.EPHEMERAL_TEMPLATES`), so the address does not stay behind.
    """
    account = accounts.get_by_id(conn, account_id)
    if account is None or account.status == "deleted":
        return []
    customer = conn.execute("SELECT customer_id FROM stripe_customers WHERE account_id = ?", (account_id,)).fetchone()
    customer_id = customer["customer_id"] if customer else None
    if customer_id and conn.execute("SELECT 1 FROM tombstones WHERE customer_id = ?", (customer_id,)).fetchone():
        customer_id = None
    conn.execute(
        "INSERT INTO tombstones (account_hash, customer_id, deleted_at) VALUES (?, ?, ?) "
        "ON CONFLICT (account_hash) DO UPDATE SET deleted_at = excluded.deleted_at, "
        "customer_id = COALESCE(tombstones.customer_id, excluded.customer_id)",
        (tombstone_hash(settings, account.allowance_key), customer_id, now),
    )
    conn.execute("DELETE FROM email_outbox WHERE lower(to_email) = lower(?)", (account.email,))
    conn.execute("DELETE FROM web_sessions WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM email_tokens WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM connect_requests WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM devices WHERE account_id = ?", (account_id,))
    # The link to the person's Google account is personal data too, and without it the Google
    # account could never make a new Starling account on the same address.
    identities.delete_all(conn, account_id)
    placeholder = f"deleted-{account_id}@invalid"
    conn.execute(
        "UPDATE accounts SET email = ?, email_norm = ?, allowance_key = ?, password_hash = '!', status = 'deleted', "
        "is_admin = 0, totp_secret_enc = NULL, totp_enabled_at = NULL, signup_ip_prefix = NULL, deleted_at = ? "
        "WHERE id = ?",
        (placeholder, placeholder, placeholder, now, account_id),
    )
    when = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%B %d, %Y").replace(" 0", " ")
    goodbye = mail.account_deleted(settings, when, plan_name)
    return [sender.queue_content(conn, settings, account.email, goodbye, now)]


async def delete_account(db: Database, settings: Settings, pool: Any, account_id: str,
                         plan_name: str | None = None) -> None:
    account = await db.write_async(lambda conn: begin_deletion(conn, account_id, clock.now()))
    if account is None:
        return
    try:
        await cancel_billing(db, settings, account_id)
        await pool.delete_data(account_id, account.tenant_seq)
    except Exception as exc:
        log.error("account deletion paused account=%s: %s", account_id, type(exc).__name__)
        raise DeletionError("account deletion could not finish") from exc
    queued = await db.write_async(lambda conn: anonymize(conn, settings, account_id, clock.now(), plan_name))
    await sender.after_commit_async(settings, queued)
    log.info("account deleted account=%s", account_id)


async def resume_pending_deletions(db: Database, settings: Settings, pool: Any) -> list[str]:
    """Finish deletions left in `deleting`; returns the account ids completed."""
    with db.read() as conn:
        pending = [row["id"] for row in conn.execute("SELECT id FROM accounts WHERE status = 'deleting'").fetchall()]
    done = []
    for account_id in pending:
        try:
            await delete_account(db, settings, pool, account_id)
        except DeletionError:
            continue
        done.append(account_id)
    return done
