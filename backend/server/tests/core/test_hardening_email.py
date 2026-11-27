"""Adversarial cover for outgoing email: headers, what a body may carry, and delivery failure.

Nothing here talks to a real SMTP server; the transport is a stand-in that can be made to fail.
"""
from __future__ import annotations

import pytest

from starling_server.config import load_settings
from starling_server.db import Database
from starling_server.email import sender
from starling_server.email import templates as mail

from ..conftest import PUBLIC_URL, base_env
from .helpers import PASSWORD, account_row, create_user, outbox, post_form, signup


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


CRLF_CASES = [
    "victim@example.com\r\nBcc: attacker@example.com",
    "victim@example.com\nBcc: attacker@example.com",
    "victim@example.com\rSubject: hijacked",
    "victim@example.com\r\n\r\nInjected body",
]


# --------------------------------------------------------------------------- headers


@pytest.mark.parametrize("value", CRLF_CASES)
def test_a_recipient_with_a_line_break_is_refused_before_it_reaches_a_header(database, settings, value):
    with pytest.raises(sender.EmailRefused):
        database.write(lambda conn: sender.queue_email(conn, settings, value, "verify_email", "Subject", "Body", 1))
    with pytest.raises(sender.EmailRefused):
        sender.build_message(settings, value, "Subject", "Body")


@pytest.mark.parametrize("value", ["Hijack\r\nBcc: attacker@example.com", "Hijack\nX-Header: x", "Hijack\r"])
def test_a_subject_with_a_line_break_is_refused(database, settings, value):
    with pytest.raises(sender.EmailRefused):
        database.write(lambda conn: sender.queue_email(conn, settings, "ana@example.com", "t", value, "Body", 1))
    with pytest.raises(sender.EmailRefused):
        sender.build_message(settings, "ana@example.com", value, "Body")


def test_a_body_can_never_reach_the_headers(settings):
    """A body is the payload, not headers: line breaks in it must stay in the payload."""
    body = "Hello\r\n\r\nBcc: attacker@example.com\r\nSubject: hijacked\r\n"
    message = sender.build_message(settings, "ana@example.com", "Real subject", body)
    assert message["Subject"] == "Real subject"
    assert message["Bcc"] is None
    assert message.get_content().startswith("Hello")
    raw = bytes(message).decode("utf-8")
    header_block = raw.split("\r\n\r\n", 1)[0] if "\r\n\r\n" in raw else raw.split("\n\n", 1)[0]
    assert "attacker@example.com" not in header_block


def test_an_address_that_could_carry_a_header_never_reaches_the_queue(app, client):
    """The address validator is the first gate: sign-up refuses anything a header could not hold."""
    for value in CRLF_CASES:
        assert signup(client, value).status_code == 400
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 0


def test_the_notice_about_a_new_address_only_shows_a_masked_form(app, client, fake_state):
    """The old mailbox is told that an address change was requested, not which address."""
    create_user(client, "ana@example.com")
    assert post_form(client, "/account/security/email",
                     {"new_email": "secret.person@example.com", "email_password": PASSWORD}).status_code == 303
    notice = [m for m in outbox(client, "ana@example.com") if m["template"] == "email_change_notice"][-1]
    assert "s***@example.com" in notice["body"]
    assert "secret.person@example.com" not in notice["body"]


def test_no_email_body_carries_a_password(app, client, fake_state):
    create_user(client, "ana@example.com")
    post_form(client, "/account/security/email", {"new_email": "new@example.com", "email_password": PASSWORD})
    post_form(client, "/forgot-password", {"email": "ana@example.com"})
    with app.state.db.read() as conn:
        bodies = [row[0] or "" for row in conn.execute("SELECT body FROM email_outbox")]
    assert bodies
    for body in bodies:
        assert PASSWORD not in body


# --------------------------------------------------------------------------- delivery failure


def test_a_sign_up_survives_a_failing_outbox_writer(app, client, monkeypatch):
    """The account row is already committed when delivery is handed off. A hand-off that fails
    must be logged, not turned into a 500 that tells the customer sign-up did not work."""
    def explode(settings, queued):
        raise OSError("outbox directory is not writable")

    monkeypatch.setattr(sender, "_write_outbox_file", explode)
    response = signup(client, "ana@example.com")
    assert response.status_code == 303
    assert account_row(app, "ana@example.com") is not None
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 1


def test_a_password_reset_request_survives_a_failing_outbox_writer(app, client, second_client, monkeypatch):
    create_user(client, "ana@example.com")

    def explode(settings, queued):
        raise OSError("disk full")

    monkeypatch.setattr(sender, "_write_outbox_file", explode)
    response = post_form(second_client, "/forgot-password", {"email": "ana@example.com"})
    assert response.status_code == 200
    assert "Traceback" not in response.text and "OSError" not in response.text


def test_a_failed_hand_off_is_not_reported_to_the_visitor(app, client, monkeypatch, caplog):
    monkeypatch.setattr(sender, "_write_outbox_file", lambda settings, queued: (_ for _ in ()).throw(OSError("nope")))
    with caplog.at_level("WARNING"):
        assert signup(client, "ana@example.com").status_code == 303
    assert any("email" in record.getMessage().lower() for record in caplog.records)


def test_delivery_errors_never_escape_the_sender(smtp_db, smtp_settings):
    def always_fails(settings, message):
        raise RuntimeError("smtp is down")

    sender.send_email(smtp_db, smtp_settings, "ana@example.com", "verify_email", "Subject", "Body")
    assert sender.deliver_pending(smtp_db, smtp_settings, send=always_fails) == 0
    with smtp_db.read() as conn:
        row = conn.execute("SELECT attempts, last_error, sent_at FROM email_outbox").fetchone()
    assert row["attempts"] == 1 and row["sent_at"] is None and "RuntimeError" in row["last_error"]


def test_the_error_note_never_carries_the_message_text(smtp_db, smtp_settings):
    secret = "https://starling.example/verify-email?token=SECRET"

    def fails_with_the_body(settings, message):
        raise RuntimeError(secret)

    sender.send_email(smtp_db, smtp_settings, "ana@example.com", "verify_email", "Subject", secret)
    sender.deliver_pending(smtp_db, smtp_settings, send=fails_with_the_body)
    with smtp_db.read() as conn:
        last_error = conn.execute("SELECT last_error FROM email_outbox").fetchone()[0]
    assert "SECRET" not in last_error and last_error.startswith("RuntimeError")


# --------------------------------------------------------------------------- templates


def test_every_template_link_is_built_from_the_configured_origin(settings):
    contents = [
        mail.verify_email(settings, f"{PUBLIC_URL}/verify-email?token=t"),
        mail.signup_existing(settings),
        mail.password_reset(settings, f"{PUBLIC_URL}/reset-password?token=t"),
        mail.password_reset_notice(settings),
        mail.password_changed_notice(settings),
        mail.email_change_confirm(settings, f"{PUBLIC_URL}/verify-email?token=t"),
        mail.email_change_notice(settings, "a***@example.com", f"{PUBLIC_URL}/reset-password?token=t"),
        mail.email_in_use_notice(settings),
    ]
    for content in contents:
        assert content.template and content.subject and "\n" not in content.subject
        for line in content.body.splitlines():
            if line.startswith("http"):
                assert line.startswith(PUBLIC_URL), content.template
        assert settings.support_email in content.body
