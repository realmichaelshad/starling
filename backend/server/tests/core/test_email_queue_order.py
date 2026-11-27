"""A sign-up code queued while older mail waits out its retries goes out at once."""
from __future__ import annotations

from starling_server.email import sender
from starling_server.security import clock


def test_new_mail_is_not_stuck_behind_old_retries(database, settings):
    now = clock.now()

    def queue(conn, n):
        for i in range(n):
            conn.execute(
                "INSERT INTO email_outbox (to_email, template, subject, body, created_at, attempts) "
                "VALUES (?, 'old', 's', 'b', ?, 1)",
                (f"old{i}@example.com", now),
            )

    database.write(lambda conn: queue(conn, sender.BATCH_SIZE + 5))  # all waiting out a 30 s backoff
    database.write(lambda conn: conn.execute(
        "INSERT INTO email_outbox (to_email, template, subject, body, created_at, attempts) "
        "VALUES ('new@example.com', 'verify_email', 'Your Starling code: 123 456', 'b', ?, 0)", (now,)))
    delivered: list[str] = []
    sent = sender.deliver_pending(database, settings, now=now, send=lambda s, message: delivered.append(message["To"]))
    assert sent == 1 and delivered == ["new@example.com"]
