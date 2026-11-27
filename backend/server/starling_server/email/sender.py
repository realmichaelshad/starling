"""Transactional email. Interface fixed by CONTRACTS section 14; implemented by the server core workstream.

`send_email` (and `queue_email` inside a caller's writer transaction) stores the message in
`email_outbox`:

- `EMAIL_MODE=smtp`: the background sender (`email.lifecycle`) delivers queued rows over SMTP
  (STARTTLS or implicit TLS on port 465, timeouts), retries with backoff, and deletes the body
  once the message is sent or given up.
- `EMAIL_MODE=outbox` (development and e2e only): the row is marked sent and the message is also
  written as an `.eml` file under `settings.outbox_dir`; `GET /_dev/outbox?to=` reads the rows.
  Production refuses outbox mode here as well as in the configuration check.

Logs carry the template name and a masked address only, never bodies, links or tokens.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import sqlite3
import ssl
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable
from urllib.parse import urlsplit

import anyio

from ..config import Settings
from ..db import Database
from ..security import clock
from .templates import EmailContent

log = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 20
MAX_ATTEMPTS = 8
BATCH_SIZE = 20
BACKOFF_BASE_S = 30
BACKOFF_MAX_S = 3600
SENT_RETENTION_S = 30 * 86400
# Messages whose row is removed as soon as it is sent or given up: the goodbye to a deleted
# account must not keep its address in the outbox for the retention period.
EPHEMERAL_TEMPLATES = frozenset({"account_deleted"})

_wake = threading.Event()


class EmailRefused(RuntimeError):
    """The message cannot be queued or sent with this configuration."""


@dataclass(frozen=True)
class QueuedEmail:
    id: int
    to_email: str
    template: str
    subject: str
    body: str
    created_at: int


def mask_address(address: str) -> str:
    local, _, domain = address.rpartition("@")
    return f"{local[:1]}***@{domain}" if local else "***"


def _single_line(value: str, name: str) -> str:
    if "\r" in value or "\n" in value:
        raise EmailRefused(f"email {name} must be a single line")
    return value


def queue_email(
    conn: sqlite3.Connection,
    settings: Settings,
    to_email: str,
    template: str,
    subject: str,
    body: str,
    now: int,
) -> QueuedEmail:
    """Insert a message inside the caller's writer transaction; call `after_commit` afterwards."""
    if settings.email_mode == "outbox" and settings.is_production:
        raise EmailRefused("EMAIL_MODE=outbox is not allowed in production")
    _single_line(to_email, "recipient")
    _single_line(subject, "subject")
    sent_at = now if settings.email_mode == "outbox" else None
    row_id = conn.execute(
        "INSERT INTO email_outbox (to_email, template, subject, body, created_at, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
        (to_email, template, subject, body, now, sent_at),
    ).fetchone()[0]
    return QueuedEmail(int(row_id), to_email, template, subject, body, now)


def queue_content(conn: sqlite3.Connection, settings: Settings, to_email: str, content: EmailContent, now: int) -> QueuedEmail:
    return queue_email(conn, settings, to_email, content.template, content.subject, content.body, now)


def after_commit(settings: Settings, queued: list[QueuedEmail]) -> None:
    """Hand committed messages to delivery: `.eml` files in outbox mode, a wake-up in SMTP mode."""
    for message in queued:
        log.info("email queued id=%d template=%s to=%s", message.id, message.template, mask_address(message.to_email))
    if not queued:
        return
    if settings.email_mode == "outbox":
        for message in queued:
            try:
                _write_outbox_file(settings, message)
            except Exception as exc:
                # The committed row is the record of the message; the file is a convenience for
                # development. A directory that cannot be written must not turn a sign-up or a
                # password reset that already happened into a 500 for the visitor.
                log.warning("email outbox file not written id=%d: %s", message.id, type(exc).__name__)
    else:
        _wake.set()


async def after_commit_async(settings: Settings, queued: list[QueuedEmail]) -> None:
    if queued:
        await anyio.to_thread.run_sync(after_commit, settings, queued)


def send_email(db: Database, settings: Settings, to_email: str, template: str, subject: str, body: str) -> None:
    """Queue an email. SMTP mode delivers it; outbox mode (development and e2e only) stores it for tests."""
    now = clock.now()
    queued = db.write(lambda conn: queue_email(conn, settings, to_email, template, subject, body, now))
    after_commit(settings, [queued])


# --------------------------------------------------------------------------- message building


def _mail_domain(settings: Settings) -> str:
    if "@" in settings.smtp_from:
        return settings.smtp_from.rpartition("@")[2].strip(" >") or "localhost"
    return urlsplit(settings.public_url).hostname or "localhost"


def _reply_to(settings: Settings) -> str | None:
    """Replies go to a person: the support address, once it is a real one."""
    address = (settings.support_email or "").strip()
    if not address or address.startswith("[[") or "@" not in address or any(c in address for c in "\r\n"):
        return None
    return address


def build_message(settings: Settings, to_email: str, subject: str, body: str, template: str | None = None) -> EmailMessage:
    """The message: plain text first, and an HTML alternative in Starling's colours when the template
    is known (email/html.py), so a text-only client shows exactly the words it always did."""
    message = EmailMessage()
    message["From"] = settings.smtp_from or f"Starling <no-reply@{_mail_domain(settings)}>"
    message["To"] = _single_line(to_email, "recipient")
    message["Subject"] = _single_line(subject, "subject")
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=_mail_domain(settings))
    message["Auto-Submitted"] = "auto-generated"
    reply_to = _reply_to(settings)
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)
    if template:
        from . import html as email_html

        message.add_alternative(email_html.render(template, subject, body), subtype="html")
    return message


