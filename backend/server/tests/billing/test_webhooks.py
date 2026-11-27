"""Webhook intake: signatures, mode, idempotent storage and the unauthenticated route."""
from __future__ import annotations

import threading
from dataclasses import replace

from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.billing import mirror, signatures, webhooks
from starling_server.config import load_settings
from starling_server.security import clock

from ..conftest import PUBLIC_URL
from .conftest import PREVIOUS_WEBHOOK_SECRET, WEBHOOK_SECRET, billing_env, make_client


def an_event(fake, event_type="invoice.paid", obj=None, livemode=False):
    return {
        "id": fake.new_id("evt"), "object": "event", "type": event_type, "livemode": livemode, "created": fake.now,
        "data": {"object": obj or {"id": "in_00000001", "object": "invoice", "customer": "cus_00000001"}},
    }


def stored(db):
    with db.read() as conn:
        return conn.execute("SELECT event_id, status, attempts FROM billing_events ORDER BY rowid").fetchall()


def test_event_is_stored_once_and_a_processed_duplicate_does_no_work(db, bsettings, fake):
    event = an_event(fake)
    body, header = fake.delivery(event, WEBHOOK_SECRET)
    first = webhooks.intake(db, bsettings, body, header, fake.now)
    assert (first.status_code, first.enqueue, first.body) == (200, True, {"received": True})
    again = webhooks.intake(db, bsettings, body, header, fake.now)
    assert again.status_code == 200 and again.body.get("duplicate")
    assert len(stored(db)) == 1
    db.write(lambda conn: conn.execute("UPDATE billing_events SET status = 'processed' WHERE event_id = ?", (event["id"],)))
    late = webhooks.intake(db, bsettings, body, header, fake.now)
    assert late.status_code == 200 and not late.enqueue
    assert [(row["status"], row["attempts"]) for row in stored(db)] == [("processed", 0)]


def test_concurrent_deliveries_store_one_row(db, bsettings, fake):
    body, header = fake.delivery(an_event(fake), WEBHOOK_SECRET)
    results = []
    threads = [threading.Thread(target=lambda: results.append(webhooks.intake(db, bsettings, body, header, fake.now)))
               for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [result.status_code for result in results] == [200] * 12
    assert sum(1 for result in results if not result.body.get("duplicate")) == 1
    assert len(stored(db)) == 1


def test_bad_signatures_are_rejected_and_nothing_is_stored(db, bsettings, fake):
    event = an_event(fake)
    body, header = fake.delivery(event, WEBHOOK_SECRET)
    attempts = [
        (body, signatures.sign_header(body, "whsec_" + "z" * 32, fake.now)),
        (body.replace(b"invoice.paid", b"invoice.void"), header),
        fake.delivery(event, WEBHOOK_SECRET, timestamp=fake.now - 301),
        fake.delivery(event, WEBHOOK_SECRET, timestamp=fake.now + 301),
        (body, None),
    ]
    for raw, signature in attempts:
        result = webhooks.intake(db, bsettings, raw, signature, fake.now)
        assert result.status_code == 400 and result.body["error"]["code"] == "bad_signature"
    assert stored(db) == []
    assert webhooks.intake(db, bsettings, body, header, fake.now).status_code == 200  # control


def test_previous_secret_is_accepted_while_rotating(db, bsettings, fake):
    body, header = fake.delivery(an_event(fake), PREVIOUS_WEBHOOK_SECRET)
    assert webhooks.intake(db, bsettings, body, header, fake.now).status_code == 400
    rotated = replace(bsettings, stripe_webhook_secret_previous=PREVIOUS_WEBHOOK_SECRET)
    assert webhooks.intake(db, rotated, body, header, fake.now).status_code == 200
    both_body, both_header = fake.delivery(an_event(fake), [PREVIOUS_WEBHOOK_SECRET, WEBHOOK_SECRET])
    assert webhooks.intake(db, bsettings, both_body, both_header, fake.now).status_code == 200


def test_live_event_on_a_test_endpoint_is_rejected(db, bsettings, fake):
    body, header = fake.delivery(an_event(fake, livemode=True), WEBHOOK_SECRET)
    result = webhooks.intake(db, bsettings, body, header, fake.now)
    assert result.status_code == 400 and result.body["error"]["code"] == "livemode_mismatch"
    assert stored(db) == []
    with db.read() as conn:
        assert [alert["kind"] for alert in mirror.list_alerts(conn)] == ["livemode_mismatch"]
    body, header = fake.delivery(an_event(fake, livemode=False), WEBHOOK_SECRET)
    assert webhooks.intake(db, bsettings, body, header, fake.now).status_code == 200  # control


def test_unconfigured_secret_unsubscribed_type_and_non_events(db, bsettings, fake):
    body, header = fake.delivery(an_event(fake), WEBHOOK_SECRET)
    assert webhooks.intake(db, replace(bsettings, stripe_webhook_secret=""), body, header, fake.now).status_code == 503
    other_body, other_header = fake.delivery(an_event(fake, "customer.updated", {"id": "cus_1", "object": "customer"}), WEBHOOK_SECRET)
    result = webhooks.intake(db, bsettings, other_body, other_header, fake.now)
    assert result.status_code == 200 and result.body.get("ignored") and not result.enqueue
    junk = b'{"hello":"world"}'
    assert webhooks.intake(db, bsettings, junk, signatures.sign_header(junk, WEBHOOK_SECRET, fake.now), fake.now).status_code == 400
    assert stored(db) == []


def test_route_accepts_signed_events_without_a_session_and_queues_them(tmp_path, fake, monkeypatch):
    settings = load_settings(billing_env(tmp_path / "data"))
    app = create_app(settings)
    app.state.stripe_client = make_client(settings, fake)
    app.state.billing_background_jobs = False
    monkeypatch.setattr(clock, "now", lambda: fake.now)
    event = an_event(fake, "checkout.session.expired", {"id": "cs_test_00000099", "object": "checkout.session", "customer": None})
    body, header = fake.delivery(event, WEBHOOK_SECRET)
    with TestClient(app, base_url=PUBLIC_URL) as client:
        accepted = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": header, "content-type": "application/json"})
        assert accepted.status_code == 200 and accepted.json() == {"received": True}
        assert app.state.billing_queue.drain(10)
        rejected = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": "t=1,v1=00", "content-type": "application/json"})
        assert rejected.status_code == 400
        oversized = client.post("/webhooks/stripe", content=b"x" * (webhooks.MAX_BODY_BYTES + 1), headers={"stripe-signature": header})
        assert oversized.status_code == 413
    with app.state.db.read() as conn:
        row = conn.execute("SELECT status FROM billing_events WHERE event_id = ?", (event["id"],)).fetchone()
    assert row["status"] == "ignored"  # processed by the queue: not a session this service created
