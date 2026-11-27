"""Owner alerts: the three stored formats, the derived ones, the emails and the background pass.

Each writer owns its own shape and none of them agree, so `alerts.normalize` is the single place that
turns them into {level, text, created_at, key}. The values below are written exactly as
`billing/mirror.py raise_alert`, `engines/pool.py _write_alert` and `services/metering.py record_alert`
write them, including the engine key's dot.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from starling_server.admin import alerts, lifecycle
from starling_server.billing import mirror
from starling_server.db import request_errors

from .conftest import add_request_error, add_service_state, service_state

DAY = 86_400
NOW = 1_800_000_000


def collect(database, settings, now=NOW, **kwargs):
    with database.read() as conn:
        return alerts.collect(conn, settings, now, **kwargs)


@pytest.fixture
def no_disk_alert(monkeypatch):
    """A roomy volume, so only the alerts under test appear."""
    monkeypatch.setattr(alerts, "disk_status", lambda settings: {"path": "/data", "total_bytes": 100,
                                                                 "free_bytes": 90, "free_pct": 90.0})


# --------------------------------------------------------------------------- the three stored formats


def test_a_billing_alert_is_normalized_from_what_mirror_writes(database, settings, no_disk_alert):
    key = database.write(lambda conn: mirror.raise_alert(conn, "price_missing", "price_ABC/1", "A price id is unknown.",
                                                         NOW - 60, {"price_id": "price_ABC"}))
    assert key == "alert:billing:price_missing:price_ABC_1"
    [item] = collect(database, settings)
    assert item["key"] == key and item["level"] == "warning" and item["created_at"] == NOW - 60
    assert item["text"] == "Billing: A price id is unknown. Reference price_ABC_1."

    database.write(lambda conn: mirror.raise_alert(conn, "price_missing", "price_ABC/1", "A price id is unknown.",
                                                   NOW - 30, {"price_id": "price_ABC"}))
    [again] = collect(database, settings)
    assert again["created_at"] == NOW - 30 and again["text"].endswith("Seen 2 times.")


@pytest.mark.parametrize("kind", ["price_missing", "duplicate_subscriptions_canceled", "livemode_mismatch",
                                  "unknown_customer", "unknown_price", "subscription_account_mismatch",
                                  "duplicate_subscriptions", "unknown_checkout_session", "checkout_customer_mismatch",
                                  "invoice_without_period", "partial_refund", "dispute_without_subscription",
                                  "dispute_lost", "event_retries_exhausted"])
def test_every_billing_alert_kind_normalizes(database, settings, no_disk_alert, kind):
    database.write(lambda conn: mirror.raise_alert(conn, kind, None, f"Something about {kind}.", NOW - 10))
    [item] = collect(database, settings)
    assert item["key"] == f"alert:billing:{kind}:-"
    assert item["text"] == f"Billing: Something about {kind}."  # ref "-" is not shown
    assert item["level"] in alerts.LEVELS


def test_an_engine_circuit_alert_uses_the_dotted_key(database, settings, no_disk_alert):
    add_service_state(database, "alert.engine_circuit.42", {
        "kind": "engine_circuit_open", "severity": "warning", "account_id": "acct-1", "tenant_seq": 42,
        "crashes": 3, "window_s": 600, "opened_at": NOW - 120, "retry_after_s": 300}, NOW - 120)
    [item] = collect(database, settings)
    assert item["key"] == "alert.engine_circuit.42" and item["level"] == "warning"
    assert item["created_at"] == NOW - 120
    assert item["text"] == ("The engine for tenant 42 crashed 3 times within 10 minutes; "
                            "new starts are paused for 300 seconds.")
    assert "acct-1" not in item["text"]  # the alert text carries metadata, not identifiers


def test_an_ai_key_rejection_normalizes_and_never_shows_key_material(database, settings, no_disk_alert):
    add_service_state(database, "alert:ai_key_rejected",
                      {"at": NOW - 300, "key": "primary", "upstream_status": 401, "retried_with": "secondary"},
                      NOW - 300)
    [item] = collect(database, settings)
    assert item["level"] == "error" and item["created_at"] == NOW - 300
    assert item["text"] == ("The AI provider rejected the primary API key (HTTP 401); requests were retried with "
                            "the secondary key. Check the keys in the services environment.")

    add_service_state(database, "alert:ai_key_rejected",
                      {"at": NOW - 100, "key": "secondary", "upstream_status": 403, "retried_with": None}, NOW - 100)
    [item] = collect(database, settings)
    assert "no other key was available" in item["text"]


def test_unreadable_and_unknown_alerts_still_show_up(database, settings, no_disk_alert):
    add_service_state(database, "alert:billing:unknown_price:price_X", "{not json", NOW - 10)
    add_service_state(database, "alert:something_new", {"level": "error", "message": "A new alert.", "at": NOW - 20},
                      NOW - 20)
    add_service_state(database, "alert:ai_other", {"at": NOW - 30}, NOW - 30)
    found = {item["key"]: item for item in collect(database, settings)}
    assert "could not be read" in found["alert:billing:unknown_price:price_X"]["text"]
    assert found["alert:something_new"]["text"] == "A new alert." and found["alert:something_new"]["level"] == "error"
    assert found["alert:ai_other"]["text"] == "AI service alert: ai_other."
    assert all(item["level"] in alerts.LEVELS for item in found.values())


def test_alerts_are_newest_first_and_old_ones_drop_out_of_the_window(database, settings, no_disk_alert):
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW - 100, "key": "primary"}, NOW - 100)
    add_service_state(database, "alert.engine_circuit.7", {"severity": "warning", "tenant_seq": 7,
                                                           "opened_at": NOW - 50}, NOW - 50)
    add_service_state(database, "alert.engine_circuit.8", {"severity": "warning", "tenant_seq": 8,
                                                           "opened_at": NOW - 8 * DAY}, NOW - 8 * DAY)
    keys = [item["key"] for item in collect(database, settings)]
    assert keys == ["alert.engine_circuit.7", "alert:ai_key_rejected"]  # the 8-day-old one is outside the window
    assert len(collect(database, settings, recent_s=9 * DAY)) == 3
    # Rows that are not alerts are never read.
    add_service_state(database, "backup:last", {"at": NOW, "ok": True}, NOW)
    assert len(collect(database, settings)) == 2


# --------------------------------------------------------------------------- backups


def test_backup_status_hides_the_folder_and_survives_a_broken_row(database, settings):
    with database.read() as conn:
        assert alerts.backup_status(conn) is None
    add_service_state(database, "backup:last", {"at": NOW - 3_600, "ok": True, "archive": "/data/backups/a.tar.age",
                                                "size_bytes": 5, "verified": True, "error": None}, NOW - 3_600)
    with database.read() as conn:
        assert alerts.backup_status(conn) == {"at": NOW - 3_600, "ok": True, "archive": "a.tar.age", "size_bytes": 5,
                                              "verified": True, "error": None}
    add_service_state(database, "backup:last", "[]", NOW - 10)
    with database.read() as conn:
        broken = alerts.backup_status(conn)
    assert broken["ok"] is False and "could not be read" in broken["error"]


def test_backup_alerts_only_fire_in_production(database, settings, no_disk_alert):
    production = dataclasses.replace(settings, env="production")
    add_service_state(database, "backup:last", {"at": NOW - 3_600, "ok": False, "error": "the passphrase is not set"},
                      NOW - 3_600)
    assert collect(database, settings) == []  # development: nothing, however bad the backup is
    [failed] = collect(database, production)
    assert failed["key"] == alerts.BACKUP_FAILED_KEY and failed["level"] == "error"
    assert "the passphrase is not set" in failed["text"]

    add_service_state(database, "backup:last", {"at": NOW - 27 * 3_600, "ok": True}, NOW - 27 * 3_600)
    keys = {item["key"] for item in collect(database, production)}
    assert keys == {alerts.BACKUP_MISSING_KEY}
    add_service_state(database, "backup:last", {"at": NOW - 3_600, "ok": True}, NOW - 3_600)
    assert collect(database, production) == []  # the negative control: a fresh, successful run


def test_a_server_with_no_backup_at_all_is_only_alerted_after_a_day(database, settings):
    production = dataclasses.replace(settings, env="production")
    database.write(lambda conn: conn.execute("UPDATE schema_migrations SET applied_at = ? WHERE version = 1",
                                             (NOW - 3_600,)))
    with database.read() as conn:
        assert alerts.backup_alerts(conn, production, NOW) == []
    database.write(lambda conn: conn.execute("UPDATE schema_migrations SET applied_at = ? WHERE version = 1",
                                             (NOW - 30 * 3_600,)))
    with database.read() as conn:
        [missing] = alerts.backup_alerts(conn, production, NOW)
    assert missing["key"] == alerts.BACKUP_MISSING_KEY and "set up more than 26 hours ago" in missing["text"]


# --------------------------------------------------------------------------- emails


class Mailbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, db, settings, to_email, template, subject, body) -> None:
        self.sent.append((to_email, subject, body))


def with_alert_email(settings, address="owner@example.com"):
    return dataclasses.replace(settings, alert_email=address)


def test_nothing_is_emailed_without_alert_email(database, settings, no_disk_alert):
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW - 10, "key": "primary"}, NOW - 10)
    mailbox = Mailbox()
    assert settings.alert_email == ""
    assert alerts.notify(database, settings, NOW, send=mailbox) == []
    assert mailbox.sent == []
    assert service_state(database, alerts.ALERT_NOTIFICATIONS_KEY) is None

    # The negative control: the same alert with ALERT_EMAIL set is emailed.
    assert alerts.notify(database, with_alert_email(settings), NOW, send=mailbox) == ["alert:ai_key_rejected"]
    assert len(mailbox.sent) == 1 and mailbox.sent[0][0] == "owner@example.com"


def test_each_alert_is_emailed_once_every_six_hours(database, settings, no_disk_alert):
    config = with_alert_email(settings)
    mailbox = Mailbox()
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW, "key": "primary"}, NOW)

    assert alerts.notify(database, config, NOW, send=mailbox) == ["alert:ai_key_rejected"]
    assert alerts.notify(database, config, NOW + 60, send=mailbox) == []  # same alert, nothing new
    assert alerts.notify(database, config, NOW + alerts.RESEND_AFTER_S + 1, send=mailbox) == []  # still not re-fired

    # It fires again: after the 6 hours have passed, that earns one more email.
    later = NOW + alerts.RESEND_AFTER_S + 100
    add_service_state(database, "alert:ai_key_rejected", {"at": later, "key": "primary"}, later)
    assert alerts.notify(database, config, later, send=mailbox) == ["alert:ai_key_rejected"]
    assert len(mailbox.sent) == 2

    # A re-fire inside the 6 hours waits.
    sooner = later + 60
    add_service_state(database, "alert:ai_key_rejected", {"at": sooner, "key": "primary"}, sooner)
    assert alerts.notify(database, config, sooner, send=mailbox) == []
    assert len(mailbox.sent) == 2

    log = service_state(database, alerts.ALERT_NOTIFICATIONS_KEY)
    assert log["alert:ai_key_rejected"] == {"sent_at": later, "fired_at": later}


def test_one_email_covers_every_due_alert_and_links_to_the_dashboard(database, settings, no_disk_alert):
    config = with_alert_email(settings)
    mailbox = Mailbox()
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW - 10, "key": "primary"}, NOW - 10)
    database.write(lambda conn: mirror.raise_alert(conn, "dispute_lost", "sub_1", "A dispute was lost.", NOW - 20))

    assert sorted(alerts.notify(database, config, NOW, send=mailbox)) == ["alert:ai_key_rejected",
                                                                         "alert:billing:dispute_lost:sub_1"]
    [(to_email, subject, body)] = mailbox.sent
    assert to_email == "owner@example.com" and subject == "Starling: 2 owner alerts"
    assert "rejected the primary API key" in body and "A dispute was lost." in body
    assert f"{settings.public_url}/admin" in body


def test_the_notifier_forgets_alerts_that_have_gone_away(database, settings, no_disk_alert):
    config = with_alert_email(settings)
    mailbox = Mailbox()
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW, "key": "primary"}, NOW)
    assert alerts.notify(database, config, NOW, send=mailbox) == ["alert:ai_key_rejected"]
    database.write(lambda conn: conn.execute("DELETE FROM service_state WHERE key = 'alert:ai_key_rejected'"))
    assert alerts.notify(database, config, NOW + 7 * 3_600, send=mailbox) == []
    assert service_state(database, alerts.ALERT_NOTIFICATIONS_KEY) == {}

    # It is a new alert next time it fires, so it is emailed again straight away.
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW + 8 * 3_600, "key": "primary"}, NOW + 8 * 3_600)
    assert alerts.notify(database, config, NOW + 8 * 3_600, send=mailbox) == ["alert:ai_key_rejected"]


def test_the_real_sender_queues_one_email(database, settings, no_disk_alert):
    """`email.sender` is the delivery path; here it must land in `email_outbox` exactly once."""
    config = with_alert_email(settings)
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW, "key": "primary"}, NOW)
    assert alerts.notify(database, config, NOW) == ["alert:ai_key_rejected"]
    with database.read() as conn:
        rows = [dict(row) for row in conn.execute("SELECT to_email, template, subject, body FROM email_outbox")]
    assert len(rows) == 1 and rows[0]["to_email"] == "owner@example.com"
    assert rows[0]["template"] == alerts.EMAIL_TEMPLATE and "owner alert" in rows[0]["subject"]


def test_a_malformed_notification_log_is_ignored_rather_than_trusted(database, settings, no_disk_alert):
    config = with_alert_email(settings)
    add_service_state(database, alerts.ALERT_NOTIFICATIONS_KEY,
                      {"alert:ai_key_rejected": "not a dict", "other": {"sent_at": "soon", "fired_at": 1}}, NOW)
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW, "key": "primary"}, NOW)
    mailbox = Mailbox()
    assert alerts.notify(database, config, NOW, send=mailbox) == ["alert:ai_key_rejected"]
    assert list(service_state(database, alerts.ALERT_NOTIFICATIONS_KEY)) == ["alert:ai_key_rejected"]


# --------------------------------------------------------------------------- the background pass


def test_the_daily_prune_removes_only_expired_request_errors(database, settings, no_disk_alert):
    add_request_error(database, route="/api/v1/assist", code="busy", http_status=409,
                      at=NOW - request_errors.RETENTION_S - 10)
    add_request_error(database, route="/api/v1/assist", code="busy", http_status=409, at=NOW - 60)

    assert lifecycle.prune_if_due(database, NOW) == 1
    with database.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_errors").fetchone()[0] == 1
    assert service_state(database, lifecycle.PRUNED_AT_KEY) == NOW

    add_request_error(database, route="/api/v1/assist", code="busy", http_status=409,
                      at=NOW - request_errors.RETENTION_S - 10)
    assert lifecycle.prune_if_due(database, NOW + lifecycle.PRUNE_EVERY_S - 1) is None  # not due yet
    with database.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_errors").fetchone()[0] == 2
    assert lifecycle.prune_if_due(database, NOW + lifecycle.PRUNE_EVERY_S) == 1


def test_one_background_pass_emails_and_prunes(database, settings, no_disk_alert):
    config = with_alert_email(settings)
    add_service_state(database, "alert:ai_key_rejected", {"at": NOW, "key": "primary"}, NOW)
    add_request_error(database, route="/api/v1/assist", code="busy", http_status=409,
                      at=NOW - request_errors.RETENTION_S - 10)

    report = lifecycle.run_once(database, config, NOW)
    assert report == {"notified": ["alert:ai_key_rejected"], "pruned": 1}
    assert lifecycle.run_once(database, config, NOW + 60) == {"notified": [], "pruned": None}


def test_a_failing_pass_is_logged_and_never_raises(database, settings, monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(alerts, "notify", explode)
    monkeypatch.setattr(lifecycle, "prune_if_due", explode)
    with caplog.at_level("ERROR"):
        assert lifecycle.run_once(database, settings, NOW) == {"notified": [], "pruned": None}
    assert "owner alert email failed" in caplog.text and "request_errors prune failed" in caplog.text


def test_startup_can_be_switched_off_and_shutdown_is_safe_either_way(app):
    """`app.py` will call these two hooks; neither may leave a task behind or close the db under one."""
    import asyncio

    async def scenario():
        app.state.admin_background_jobs = False
        await lifecycle.startup(app)
        assert getattr(app.state, "admin_jobs_task", None) is None
        await lifecycle.shutdown(app)  # nothing to stop

        app.state.admin_background_jobs = True
        await lifecycle.startup(app)
        task = app.state.admin_jobs_task
        assert task is not None and not task.done()
        await lifecycle.shutdown(app)
        assert task.done() and app.state.admin_jobs_task is None

    asyncio.run(scenario())


def test_the_cli_alerts_command_prints_what_the_dashboard_shows(database, settings, monkeypatch, capsys,
                                                               no_disk_alert):
    import argparse

    from starling_server.admin import commands
    from starling_server.security import clock

    add_service_state(database, "alert:ai_key_rejected", {"at": NOW - 60, "key": "primary", "upstream_status": 401},
                      NOW - 60)
    monkeypatch.setattr(commands, "get_settings", lambda: settings)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    parser = argparse.ArgumentParser()
    commands.register(parser.add_subparsers(dest="command", required=True))

    listing = parser.parse_args(["alerts"])
    assert listing.func(listing) == 0
    text = capsys.readouterr().out
    assert "ERROR" in text and "rejected the primary API key" in text

    as_json = parser.parse_args(["alerts", "--json"])
    assert as_json.func(as_json) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["key"] for item in payload] == ["alert:ai_key_rejected"]

    database.write(lambda conn: conn.execute("DELETE FROM service_state"))
    empty = parser.parse_args(["alerts"])
    assert empty.func(empty) == 0 and "No owner alerts." in capsys.readouterr().out
