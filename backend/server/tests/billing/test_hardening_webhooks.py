"""Adversarial webhook intake: nothing an attacker can send may crash the endpoint.

The route is unauthenticated, so every rejection has to be a shaped 4xx (or 503 when the
endpoint is not configured) and nothing may be stored. A 500 here would both hide the real
reason from the owner and make Stripe retry a request that can never succeed.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from starling_server.app import create_app
from starling_server.billing import processor, signatures, webhooks
from starling_server.config import load_settings
from starling_server.security import clock

from ..conftest import PUBLIC_URL
from .conftest import WEBHOOK_SECRET, billing_env, make_account, make_client
from .test_webhooks import an_event, stored


def intake(db, settings, body, header, now):
    """Intake that turns any leaked exception into a failure the test can name."""
    try:
        return webhooks.intake(db, settings, body, header, now)
    except Exception as exc:  # a leaked exception is a 500 on the live route
        pytest.fail(f"webhook intake raised {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- signature header


def test_malformed_signature_headers_are_shaped_400_never_a_crash(db, bsettings, fake):
    body, header = fake.delivery(an_event(fake), WEBHOOK_SECRET)
    good = header.split(",")[1]
    headers = [
        "",
        "   ",
        "v1=deadbeef",  # no timestamp
        f"t={fake.now}",  # no signature
        f"t={fake.now},t={fake.now},{good}",  # two timestamps
        f"t=-1,{good}",
        f"t=,{good}",
        f"t=abc,{good}",
        f"t=1.5,{good}",
        # "²".isdigit() is True but int() refuses it: the guard and the parse disagree.
        f"t=²,{good}",
        f"t=١٢٣,{good}",  # Arabic-Indic digits: isdigit() and int() both accept
        f"t={'9' * 5000},{good}",  # beyond CPython's int-from-string digit limit
        f"t={fake.now},v1=",  # empty signature value
        f"t={fake.now},v1=" + "z" * 9000,  # oversized header
        f"t={fake.now}," + ",".join(["v1=00"] * 17),  # more signatures than allowed
        f"t={fake.now},v0={good.split('=')[1]}",  # v0 only: downgrade attempt
        "\x00\x01\x02",
    ]
    for candidate in headers:
        result = intake(db, bsettings, body, candidate, fake.now)
        assert result.status_code == 400, f"header {candidate[:40]!r} gave {result.status_code}"
        assert result.body["error"]["code"] == "bad_signature", candidate[:40]
    assert stored(db) == []
    assert intake(db, bsettings, body, header, fake.now).status_code == 200  # control


def test_unreadable_extra_elements_beside_a_valid_signature_are_ignored(db, bsettings, fake):
    """Stripe skips header elements it does not recognise, so an added one must not matter."""
    body, header = fake.delivery(an_event(fake), WEBHOOK_SECRET)
    for extra in ("\udcff", "junk", "v2=ff", "scheme", "=", " "):
        assert intake(db, bsettings, body, f"{header},{extra}", fake.now).status_code == 200, extra


def test_tolerance_boundaries_hold_in_both_directions(db, bsettings, fake):
    event = an_event(fake)
    tolerance = signatures.DEFAULT_TOLERANCE_S
    for offset, expected in ((-tolerance, 200), (tolerance, 200), (-tolerance - 1, 400), (tolerance + 1, 400)):
        body, header = fake.delivery(an_event(fake), WEBHOOK_SECRET, timestamp=fake.now + offset)
        assert intake(db, bsettings, body, header, fake.now).status_code == expected, offset
    # The timestamp is part of the signed message, so it cannot be moved into the window.
    body, header = fake.delivery(event, WEBHOOK_SECRET, timestamp=fake.now - 10_000)
    moved = header.replace(f"t={fake.now - 10_000}", f"t={fake.now}")
    assert intake(db, bsettings, body, moved, fake.now).status_code == 400


def test_one_altered_byte_anywhere_in_the_body_is_rejected(db, bsettings, fake):
    body, header = fake.delivery(an_event(fake), WEBHOOK_SECRET)
    for index in range(0, len(body), 7):
        altered = bytearray(body)
        altered[index] = altered[index] ^ 0x01
        result = intake(db, bsettings, bytes(altered), header, fake.now)
        assert result.status_code == 400 and result.body["error"]["code"] == "bad_signature"
    assert stored(db) == []


def test_a_valid_signature_is_found_among_many_and_the_wrong_secret_never_passes(db, bsettings, fake):
    body, header = fake.delivery(an_event(fake), WEBHOOK_SECRET)
    timestamp, real = header.split(",")[0], header.split("v1=")[1]
    crowded = ",".join([timestamp] + ["v1=" + "ab" * 32] * 15 + [f"v1={real}"])
    assert intake(db, bsettings, body, crowded, fake.now).status_code == 200
    wrong = ",".join([timestamp] + [f"v1={signatures.compute_signature(body, 'whsec_' + 'q' * 40, int(timestamp[2:]))}"])
    assert intake(db, bsettings, body, wrong, fake.now).status_code == 400


# --------------------------------------------------------------------------- bodies


def test_bodies_that_are_not_stripe_events_are_refused_without_crashing(db, bsettings, fake):
    bodies = [
        b"",
        b"{",
        b"null",
        b"[]",
        b'"a string"',
        b"123",
        b'{"id":"evt_1"}',
        b'{"id":"evt_1","type":"invoice.paid","livemode":false,"data":{}}',
        b'{"id":"evt_1","type":"invoice.paid","livemode":false,"data":{"object":[]}}',
        b'{"id":"in_1","type":"invoice.paid","livemode":false,"data":{"object":{}}}',  # not an evt_ id
        b'{"id":"evt_' + b"9" * 300 + b'","type":"invoice.paid","livemode":false,"data":{"object":{}}}',
        b'{"id":"evt_1","type":"invoice.paid","livemode":"no","data":{"object":{}}}',
        b'{"id":"evt_1","type":7,"livemode":false,"data":{"object":{}}}',
        b'{"id":"evt_1","type":"invoice.paid","livemode":false,"data":{"object":{}}}\xff',
        "{}".encode("utf-16"),  # valid JSON bytes in the wrong encoding
        # Nested far past the parser's recursion limit but well under the size cap.
        b'{"id":"evt_1","type":"invoice.paid","livemode":false,"data":{"object":{"id":'
        + b"[" * 100_000 + b"]" * 100_000 + b"}}}",
    ]
    for raw in bodies:
        header = signatures.sign_header(raw, WEBHOOK_SECRET, fake.now)
        result = intake(db, bsettings, raw, header, fake.now)
        assert result.status_code == 400, f"{raw[:40]!r} gave {result.status_code}"
        assert result.body["error"]["code"] in ("bad_request", "bad_signature"), raw[:40]
    assert stored(db) == []


def test_a_body_at_and_over_the_cap_is_refused_before_any_work(db, bsettings, fake):
    filler = "x" * (webhooks.MAX_BODY_BYTES + 10)
    event = an_event(fake)
    event["data"]["object"]["note"] = filler
    raw = json.dumps(event).encode("utf-8")
    assert len(raw) > webhooks.MAX_BODY_BYTES
    header = signatures.sign_header(raw, WEBHOOK_SECRET, fake.now)
    result = intake(db, bsettings, raw, header, fake.now)
    assert result.status_code == 413 and result.body["error"]["code"] == "payload_too_large"
    assert stored(db) == []


def test_route_answers_4xx_for_hostile_requests_and_never_500(tmp_path, fake, monkeypatch):
    settings = load_settings(billing_env(tmp_path / "data"))
    app = create_app(settings)
    app.state.stripe_client = make_client(settings, fake)
    app.state.billing_background_jobs = False
    monkeypatch.setattr(clock, "now", lambda: fake.now)
    body, header = fake.delivery(an_event(fake), WEBHOOK_SECRET)
    attempts = [
        # Sent as raw bytes: a real client can put any byte in a header, and the ASGI server
        # decodes it latin-1, so "\xb2" arrives as the character int() refuses.
        (body, {"stripe-signature": b"t=\xb2," + header.split(",")[1].encode("ascii")}, 400),
        (body, {"stripe-signature": f"t={'7' * 6000},v1=00"}, 400),
        (body, {}, 400),
        (b"not json at all", {"stripe-signature": signatures.sign_header(b"not json at all", WEBHOOK_SECRET, fake.now)}, 400),
        (b"x" * (webhooks.MAX_BODY_BYTES + 1), {"stripe-signature": header}, 413),
        (body, {"stripe-signature": header, "content-length": "nonsense"}, 413),
    ]
    with TestClient(app, base_url=PUBLIC_URL) as client:
        for raw, extra, expected in attempts:
            response = client.post("/webhooks/stripe", content=raw, headers={"content-type": "application/json", **extra})
            assert response.status_code == expected, (extra, response.status_code, response.text[:200])
            assert "error" in response.json()
        ok = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": header})
        assert ok.status_code == 200
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM billing_events").fetchone()[0] == 1


# --------------------------------------------------------------------------- replay and ordering


def test_the_same_event_processed_from_two_threads_at_once_runs_once(billing):
    account = make_account(billing.db)
    billing.fake.complete_checkout(billing.checkout(account).session_id)
    billing.deliver()
    event_ids = [row["event_id"] for row in billing.all("SELECT event_id FROM billing_events ORDER BY rowid")]
    assert event_ids
    outcomes: list[str] = []
    start = threading.Barrier(8)

    def run(event_id):
        start.wait()
        outcomes.append(processor.process_event(billing.db, billing.settings, billing.client, event_id, billing.now))

    for event_id in event_ids:
        outcomes.clear()
        threads = [threading.Thread(target=run, args=(event_id,)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert set(outcomes) <= {"processed", "ignored", "processing", "failed"}, (event_id, outcomes)
        # `attempts` counts claims, so it is the one measure of how often the work really ran.
        row = billing.one("SELECT attempts, status FROM billing_events WHERE event_id = ?", event_id)
        assert row["attempts"] == 1, (event_id, outcomes)
        assert row["status"] in ("processed", "ignored"), (event_id, row["status"])
    attempts = [row["attempts"] for row in billing.all("SELECT attempts FROM billing_events")]
    assert attempts == [1] * len(event_ids)
    assert len(billing.all(
        "SELECT id FROM entitlement_periods WHERE account_id = ? AND source = 'subscription'", account)) == 1
    assert billing.entitlement(account).allowance == 250


def test_unknown_customers_and_subscriptions_are_acknowledged_not_applied(billing):
    events = [
        an_event(billing.fake, "invoice.paid", {"id": "in_absent", "object": "invoice", "customer": "cus_absent"}),
        an_event(billing.fake, "customer.subscription.updated",
                 {"id": "sub_absent", "object": "subscription", "customer": "cus_absent"}),
        an_event(billing.fake, "checkout.session.completed",
                 {"id": "cs_test_absent", "object": "checkout.session", "customer": "cus_absent"}),
        an_event(billing.fake, "customer.deleted", {"id": "cus_absent", "object": "customer"}),
    ]
    for event in events:
        body, header = billing.fake.delivery(event, WEBHOOK_SECRET)
        assert intake(billing.db, billing.settings, body, header, billing.now).status_code == 200
    billing.process()
    statuses = {row["type"]: row["status"] for row in billing.all("SELECT type, status FROM billing_events")}
    assert statuses["customer.subscription.updated"] == "processed"  # refetch 404s: mirrored as gone
    assert statuses["checkout.session.completed"] == "ignored"
    assert statuses["customer.deleted"] == "ignored"
    # An invoice Stripe no longer has cannot be applied; it is retried and then alerted, never
    # applied to a guessed account.
    assert statuses["invoice.paid"] == "failed"
    assert billing.all("SELECT * FROM subscriptions") == []
    assert billing.all("SELECT * FROM entitlement_periods WHERE source = 'subscription'") == []
    assert billing.alerts("unknown_checkout_session")


def test_an_event_from_the_other_mode_is_refused_even_with_a_valid_signature(db, bsettings, fake):
    live = replace(bsettings, stripe_mode="live")
    body, header = fake.delivery(an_event(fake, livemode=False), WEBHOOK_SECRET)
    result = intake(db, live, body, header, fake.now)
    assert result.status_code == 400 and result.body["error"]["code"] == "livemode_mismatch"
    assert stored(db) == []
