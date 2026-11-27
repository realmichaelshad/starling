"""Helpers for the engine pool tests: the fake engine, accounts, a usage fake, a slot recorder
around the real `account_slot`, and a real uvicorn server (Starlette's TestClient buffers
streamed responses, so streaming behaviour needs real HTTP)."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, Request

from starling_server.auth import deps
from starling_server.auth.deps import DeviceAuth
from starling_server.config import Settings
from starling_server.db import Database
from starling_server.engines.dev_spawner import DevSpawner
from starling_server.engines.pool import EnginePool, PoolOptions
from starling_server.security.errors import ApiError
from starling_server.security.middleware import RequestIdMiddleware
from starling_server.usage import concurrency, service
from starling_server.usage.service import Replay, Reservation, UsageError

FAKE_ENGINE = Path(__file__).with_name("fake_engine.py")
LIVE_STATES = ("reserved", "active")


def run(coro):
    return asyncio.run(coro)


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------- accounts


def insert_account(
    db: Database,
    settings: Settings,
    tenant_seq: int,
    *,
    verified: bool = True,
    status: str = "active",
    consent: bool = True,
    passive: bool = False,
) -> str:
    account_id = str(uuid.uuid4())
    email = f"customer{tenant_seq}@example.com"
    now = int(time.time())

    def write(conn) -> None:
        conn.execute(
            "INSERT INTO accounts (id, tenant_seq, email, email_norm, allowance_key, email_verified_at, "
            "password_hash, password_changed_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'x', 0, ?, ?)",
            (account_id, tenant_seq, email, email, f"ak-{tenant_seq}", now - 3600 if verified else None, status, now - 7200),
        )
        if consent:
            conn.execute(
                "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at) "
                "VALUES (?, 'extension_disclosure', ?, ?, ?)",
                (account_id, settings.consent_version, 1 if passive else 0, now - 1800),
            )

    db.write(write)
    return account_id


def set_consent(db: Database, settings: Settings, account_id: str, *, passive: bool) -> None:
    db.write(lambda conn: conn.execute(
        "INSERT INTO consents (account_id, kind, version, passive_learning, granted_at) "
        "VALUES (?, 'extension_disclosure', ?, ?, ?) ON CONFLICT (account_id, kind, version) DO UPDATE SET "
        "passive_learning = excluded.passive_learning, revoked_at = NULL",
        (account_id, settings.consent_version, 1 if passive else 0, int(time.time()))))


def device_for(account_id: str, tenant_seq: int, status: str = "active") -> DeviceAuth:
    return DeviceAuth(account_id=account_id, tenant_seq=tenant_seq, device_id=str(uuid.uuid4()),
                      email_verified=True, status=status, extension_version="1.0.0")


# --------------------------------------------------------------------------- pool


def fake_command(*args: str) -> list[str]:
    return [sys.executable, str(FAKE_ENGINE), *args]


def make_pool(settings: Settings, db: Database, *, fake_args: tuple[str, ...] = (), ready_timeout_s: float = 15.0,
              **options: Any) -> EnginePool:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    spawner = DevSpawner(settings, command=fake_command(*fake_args), cwd=str(settings.data_dir),
                         ready_timeout_s=ready_timeout_s)
    values: dict[str, Any] = {
        "tick_s": 0.1, "status_poll_s": 0.5, "backoff_s": (0.0, 0.0, 0.0), "stop_grace_s": 5.0,
        "livez_timeout_s": 5.0, "idle_seconds": 3600.0, "last_used_write_s": 0.0,
    }
    values.update(options)
    return EnginePool(settings, db, spawner, options=PoolOptions.from_settings(settings, **values))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def served(settings: Settings, tenant_seq: int) -> list[dict[str, Any]]:
    return read_jsonl(settings.tenants_dir / str(tenant_seq) / "served.jsonl")


# --------------------------------------------------------------------------- usage fakes


class FakeUsage:
    """In-memory stand-in for `usage.service` with the settlement rules of CONTRACTS 7
    (a usable final is treated as having used AI; a customer stop after exposed text commits,
    every other ending releases)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, float]] = []
        self.reserve_error: UsageError | None = None

    def install(self, monkeypatch) -> "FakeUsage":
        for name in ("reserve", "activate", "mark_exposed", "finish", "release", "replay", "settled_as"):
            monkeypatch.setattr(service, name, getattr(self, name))
        return self

    def _record(self, name: str, ident: str) -> None:
        with self.lock:
            self.calls.append((name, ident, time.monotonic()))

    def names(self) -> list[str]:
        with self.lock:
            return [name for name, _, _ in self.calls]

    def call_time(self, name: str) -> float | None:
        with self.lock:
            return next((at for n, _, at in self.calls if n == name), None)

    def _block(self, account_id: str) -> dict[str, Any]:
        rows = [r for r in self.by_id.values() if r["account_id"] == account_id]
        used = sum(1 for r in rows if r["state"] == "committed")
        reserved = sum(1 for r in rows if r["state"] in LIVE_STATES)
        return {"unit": "draft", "unit_label": "drafts", "allowance": 25, "used": used, "reserved": reserved,
                "remaining": 25 - used - reserved, "resets_at": 0, "near_limit": False, "fair_use_blocked": False}

    def state(self, reservation_id: str) -> str | None:
        with self.lock:
            row = self.by_id.get(reservation_id)
            return row["state"] if row else None

    def only_row(self) -> dict[str, Any]:
        with self.lock:
            assert len(self.by_id) == 1, self.by_id
            return dict(next(iter(self.by_id.values())))

    def reserve(self, db, settings, account_id, idempotency_key, body_hash, route, mode, now=None, consent=None):
        self._record("reserve", idempotency_key)
        if self.reserve_error is not None:
            raise self.reserve_error
        with self.lock:
            row = self.rows.get((account_id, idempotency_key))
            if row is not None:
                if row["hash"] != body_hash:
                    raise UsageError("idempotency_mismatch", 409, "This request id was already used for a different request.")
                if row["state"] == "committed" and row["result_json"] is not None:
                    return Replay(row["result_json"], self._block(account_id))
                if row["state"] in LIVE_STATES:
                    raise UsageError("duplicate_request", 409, "This request is still running.")
            rid = str(uuid.uuid4())
            row = {"id": rid, "account_id": account_id, "key": idempotency_key, "hash": body_hash, "route": route,
                   "mode": mode, "state": "reserved", "exposed": False, "result_json": None, "reason": None}
            self.rows[(account_id, idempotency_key)] = row
            self.by_id[rid] = row
            return Reservation(rid, account_id, idempotency_key, 1, "period", "reserved")

    def activate(self, db, reservation_id, now=None):
        self._record("activate", reservation_id)
        with self.lock:
            row = self.by_id[reservation_id]
            if row["state"] == "reserved":
                row["state"] = "active"
            elif row["state"] != "active":
                raise UsageError("reservation_inactive", 409, "This request is no longer running.")

    def mark_exposed(self, db, reservation_id, now=None):
        self._record("mark_exposed", reservation_id)
        with self.lock:
            row = self.by_id[reservation_id]
            if row["state"] in LIVE_STATES:
                row["exposed"] = True

    def finish(self, db, settings, reservation_id, usable, result_json, now=None, customer_stopped=False):
        self._record("finish", reservation_id)
        with self.lock:
            row = self.by_id[reservation_id]
            if row["state"] in LIVE_STATES:
                if usable or (customer_stopped and row["exposed"]):
                    row["state"], row["result_json"] = "committed", (result_json if usable else None)
                else:
                    row["state"], row["reason"] = "released", "unusable"
            return self._block(row["account_id"])

    def release(self, db, settings, reservation_id, reason, now=None):
        self._record("release", reservation_id)
        with self.lock:
            row = self.by_id[reservation_id]
            if row["state"] in LIVE_STATES:
                if row["exposed"] and reason in service.CUSTOMER_ENDED:
                    row["state"] = "committed"
                else:
                    row["state"], row["reason"] = "released", reason
            return self._block(row["account_id"])

    def settled_as(self, db, reservation_id):
        with self.lock:
            row = self.by_id.get(reservation_id)
            return row["state"] if row is not None else None

    def replay(self, db, account_id, idempotency_key, now=None):
        with self.lock:
            row = self.rows.get((account_id, idempotency_key))
            if row is None or row["state"] != "committed" or row["result_json"] is None:
                return None
            return Replay(row["result_json"], self._block(account_id))