def _write_outbox_file(settings: Settings, queued: QueuedEmail) -> None:
    directory = settings.outbox_dir
    directory.mkdir(parents=True, exist_ok=True)
    message = build_message(settings, queued.to_email, queued.subject, queued.body, queued.template)
    path = directory / f"{queued.created_at}-{queued.id:06d}-{queued.template}.eml"
    path.write_bytes(bytes(message))


def outbox_messages(db: Database, to_email: str) -> list[dict[str, Any]]:
    """Messages for one recipient, oldest first (development outbox route)."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, to_email, template, subject, body, created_at, sent_at FROM email_outbox "
            "WHERE lower(to_email) = lower(?) ORDER BY id",
            (to_email.strip(),),
        ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- SMTP delivery


def smtp_send(settings: Settings, message: EmailMessage) -> None:
    implicit_tls = settings.smtp_port == 465
    if not implicit_tls and not settings.smtp_starttls and settings.is_production:
        raise EmailRefused("SMTP without TLS is not allowed in production")
    context = ssl.create_default_context()
    if implicit_tls:
        client: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_S, context=context)
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_S)
    with client:
        client.ehlo()
        if not implicit_tls and settings.smtp_starttls:
            client.starttls(context=context)
            client.ehlo()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


def next_attempt_at(created_at: int, attempts: int) -> int:
    delay = sum(min(BACKOFF_BASE_S * 2 ** (n - 1), BACKOFF_MAX_S) for n in range(1, attempts + 1))
    return created_at + delay


def _error_label(exc: Exception) -> str:
    label = type(exc).__name__
    if isinstance(exc, smtplib.SMTPResponseException):
        label += f" {exc.smtp_code}"
    return label[:200]


def deliver_pending(
    db: Database,
    settings: Settings,
    now: int | None = None,
    send: Callable[[Settings, EmailMessage], None] | None = None,
) -> int:
    """Send due SMTP messages; returns how many were sent. Blocking: run it in a thread."""
    at = clock.now() if now is None else now
    transport = send or smtp_send
    with db.read() as conn:
        # Due messages only, fresh ones first. The batch limit used to apply BEFORE the "due for a
        # retry" check, so twenty old messages waiting out their backoff (a mail server that was down
        # for an hour) kept every new sign-up code and reset link out of the batch for up to half an
        # hour. Waiting rows are few and small (id and two numbers), so the check runs here.
        waiting = conn.execute(
            "SELECT id, created_at, attempts FROM email_outbox "
            "WHERE sent_at IS NULL AND body IS NOT NULL AND attempts < ? ORDER BY attempts > 0, id",
            (MAX_ATTEMPTS,),
        ).fetchall()
        due = [int(r["id"]) for r in waiting if at >= next_attempt_at(int(r["created_at"]), int(r["attempts"]))][:BATCH_SIZE]
        rows = conn.execute(
            "SELECT id, to_email, template, subject, body, created_at, attempts FROM email_outbox "
            f"WHERE id IN ({','.join('?' * len(due))}) ORDER BY attempts > 0, id",
            due,
        ).fetchall() if due else []
    sent = 0
    for row in rows:
        try:
            transport(settings, build_message(settings, row["to_email"], row["subject"], row["body"], row["template"]))
        except Exception as exc:  # delivery problems are recorded and retried, never raised
            attempts = int(row["attempts"]) + 1
            label = _error_label(exc)
            give_up = attempts >= MAX_ATTEMPTS
            if give_up and row["template"] in EPHEMERAL_TEMPLATES:
                db.write(lambda c, rid=row["id"]: c.execute("DELETE FROM email_outbox WHERE id = ?", (rid,)))
            else:
                db.write(
                    lambda c, rid=row["id"]: c.execute(
                        "UPDATE email_outbox SET attempts = ?, last_error = ?, "
                        "body = CASE WHEN ? THEN NULL ELSE body END WHERE id = ?",
                        (attempts, label, 1 if give_up else 0, rid),
                    )
                )
            log.warning(
                "email delivery failed id=%d template=%s to=%s attempt=%d error=%s%s",
                row["id"], row["template"], mask_address(row["to_email"]), attempts, label,
                " (giving up)" if give_up else "",
            )
            continue
        if row["template"] in EPHEMERAL_TEMPLATES:
            db.write(lambda c, rid=row["id"]: c.execute("DELETE FROM email_outbox WHERE id = ?", (rid,)))
        else:
            db.write(
                lambda c, rid=row["id"]: c.execute(
                    "UPDATE email_outbox SET sent_at = ?, attempts = attempts + 1, body = NULL, last_error = NULL "
                    "WHERE id = ?",
                    (clock.now(), rid),
                )
            )
        sent += 1
        log.info("email sent id=%d template=%s", row["id"], row["template"])
    db.write(
        lambda c: c.execute(
            "DELETE FROM email_outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (at - SENT_RETENTION_S,)
        )
    )
    return sent


async def wait_for_work(timeout_s: float) -> None:
    """Sleep until new mail is queued in this process or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _wake.is_set():
            _wake.clear()
            return
        await asyncio.sleep(0.25)
