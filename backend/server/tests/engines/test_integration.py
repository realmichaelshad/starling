"""Integration with the real usage service, real device authentication, the real account slot,
the real app factory and the usage sweeper (only the engine is fake)."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx

from starling_server.app import create_app
from starling_server.auth import deps
from starling_server.devices import tokens as device_tokens
from starling_server.security import clock
from starling_server.usage import lifecycle as usage_lifecycle

from ..conftest import TEST_EXTENSION_ID
from .support import LiveServer, SlotRecorder, build_app, device_for, insert_account, make_pool, read_jsonl, served, wait_until
from .test_streams import Stream, chat


def issue_device(database, account_id: str) -> str:
    def write(conn):
        token, _device = device_tokens.issue(
            conn, device_id=str(uuid.uuid4()), account_id=account_id, auth_epoch=1, label="test browser",
            extension_id=TEST_EXTENSION_ID, extension_version="1.0.0", now=clock.now())
        return token

    return database.write(write)


class RealHarness:
    def __init__(self, settings, database, server, tokens):
        self.settings, self.db, self.server, self.tokens = settings, database, server, tokens

    def url(self, path: str) -> str:
        return self.server.base_url + path

    def headers(self, name: str, key: str | None = None, time_zone: str | None = "Europe/London") -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.tokens[name]}", "X-Starling-Extension": "1.0.0"}
        if key:
            headers["Idempotency-Key"] = key
        if time_zone:
            headers["X-Starling-Time-Zone"] = time_zone
        return headers


def reservation(database, key: str) -> dict:
    with database.read() as conn:
        return dict(conn.execute("SELECT * FROM usage_reservations WHERE idempotency_key = ?", (key,)).fetchone())


def commits(database, account_id: str) -> int:
    with database.read() as conn:
        return conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE account_id = ? AND kind = 'commit'", (account_id,)).fetchone()[0]


def test_real_usage_service_meters_streams_and_assist_end_to_end(settings, database):
    a = insert_account(database, settings, 1)
    token = issue_device(database, a)
    pool = make_pool(settings, database)
    with LiveServer(build_app(settings, pool, devices=None)) as server:
        h = RealHarness(settings, database, server, {"a": token})
        with httpx.Client(trust_env=False) as client:
            signed_out = client.get(h.url("/api/v1/own-material"), headers={"Authorization": "Bearer stl_dev_" + "x" * 43})
        assert signed_out.status_code == 401 and signed_out.json()["error"]["code"] == "signed_out"

        # The services proxy is not in this test: stand in for it recording one successful AI call per
        # request the gateway starts, so a usable final is charged as it is in production.
        database.write(lambda conn: conn.execute(
            "CREATE TRIGGER stand_in_ai_call AFTER UPDATE OF state ON usage_reservations "
            "WHEN NEW.state = 'active' AND OLD.state = 'reserved' BEGIN "
            "INSERT INTO ai_calls (account_id, reservation_id, bucket, lane, model, status, started_at, finished_at) "
            "VALUES (NEW.account_id, NEW.id, 'press', 'quality', 'test-model', 'ok', 0, 0); END"))

        stream = Stream(h, body=chat(tokens=["Real", " draft"]))
        assert stream.rest()[-2:] == ["usage", "final"]
        row = reservation(database, stream.key)
        assert row["state"] == "committed" and row["exposure_at"] is not None and row["route"] == "/assist/stream"
        assert json.loads(row["result_json"])["answer"] == "Real draft"
        assert stream.first("usage")[1]["usage"]["used"] == 1 and commits(database, a) == 1
        assert served(settings, 1)[-1]["request_id"] == row["id"]

        replay = Stream(h, key=stream.key, body=chat(tokens=["Real", " draft"]))
        assert replay.rest() == ["usage", "final"] and commits(database, a) == 1
        assert len(served(settings, 1)) == 1

        failed = Stream(h, body=chat(script="error", code="ai_unavailable"))
        assert failed.rest()[-2:] == ["usage", "error"]
        failed_row = reservation(database, failed.key)
        assert failed_row["state"] == "released" and failed_row["release_reason"] == "engine_error"

        database.write(lambda conn: conn.execute("DROP TRIGGER stand_in_ai_call"))
        with httpx.Client(timeout=60, trust_env=False) as client:
            key = str(uuid.uuid4())
            assist = client.post(h.url("/api/v1/assist"), json={"mode": "context", "context": {"text": "x"}},
                                 headers=h.headers("a", key))
        assert assist.status_code == 200 and assist.json()["decision"]["draft"]
        # The real rule: a usable result that needed no AI call is released, not charged.
        assert reservation(database, key)["state"] == "released" and commits(database, a) == 1
        with database.read() as conn:
            counters = conn.execute("SELECT committed, reserved FROM usage_counters WHERE account_id = ?", (a,)).fetchone()
        assert (counters["committed"], counters["reserved"]) == (1, 0)

        database.write(lambda conn: conn.execute("UPDATE consents SET revoked_at = ? WHERE account_id = ?", (clock.now(), a)))
        withdrawn = Stream(h, body=chat(tokens=["no"]))
        assert withdrawn.response.status_code == 403
        withdrawn.close()


def test_real_slot_is_held_after_a_disconnect_until_the_engine_finishes(settings, database, monkeypatch):
    slots = SlotRecorder().install(monkeypatch)  # records around the real account_slot
    b = insert_account(database, settings, 2)
    token = issue_device(database, b)
    pool = make_pool(settings, database)
    with LiveServer(build_app(settings, pool, devices=None)) as server:
        h = RealHarness(settings, database, server, {"b": token})
        stream = Stream(h, name="b", body=chat(script="tokens_then_hang", tokens=["seen"], hang_s=30, cancel_delay_s=1.0))
        stream.until("token")
        stream.close()
        assert wait_until(lambda: read_jsonl(settings.tenants_dir / "2" / "cancels.jsonl"), timeout=10)
        assert slots.held(b) is True
        assert wait_until(lambda: not slots.held(b), timeout=10)
        assert wait_until(lambda: reservation(database, stream.key)["state"] == "committed", timeout=10)
        ended = read_jsonl(settings.tenants_dir / "2" / "stream_end.jsonl")[0]["at"]
        assert slots.exit_time(b) + (time.time() - time.monotonic()) >= ended - 0.05


def test_usage_sweeper_cancels_expired_requests_through_the_pool(settings, database, monkeypatch):
    c = insert_account(database, settings, 3)
    token = issue_device(database, c)
    pool = make_pool(settings, database)
    app = build_app(settings, pool, devices=None)
    with LiveServer(app) as server:
        h = RealHarness(settings, database, server, {"c": token})
        stream = Stream(h, name="c", body=chat(script="hang", hang_s=30))
        stream.until("status")
        stream.until("status")
        rid = reservation(database, stream.key)["id"]
        database.write(lambda conn: conn.execute("UPDATE usage_reservations SET expires_at = 0 WHERE id = ?", (rid,)))
        future = asyncio.run_coroutine_threadsafe(usage_lifecycle.sweep_once(app), pool._loop)
        assert future.result(30) == [rid]
        assert stream.rest()[-1] == "final"
        assert [item["request_id"] for item in read_jsonl(settings.tenants_dir / "3" / "cancels.jsonl")] == [rid]
        assert reservation(database, stream.key)["state"] == "expired"


def test_health_endpoints_of_the_real_app_never_spawn_engines(settings, database):
    a = insert_account(database, settings, 4)
    app = create_app(settings)
    pool = make_pool(settings, database)
    app.state.engine_pool = pool
    device = device_for(a, 4)
    app.dependency_overrides[deps.require_device] = lambda: device
    with LiveServer(app) as server:
        with httpx.Client(timeout=60, trust_env=False) as client:
            for path in ("/healthz", "/readyz", "/robots.txt", "/.well-known/security.txt", "/"):
                client.get(server.base_url + path)
            spawned_by_health = pool.spawn_count
            control = client.get(server.base_url + "/api/v1/own-material", headers={"X-Starling-Extension": "1.0.0"})
    assert spawned_by_health == 0
    assert control.status_code == 200 and pool.spawn_count == 1  # control: an engine route does spawn