class SlotRecorder:
    """Wraps the real `usage.concurrency.account_slot` and records when each slot is held."""

    def __init__(self) -> None:
        self.real = concurrency.account_slot
        self.events: list[tuple[str, str, float]] = []

    def install(self, monkeypatch) -> "SlotRecorder":
        monkeypatch.setattr(concurrency, "account_slot", self.account_slot)
        return self

    @asynccontextmanager
    async def account_slot(self, account_id: str, max_waiting: int = 1, wait_s: float = 15):
        async with self.real(account_id, max_waiting=max_waiting, wait_s=wait_s):
            self.events.append(("enter", account_id, time.monotonic()))
            try:
                yield
            finally:
                self.events.append(("exit", account_id, time.monotonic()))

    def held(self, account_id: str) -> bool:
        balance = 0
        for kind, account, _ in list(self.events):
            if account == account_id:
                balance += 1 if kind == "enter" else -1
        return balance > 0

    def exit_time(self, account_id: str) -> float | None:
        return next((at for kind, account, at in reversed(list(self.events)) if kind == "exit" and account == account_id), None)


# --------------------------------------------------------------------------- app and server


class StateRecorder:
    """Pure ASGI middleware: after each HTTP request, append a copy of its `request.state`."""

    def __init__(self, app, log: list[dict[str, Any]]) -> None:
        self.app, self.log = app, log

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.log.append({"path": scope.get("path"), **dict(scope.get("state") or {})})


