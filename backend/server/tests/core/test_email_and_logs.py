"""Email delivery (outbox and SMTP) and log hygiene across the account flows (CONTRACTS 13.6)."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import smtplib
from types import SimpleNamespace

import pytest

from starling_server.config import load_settings
from starling_server.db import Database
from starling_server.email import lifecycle, sender

from ..conftest import base_env
from .helpers import (
    NEW_PASSWORD,
    PASSWORD,
    approve_connect,
    connect_params,
    link_token,
    login,
    outbox,
    pkce_pair,
    post_form,
    redeem,
    signup,
)


@pytest.fixture
def smtp_settings(tmp_path):
    env = base_env(tmp_path / "smtp-data")
    env.update({"EMAIL_MODE": "smtp", "SMTP_HOST": "smtp.example.test", "SMTP_FROM": "Starling <no-reply@example.test>"})
    return load_settings(env)


@pytest.fixture
def smtp_db(smtp_settings):
    db = Database(smtp_settings.control_db_path)
    try:
        yield db
    finally:
        db.close()


def outbox_row(db, row_id):
    with db.read() as conn:
        return conn.execute("SELECT * FROM email_outbox WHERE id = ?", (row_id,)).fetchone()


def test_outbox_mode_stores_rows_and_eml_files(database, settings):
    sender.send_email(database, settings, "ana@example.com", "unit_test", "Hello", "Body with a link")
    rows = sender.outbox_messages(database, "ANA@example.com")
    assert len(rows) == 1 and rows[0]["sent_at"] is not None and rows[0]["body"] == "Body with a link"
    files = list(settings.outbox_dir.glob("*.eml"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "To: ana@example.com" in content and "Subject: Hello" in content and "Body with a link" in content


def test_header_injection_is_refused(database, settings):
    with pytest.raises(sender.EmailRefused):
        sender.send_email(database, settings, "ana@example.com\r\nBcc: x@example.org", "unit_test", "Hi", "Body")
    with pytest.raises(sender.EmailRefused):
        sender.send_email(database, settings, "ana@example.com", "unit_test", "Hi\nBcc: x@example.org", "Body")


def test_production_refuses_outbox_mode(tmp_path, database):
    env = base_env(tmp_path / "data")
    env.update({"STARLING_ENV": "production", "EMAIL_MODE": "outbox"})
    with pytest.raises(sender.EmailRefused):
        sender.send_email(database, load_settings(env), "ana@example.com", "unit_test", "Hi", "Body")


def test_smtp_delivery_retries_with_backoff_and_deletes_the_body(smtp_db, smtp_settings):
    now = 1_789_000_000
    queued = smtp_db.write(lambda c: sender.queue_email(c, smtp_settings, "ana@example.com", "unit_test", "Hi", "link body", now))
    attempts: list[str] = []

    def failing(settings, message):
        attempts.append(message["To"])
        raise smtplib.SMTPServerDisconnected("connection lost")

    assert sender.deliver_pending(smtp_db, smtp_settings, now=now, send=failing) == 0
    row = outbox_row(smtp_db, queued.id)
    assert row["attempts"] == 1 and row["body"] == "link body" and row["last_error"] == "SMTPServerDisconnected"
    assert sender.deliver_pending(smtp_db, smtp_settings, now=now + 1, send=failing) == 0
    assert len(attempts) == 1  # not due again yet

    delivered = []
    assert sender.deliver_pending(smtp_db, smtp_settings, now=sender.next_attempt_at(now, 1),
                                  send=lambda s, m: delivered.append((m["To"], m.get_body(("plain",)).get_content()))) == 1
    row = outbox_row(smtp_db, queued.id)
    assert row["sent_at"] is not None and row["body"] is None
    assert delivered[0][0] == "ana@example.com" and "link body" in delivered[0][1]


def test_smtp_gives_up_after_max_attempts_and_deletes_the_body(smtp_db, smtp_settings):
    now = 1_789_000_000
    queued = smtp_db.write(lambda c: sender.queue_email(c, smtp_settings, "ana@example.com", "unit_test", "Hi", "link body", now))

    def failing(settings, message):
        raise OSError("unreachable")

    for attempt in range(sender.MAX_ATTEMPTS):
        sender.deliver_pending(smtp_db, smtp_settings, now=sender.next_attempt_at(now, attempt), send=failing)
    row = outbox_row(smtp_db, queued.id)
    assert row["attempts"] == sender.MAX_ATTEMPTS and row["body"] is None and row["sent_at"] is None


def test_smtp_transport_uses_starttls_login_and_timeouts(smtp_settings, monkeypatch):
    calls: list[tuple] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit",))

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, context):
            calls.append(("starttls", context is not None))

        def login(self, user, secret):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append(("send", message["To"]))

    monkeypatch.setattr(sender.smtplib, "SMTP", FakeSMTP)
    settings = dataclasses.replace(smtp_settings, smtp_username="mailer", smtp_password="pw")
    sender.smtp_send(settings, sender.build_message(settings, "ana@example.com", "Hi", "Body"))
    assert calls[0] == ("connect", "smtp.example.test", 587, sender.SMTP_TIMEOUT_S)
    assert calls.index(("starttls", True)) < calls.index(("login", "mailer")) < calls.index(("send", "ana@example.com"))
    plain_production = dataclasses.replace(settings, smtp_starttls=False, env="production")
    with pytest.raises(sender.EmailRefused):
        sender.smtp_send(plain_production, sender.build_message(settings, "ana@example.com", "Hi", "Body"))


def test_background_sender_runs_only_in_smtp_mode(smtp_settings, settings):
    async def started(config) -> bool:
        db = Database(config.control_db_path)
        app = SimpleNamespace(state=SimpleNamespace(settings=config, db=db))
        try:
            await lifecycle.startup(app)
            running = getattr(app.state, "email_sender_task", None) is not None
            await lifecycle.shutdown(app)
            return running
        finally:
            db.close()

    assert asyncio.run(started(smtp_settings)) is True
    assert asyncio.run(started(settings)) is False


def test_logs_never_contain_passwords_tokens_codes_or_bodies(caplog, client, second_client, settings, fake_state):
    # Test-client request logs (httpx) are not server logs. `set_level` also sets the capture
    # handler's level, so the DEBUG call must come last.
    caplog.set_level(logging.WARNING, logger="httpx")
    caplog.set_level(logging.WARNING, logger="httpcore")
    caplog.set_level(logging.DEBUG)
    secrets_used = [PASSWORD, NEW_PASSWORD, "wrong horse battery"]

    assert signup(client, "ana@example.com").status_code == 303
    verify_token = link_token(client, "ana@example.com", "/verify-email")
    assert post_form(client, "/verify-email", {"token": verify_token}).status_code == 303
    assert login(second_client, "ana@example.com", "wrong horse battery").status_code == 400
    assert login(client, "ana@example.com").status_code == 303
    secrets_used += [verify_token, client.cookies.get(settings.session_cookie_name)]

    verifier, challenge = pkce_pair()
    code, _ = approve_connect(client, connect_params(challenge))
    issued = redeem(client, code, verifier)
    assert issued.status_code == 200
    assert redeem(client, code, verifier).status_code == 400  # replay is logged as a refusal
    secrets_used += [verifier, code, issued.json()["token"]]

    assert post_form(second_client, "/forgot-password", {"email": "ana@example.com"}).status_code == 200
    reset_token = link_token(second_client, "ana@example.com", "/reset-password")
    assert post_form(second_client, "/reset-password", {"token": reset_token, "password": NEW_PASSWORD}).status_code == 303
    secrets_used.append(reset_token)
    bodies = [message["body"] for message in outbox(client, "ana@example.com")]

    text = caplog.text
    assert "signup account=" in text and "device connected" in text and "device token refused" in text  # capture works
    for value in secrets_used:
        assert value and value not in text
    for body in bodies:
        assert body not in text
    assert "ana@example.com" not in text
