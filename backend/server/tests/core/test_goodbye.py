"""Deleting an account ends on a page that says what happened, and an email that proves it."""
from __future__ import annotations

from .helpers import PASSWORD, context_value, create_user, outbox, post_form


def test_deleting_ends_on_the_goodbye_page_and_sends_a_confirmation(app, client, fake_pool, fake_state):
    create_user(client, "ana@example.com")
    response = post_form(client, "/account/delete", {"delete_password": PASSWORD, "delete_confirmation": "delete"})
    assert response.status_code == 303 and response.headers["location"] == "/goodbye"
    page = client.get("/goodbye")
    assert context_value(page, "email") == "ana@example.com"
    assert context_value(page, "pending") is False
    goodbye = [m for m in outbox(client, "ana@example.com") if m["template"] == "account_deleted"]
    assert len(goodbye) == 1 and "were deleted on" in goodbye[0]["body"]
    assert "{" not in goodbye[0]["body"]  # every placeholder filled in


def test_delete_accepts_the_word_in_any_case_but_nothing_else(client, fake_pool, fake_state):
    create_user(client, "ana@example.com")
    refused = post_form(client, "/account/delete", {"delete_password": PASSWORD, "delete_confirmation": "yes"})
    assert refused.status_code == 400
    assert context_value(refused, "errors") == {"delete_confirmation": "Type DELETE to confirm."}


def test_the_goodbye_page_without_a_deletion_goes_home(client):
    response = client.get("/goodbye", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


def test_the_goodbye_email_does_not_keep_the_address_once_sent(app, database, settings):
    from starling_server.email import sender
    from starling_server.email import templates as mail
    from starling_server.security import clock

    now = clock.now()
    database.write(lambda conn: sender.queue_content(conn, settings, "gone@example.com", mail.account_deleted(settings, "today", None), now))
    database.write(lambda conn: conn.execute("UPDATE email_outbox SET sent_at = NULL"))
    sender.deliver_pending(database, settings, now=now, send=lambda s, message: None)
    with database.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_outbox WHERE to_email = 'gone@example.com'").fetchone()[0] == 0