def build_app(settings: Settings, pool: EnginePool, devices: dict[str, DeviceAuth] | None,
              routers: tuple[Any, ...] = (), state_log: list[dict[str, Any]] | None = None) -> FastAPI:
    """My router (plus `routers`), the foundation's request-id middleware and the engine lifecycle
    hooks. `devices` maps bearer tokens to DeviceAuth (None keeps the real `require_device`);
    `state_log` receives each request's `request.state` after its response."""
    from starling_server.api import engine_routes
    from starling_server.engines import lifecycle

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await lifecycle.startup(app)
        try:
            yield
        finally:
            await lifecycle.shutdown(app)

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.db = pool.db
    app.state.engine_pool = pool
    if state_log is not None:
        app.add_middleware(StateRecorder, log=state_log)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(engine_routes.router)
    for router in routers:
        app.include_router(router)
    if devices is not None:
        def fake_require_device(request: Request) -> DeviceAuth:
            header = request.headers.get("authorization", "")
            device = devices.get(header[7:] if header.lower().startswith("bearer ") else "")
            if device is None:
                raise ApiError("signed_out", 401, "Sign in again to continue.")
            return device

        app.dependency_overrides[deps.require_device] = fake_require_device
    return app


class LiveServer:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> "LiveServer":
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on",
                                timeout_graceful_shutdown=5, http="h11", loop="asyncio")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="live-server", daemon=True)
        self.thread.start()
        if not wait_until(lambda: self.server.started or not self.thread.is_alive(), timeout=30):
            raise RuntimeError("the test server did not start")
        if not self.server.started:
            raise RuntimeError("the test server failed to start")
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc_info) -> None:
        assert self.server is not None and self.thread is not None
        self.server.should_exit = True
        self.thread.join(60)
