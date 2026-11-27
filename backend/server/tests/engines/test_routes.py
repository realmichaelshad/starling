"""/api/v1 engine routes: validation, consent, metering, replay, error mapping and forwarding."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from starling_server.usage.service import UsageError

from .support import served, set_consent, wait_until

CONTEXT = {"mode": "context", "context": {"text": "a post"}}


def call(h, method: str, path: str, name: str = "a", key: str | None = None, body=None, content: bytes | None = None,
         time_zone: str | None = "Europe/London") -> httpx.Response:
    with httpx.Client(timeout=60, trust_env=False) as client:
        return client.request(method, h.url(path), json=body, content=content, headers=h.headers(name, key, time_zone))


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


def test_modes_bodies_and_chat_length_are_validated_before_any_engine_work(harness, fake_usage):
    key = str(uuid.uuid4())
    assert error_code(call(harness, "POST", "/api/v1/assist", key=key, body={"mode": "chat", "message": "hi"})) == "bad_request"
    for mode in ("context", "schedule"):
        response = call(harness, "POST", "/api/v1/assist/stream", key=key, body={"mode": mode})
        assert response.status_code == 400 and error_code(response) == "bad_request"
    too_long = call(harness, "POST", "/api/v1/assist/stream", key=key, body={"mode": "chat", "message": "x" * 2001})
    assert too_long.status_code == 400
    assert call(harness, "POST", "/api/v1/assist", key=key, content=b"not json").status_code == 400
    assert call(harness, "POST", "/api/v1/assist", key=key, body=[1, 2]).status_code == 400
    big_assist = call(harness, "POST", "/api/v1/assist", key=key,
                      content=json.dumps({"mode": "schedule", "pad": "x" * (256 * 1024)}).encode())
    assert big_assist.status_code == 413 and error_code(big_assist) == "payload_too_large"
    big_capture = call(harness, "POST", "/api/v1/outcomes", content=json.dumps({"pad": "x" * (64 * 1024)}).encode())
    assert big_capture.status_code == 413
    assert harness.pool.spawn_count == 0 and "reserve" not in fake_usage.names()

    # Controls: the limits are exact, not "refuse everything".
    chat = call(harness, "POST", "/api/v1/assist/stream", key=str(uuid.uuid4()),
                body={"mode": "chat", "message": "x" * 2000, "fake": {"tokens": ["ok"]}})
    assert chat.status_code == 200 and '"final"' in chat.text
    near_limit = call(harness, "POST", "/api/v1/assist",
                      content=json.dumps({"mode": "schedule", "pad": "x" * (250 * 1024)}).encode())
    assert near_limit.status_code == 200
    assert call(harness, "POST", "/api/v1/outcomes", body={"nonce": "small"}).status_code in (200, 202)


def test_idempotency_key_is_required_and_must_be_a_uuid(harness, fake_usage):
    for key in (None, "not-a-uuid", "7a2c#b3", "{" + str(uuid.uuid4()) + "}"):
        assert call(harness, "POST", "/api/v1/assist", key=key, body=CONTEXT).status_code == 400
        assert call(harness, "POST", "/api/v1/assist/stream", key=key, body={"mode": "chat", "message": "hi"}).status_code == 400
    assert "reserve" not in fake_usage.names()
    response = call(harness, "POST", "/api/v1/assist", key=str(uuid.uuid4()), body=CONTEXT)  # control
    assert response.status_code == 200 and fake_usage.names().count("reserve") == 1


def test_consent_gates_metered_requests_and_captures(harness_factory, fake_usage, slots, settings, database):
    h = harness_factory(("a",), consent=False)
    passive_metrics = {"post_id": "1", "metadata": {"capture_mode": "passive_visible_post"}}
    for path, body in (("/api/v1/outcomes", {"nonce": 1}), ("/api/v1/snapshots/metrics", passive_metrics),
                       ("/api/v1/snapshots/profile", {"handle": "me"})):
        response = call(h, "POST", path, body=body)
        assert response.status_code == 403 and error_code(response) == "consent_required"
    assert error_code(call(h, "POST", "/api/v1/assist", key=str(uuid.uuid4()), body=CONTEXT)) == "consent_required"
    assert error_code(call(h, "POST", "/api/v1/assist/stream", key=str(uuid.uuid4()),
                           body={"mode": "chat", "message": "hi"})) == "consent_required"
    assert "reserve" not in fake_usage.names()
    # Scheduling uses no draft, but it starts the customer's engine, so it needs the disclosure too
    # (tests/engines/test_hardening_unmetered.py has the rest of that gate).
    assert error_code(call(h, "POST", "/api/v1/assist", body={"mode": "schedule", "schedule": {"op": "list"}})) == "consent_required"

    set_consent(database, settings, h.account_id("a"), passive=False)
    assert call(h, "POST", "/api/v1/outcomes", body={"nonce": 2}).status_code in (200, 202)
    assert error_code(call(h, "POST", "/api/v1/snapshots/profile", body={"handle": "me"})) == "consent_required"
    assert error_code(call(h, "POST", "/api/v1/snapshots/metrics", body=passive_metrics)) == "consent_required"
    assert error_code(call(h, "POST", "/api/v1/snapshots/metrics", body={"post_id": "2"})) == "consent_required"
    explicit = {"post_id": "3", "metadata": {"capture_mode": "explicit_refresh"}}
    assert call(h, "POST", "/api/v1/snapshots/metrics", body=explicit).status_code in (200, 202)

    set_consent(database, settings, h.account_id("a"), passive=True)
    assert call(h, "POST", "/api/v1/snapshots/profile", body={"handle": "me"}).status_code in (200, 202)
    assert call(h, "POST", "/api/v1/snapshots/metrics", body=passive_metrics).status_code in (200, 202)


def test_schedule_mode_is_forwarded_without_metering(harness, fake_usage):
    response = call(harness, "POST", "/api/v1/assist", key=str(uuid.uuid4()),
                    body={"mode": "schedule", "schedule": {"op": "list"}, "nonce": "sched"})
    assert response.status_code == 200
    assert response.json()["schedule"] == {"op": "list"}
    assert response.json()["served_by"]["tenant_id"] == harness.account_id("a")
    assert fake_usage.names() == []
    line = next(item for item in served(harness.settings, 1) if item["nonce"] == "sched")
    assert line["request_id"] is None and line["time_zone"] == "Europe/London"


def test_context_assist_is_metered_replayed_and_protected_against_key_reuse(harness, fake_usage, slots):
    key = str(uuid.uuid4())
    body = {**CONTEXT, "nonce": "ctx-1"}
    first = call(harness, "POST", "/api/v1/assist", key=key, body=body, time_zone="America/New_York")
    assert first.status_code == 200
    payload = first.json()
    assert payload["decision"]["draft"] == "A usable draft." and payload["usage"]["used"] == 1
    assert fake_usage.names() == ["reserve", "activate", "finish"]
    row = fake_usage.only_row()
    assert row["state"] == "committed" and row["route"] == "/assist" and row["mode"] == "context"
    line = next(item for item in served(harness.settings, 1) if item["nonce"] == "ctx-1")
    assert line["request_id"] == row["id"] and line["time_zone"] == "America/New_York"
    assert [kind for kind, _, _ in slots.events] == ["enter", "exit"]

    replay = call(harness, "POST", "/api/v1/assist", key=key, body=body)
    assert replay.status_code == 200 and replay.json()["decision"] == payload["decision"]
    assert len([item for item in served(harness.settings, 1) if item["nonce"] == "ctx-1"]) == 1  # no engine call
    assert fake_usage.names().count("activate") == 1

    mismatch = call(harness, "POST", "/api/v1/assist", key=key, body={**body, "nonce": "different"})
    assert mismatch.status_code == 409 and error_code(mismatch) == "idempotency_mismatch"


@pytest.mark.parametrize(("engine_code", "status", "code", "retry_after"), [
    ("budget_exhausted", 503, "service_paused", "40"),
    ("fair_use_exceeded", 402, "fair_use_exceeded", None),
    ("ai_quota", 503, "ai_unavailable", "5"),
    ("call_cap_exceeded", 503, "ai_unavailable", "5"),
    ("bad_request", 400, "bad_request", None),
])
def test_engine_refusals_on_assist_become_api_errors_and_use_nothing(harness, fake_usage, engine_code, status, code, retry_after):
    fake = {"script": "refused", "code": engine_code}
    if engine_code == "budget_exhausted":
        fake["retry_after_s"] = 40
    refused = call(harness, "POST", "/api/v1/assist", key=str(uuid.uuid4()), body={**CONTEXT, "fake": fake})
    assert refused.status_code == status
    error = refused.json()["error"]
    assert error["code"] == code and error["usage"]["used"] == 0
    assert refused.headers.get("retry-after") == retry_after
    assert fake_usage.only_row()["state"] == "released"


def test_usage_refusals_map_to_the_contract_error_body(harness, fake_usage):
    usage = {"unit": "draft", "allowance": 25, "used": 25, "reserved": 0, "remaining": 0}
    fake_usage.reserve_error = UsageError("limit_reached", 402, "You have used all drafts in this period.", usage=usage)
    limited = call(harness, "POST", "/api/v1/assist", key=str(uuid.uuid4()), body=CONTEXT)
    assert limited.status_code == 402
    error = limited.json()["error"]
    assert error["code"] == "limit_reached" and error["usage"] == usage
    assert error["upgrade_path"] == "/pricing?from=extension&reason=limit"

    fake_usage.reserve_error = UsageError("rate_limited", 429, "Too many requests.", retry_after_s=17)
    throttled = call(harness, "POST", "/api/v1/assist/stream", key=str(uuid.uuid4()), body={"mode": "chat", "message": "hi"})
    assert throttled.status_code == 429 and throttled.headers["retry-after"] == "17"
    assert throttled.json()["error"]["retry_after_s"] == 17
    assert harness.pool.spawn_count == 0


def test_own_material_routes_forward_to_the_engine_paths(harness):
    listed = call(harness, "GET", "/api/v1/own-material?kind=voice")
    assert listed.status_code == 200 and listed.json()["path"] == "/own-material/list"
    assert listed.json()["echo"] == {"kind": "voice"}
    assert call(harness, "GET", "/api/v1/own-material?kind=bogus").status_code == 400
    added = call(harness, "POST", "/api/v1/own-material", key=str(uuid.uuid4()), body={"kind": "voice", "text": "mine"})
    assert added.status_code == 200 and added.json()["path"] == "/own-material/add"
    removed = call(harness, "DELETE", "/api/v1/own-material/item-1", key=str(uuid.uuid4()))
    assert removed.status_code == 200 and removed.json()["echo"] == {"id": "item-1"}
    assert call(harness, "DELETE", "/api/v1/own-material/bad%20id", key=str(uuid.uuid4())).status_code == 400
    rejected = call(harness, "POST", "/api/v1/own-material", key=str(uuid.uuid4()),
                    body={"fake_status": 400, "fake_detail": "That entry is too short."})
    assert rejected.status_code == 400 and rejected.json()["error"]["message"] == "That entry is too short."
    export = harness.pool  # account data export uses the same pool request path
    assert all(item["tenant_id"] == harness.account_id("a") for item in served(harness.settings, 1))
    assert export.spawn_count == 1


def test_captures_are_spooled_while_the_engine_is_down_and_replayed_first(harness_factory, fake_usage, slots):
    h = harness_factory(("a",), passive=True)
    for n in range(2):
        queued = call(h, "POST", "/api/v1/snapshots/metrics", body={"post_id": str(n), "nonce": f"metric-{n}"})
        assert queued.status_code == 202 and queued.json() == {"ok": True, "queued": True}
    assert h.pool.spawn_count == 0
    added = call(h, "POST", "/api/v1/own-material", key=str(uuid.uuid4()), body={"kind": "voice", "text": "x", "nonce": "add"})
    assert added.status_code == 200
    assert [line["nonce"] for line in served(h.settings, 1)] == ["metric-0", "metric-1", "add"]
    direct = call(h, "POST", "/api/v1/snapshots/metrics", body={"post_id": "9", "nonce": "live"})
    assert direct.status_code == 200 and direct.json()["path"] == "/snapshots/metrics"  # control


def test_a_suspended_device_reaches_no_engine(harness_factory, fake_usage, slots):
    from .support import device_for  # the harness builds active devices; swap one in place

    h = harness_factory(("a",))
    app = h.server.app
    override = next(iter(app.dependency_overrides.values()))
    suspended = device_for(h.account_id("a"), 1, status="suspended")
    app.dependency_overrides[next(iter(app.dependency_overrides))] = lambda: suspended
    response = call(h, "GET", "/api/v1/own-material")
    assert response.status_code == 403 and error_code(response) == "account_suspended"
    assert h.pool.spawn_count == 0
    app.dependency_overrides[next(iter(app.dependency_overrides))] = override
    assert wait_until(lambda: call(h, "GET", "/api/v1/own-material").status_code == 200, timeout=20)  # control
