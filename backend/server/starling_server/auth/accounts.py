"""Accounts: email validation and normalisation, the allowance key and account rows.

- `email_norm` is the trimmed, lowercased address (unique per account).
- `allowance_key` groups addresses that reach the same mailbox, so one person cannot collect
  several free allowances: lowercase, `+tag` removed, and for gmail.com / googlemail.com the dots
  removed and the domain unified to gmail.com. A step that would empty the local part is skipped,
  so `+a@host` and `+b@host` keep separate keys instead of both becoming `@host`.
- Addresses are limited to printable ASCII without spaces, which also keeps them safe to place in
  email headers.
- Helpers that take a `sqlite3.Connection` work both on read connections and inside writer
  transactions; the caller decides which.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from functools import lru_cache

from ..config import Settings

log = logging.getLogger(__name__)

MAX_EMAIL_LENGTH = 254
GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
# The password of an account that signs in with Google only. Not an argon2 hash, so it can never
# verify (`passwords.verify_password` refuses anything that is not one); `has_password` is False.
NO_PASSWORD = "!none"
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_TLD_RE = re.compile(r"^(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})$")

ACCOUNT_COLUMNS = (
    "id, tenant_seq, email, email_norm, allowance_key, email_verified_at, password_hash, "
    "password_changed_at, auth_epoch, status, is_admin, totp_secret_enc, totp_enabled_at, "
    "signup_ip_prefix, created_at, deleted_at"
)


class EmailError(ValueError):
    """The submitted address is not acceptable; the message is safe to show."""


@dataclass(frozen=True)
class Account:
    id: str
    tenant_seq: int
    email: str
    email_norm: str
    allowance_key: str
    email_verified_at: int | None
    password_hash: str
    password_changed_at: int
    auth_epoch: int
    status: str
    is_admin: bool
    totp_secret_enc: str | None
    totp_enabled_at: int | None
    signup_ip_prefix: str | None
    created_at: int
    deleted_at: int | None

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def can_sign_in(self) -> bool:
        return self.status in ("active", "suspended")

    @property
    def has_password(self) -> bool:
        """False for an account that signs in with Google only (and for closed accounts)."""
        return not self.password_hash.startswith("!")

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Account":
        return cls(
            id=row["id"],
            tenant_seq=int(row["tenant_seq"]),
            email=row["email"],
            email_norm=row["email_norm"],
            allowance_key=row["allowance_key"],
            email_verified_at=row["email_verified_at"],
            password_hash=row["password_hash"],
            password_changed_at=int(row["password_changed_at"]),
            auth_epoch=int(row["auth_epoch"]),
            status=row["status"],
            is_admin=bool(row["is_admin"]),
            totp_secret_enc=row["totp_secret_enc"],
            totp_enabled_at=row["totp_enabled_at"],
            signup_ip_prefix=row["signup_ip_prefix"],
            created_at=int(row["created_at"]),
            deleted_at=row["deleted_at"],
        )


# --------------------------------------------------------------------------- addresses


def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def validate_email(raw: object) -> str:
    """Return the trimmed address or raise `EmailError`."""
    email = raw.strip() if isinstance(raw, str) else ""
    if not email:
        raise EmailError("Enter your email address.")
    invalid = EmailError("Enter a valid email address, like name@example.com.")
    if len(email) > MAX_EMAIL_LENGTH or any(ord(ch) < 33 or ord(ch) > 126 for ch in email):
        raise invalid
    local, sep, domain = email.rpartition("@")
    if not sep or not local or not domain or len(local) > 64 or not _LOCAL_RE.match(local):
        raise invalid
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels) or not _TLD_RE.match(labels[-1]):
        raise invalid
    return email


def allowance_key(email_norm: str) -> str:
    local, _, domain = email_norm.rpartition("@")
    # `+tag` is a tag on a mailbox, but a local part that is *only* a tag ("+a@host") names a
    # mailbox of its own; stripping it would give every such address the same key, so one of
    # them would inherit another mailbox's deletion tombstone and lose its free allowance.
    local = local.split("+", 1)[0] or local
    if domain in GMAIL_DOMAINS:
        local = local.replace(".", "") or local
        domain = "gmail.com"
    return f"{local}@{domain}"


def mask_email(email: str) -> str:
    local, sep, domain = email.rpartition("@")
    if not sep or not local:
        return "***"
    return f"{local[:1]}***@{domain}"


@lru_cache(maxsize=4)
def _load_domains(path: str, mtime_ns: int) -> frozenset[str]:
    domains = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            item = line.split("#", 1)[0].strip().lower().rstrip(".")
            if item:
                domains.add(item)
    return frozenset(domains)


def disposable_domains(settings: Settings) -> frozenset[str]:
    path = settings.disposable_domains_file
    if path is None:
        return frozenset()
    try:
        return _load_domains(str(path), path.stat().st_mtime_ns)
    except OSError:
        log.warning("DISPOSABLE_DOMAINS_FILE could not be read; the blocklist is not applied")
        return frozenset()


def is_disposable(settings: Settings, email_norm: str) -> bool:
    blocked = disposable_domains(settings)
    if not blocked:
        return False
    parts = email_norm.rpartition("@")[2].split(".")
    return any(".".join(parts[i:]) in blocked for i in range(len(parts) - 1))


# --------------------------------------------------------------------------- rows


def get_by_id(conn: sqlite3.Connection, account_id: str) -> Account | None:
    row = conn.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return Account.from_row(row) if row else None


def get_by_email(conn: sqlite3.Connection, email_norm: str) -> Account | None:
    row = conn.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE email_norm = ?", (email_norm,)).fetchone()
    return Account.from_row(row) if row else None


def insert_account(conn: sqlite3.Connection, *, email: str, password_hash: str, ip_prefix: str | None, now: int) -> Account:
    """Create an account inside a writer transaction; `tenant_seq` is max + 1 under the write lock."""
    tenant_seq = int(conn.execute("SELECT COALESCE(MAX(tenant_seq), 0) + 1 FROM accounts").fetchone()[0])
    account_id = str(uuid.uuid4())
    email_norm = normalize_email(email)
    conn.execute(
        "INSERT INTO accounts (id, tenant_seq, email, email_norm, allowance_key, email_verified_at, password_hash, "
        "password_changed_at, auth_epoch, status, is_admin, signup_ip_prefix, created_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 1, 'active', 0, ?, ?)",
        (account_id, tenant_seq, email, email_norm, allowance_key(email_norm), password_hash, now, ip_prefix, now),
    )
    account = get_by_id(conn, account_id)
    assert account is not None
    return account


def bump_auth_epoch(conn: sqlite3.Connection, account_id: str) -> int:
    """Invalidate every session and device of the account; return the new epoch."""
    return int(
        conn.execute(
            "UPDATE accounts SET auth_epoch = auth_epoch + 1 WHERE id = ? RETURNING auth_epoch", (account_id,)
        ).fetchone()[0]
    )


def set_password_hash(conn: sqlite3.Connection, account_id: str, password_hash: str, now: int) -> None:
    conn.execute(
        "UPDATE accounts SET password_hash = ?, password_changed_at = ? WHERE id = ?", (password_hash, now, account_id)
    )


def mark_verified(conn: sqlite3.Connection, account_id: str, now: int) -> None:
    conn.execute(
        "UPDATE accounts SET email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?", (now, account_id)
    )


def release_unverified_email(conn: sqlite3.Connection, account_id: str, now: int) -> None:
    """Take an address away from an account that never confirmed it, and close that account.

    Used only when the address's real owner undoes an email change (`auth.service.reset_password`):
    the unconfirmed account was parked on the address to block the undo. It keeps its id, so nothing
    that refers to it dangles; it can no longer sign in, and its address becomes a value nobody can
    type or receive mail at.
    """
    placeholder = f"released-{account_id}@address.invalid"
    conn.execute(
        "UPDATE accounts SET email = ?, email_norm = ?, allowance_key = ?, password_hash = '!released', "
        "password_changed_at = ?, auth_epoch = auth_epoch + 1, status = 'suspended' "
        "WHERE id = ? AND email_verified_at IS NULL",
        (placeholder, placeholder, placeholder, int(now), account_id),
    )


def claim_for_google(conn: sqlite3.Connection, account_id: str, now: int) -> None:
    """Hand an UNCONFIRMED account to the person Google says owns its address (writer transaction).

    Whoever made the account never proved they receive the address's mail, and Google has just
    vouched that this person does. The old password goes, because it may belong to somebody who
    parked an account on another person's address in advance, waiting for the owner to arrive. The
    caller signs every existing browser and device out.
    """
    conn.execute(
        "UPDATE accounts SET password_hash = ?, password_changed_at = ?, email_verified_at = ?, "
        "auth_epoch = auth_epoch + 1 WHERE id = ? AND email_verified_at IS NULL",
        (NO_PASSWORD, int(now), int(now), account_id),
    )


def set_email(conn: sqlite3.Connection, account_id: str, email: str, now: int) -> None:
    """Switch the address (already proven by a link) and clear admin access (CLI re-grant needed)."""
    email_norm = normalize_email(email)
    conn.execute(
        "UPDATE accounts SET email = ?, email_norm = ?, allowance_key = ?, email_verified_at = ?, is_admin = 0, "
        "totp_secret_enc = NULL, totp_enabled_at = NULL WHERE id = ?",
        (email, email_norm, allowance_key(email_norm), now, account_id),
    )
    conn.execute("UPDATE web_sessions SET admin_elevated_until = NULL WHERE account_id = ?", (account_id,))
