"""Failure recording for the owner dashboard (`request_errors`): a stream records its own terminal
errors (the 200 response has already started), and the assist routes expose the validated mode and
the reservation id on `request.state` for the app-wide API error recorder."""
from __future__ import annotations

import uuid

import httpx

from .support import LiveServer, build_app, device_for, insert_account, make_pool, wait_until
from .test_streams import Stream, chat


def rows(database) -> list[dict]:
    with database.read() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT account_id, route, mode, code, http_status, reserved, count FROM request_errors ORDER BY code")]


def test_stream_errors_after_the_response_started_are_recorded(harness, fake_usage, slots, database):
    ok = Stream(harness, body=chat(tokens=["fine"]))
    assert ok.rest()[-1] == "final"
    failed = Stream(harness, body=chat(script="error", code="ai_rate_limited"))
    assert failed.rest()[-1] == "error"
    assert wait_until(lambda: rows(database), timeout=10)
    assert rows(database) == [{
        "account_id": harness.account_id("a"), "route": "/api/v1/assist/stream", "mode": "chat",
        "code": "ai_unavailable", "http_status": 503, "reserved": 1, "count": 1,
    }]  # the successful stream recorded nothing


def test_a_busy_stream_is_recorded_with_its_own_status(harness, fake_usage, slots, database):
    running = Stream(harness, body=chat(script="hang", hang_s=30))
    running.until("status")
    waiting = Stream(harness, body=chat(tokens=["later"]))
    waiting.until("queued")
    third = Stream(harness, body={"mode": "enhance_composer", "composer": {"action": "reply", "draft_text": "x"}})
    assert third.rest()[-1] == "error"
    with httpx.Client(trust_env=False) as client:
        client.post(harness.url(f"/api/v1/requests/{running.key}/cancel"), headers=harness.headers("a", str(uuid.uuid4())))
    running.rest()
    waiting.rest()
    assert wait_until(lambda: rows(database), timeout=10)
    assert [(r["code"], r["http_status"], r["mode"], r["reserved"]) for r in rows(database)] == [
        ("busy", 409, "enhance_composer", 1)]  # the cancelled and the queued request ended with a final


def test_errors_after_the_client_left_are_not_recorded(harness, fake_usage, slots, database):
    stream = Stream(harness, body=chat(script="error", start_delay_s=1.0))
    stream.next()  # accepted
    stream.close()
    assert wait_until(lambda: slots.events and not slots.held(harness.account_id("a")), timeout=15)
    assert wait_until(lambda: fake_usage.only_row()["state"] == "released", timeout=10)
    assert rows(database) == []


def test_assist_routes_expose_mode_and_reservation_to_the_error_recorder(settings, database, fake_usage, slots):
    account = insert_account(database, settings, 1)
    state_log: list[dict] = []
    pool = make_pool(settings, database)
    app = build_app(settings, pool, {"device-a": device_for(account, 1)}, state_log=state_log)
    with LiveServer(app) as server:
        def post(path: str, body: dict) -> httpx.Response:
            headers = {"Authorization": "Bearer device-a", "Idempotency-Key": str(uuid.uuid4())}
            with httpx.Client(timeout=60, trust_env=False) as client:
                return client.post(server.base_url + path, json=body, headers=headers)

        failed = post("/api/v1/assist", {"mode": "context", "fake": {"script": "error_status"}})
        refused = post("/api/v1/assist/stream", {"mode": "chat", "message": "x" * 2001})
        unknown = post("/api/v1/assist", {"mode": "chat", "message": "hi"})
    assert (failed.status_code, refused.status_code, unknown.status_code) == (503, 400, 400)
    failed_state, refused_state, unknown_state = [entry for entry in state_log if entry["path"].startswith("/api/v1/assist")]
    assert failed_state["assist_mode"] == "context"
    assert failed_state["reservation_id"] == fake_usage.only_row()["id"]
    assert refused_state["assist_mode"] == "chat" and "reservation_id" not in refused_state
    assert "assist_mode" not in unknown_state and "reservation_id" not in unknown_state  # unvalidated mode
