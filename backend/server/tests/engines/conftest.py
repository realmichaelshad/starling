"""Fixtures for the engine pool tests (settings and database come from tests/conftest.py)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from starling_server.config import Settings
from starling_server.db import Database
from starling_server.engines.pool import EnginePool

from .support import FakeUsage, LiveServer, SlotRecorder, build_app, device_for, insert_account, make_pool


@pytest.fixture
def fake_usage(monkeypatch) -> FakeUsage:
    return FakeUsage().install(monkeypatch)


@pytest.fixture
def slots(monkeypatch) -> SlotRecorder:
    return SlotRecorder().install(monkeypatch)


@dataclass
class Harness:
    settings: Settings
    db: Database
    pool: EnginePool
    server: LiveServer
    accounts: dict[str, tuple[str, int]] = field(default_factory=dict)

    def url(self, path: str) -> str:
        return self.server.base_url + path

    def account_id(self, name: str) -> str:
        return self.accounts[name][0]

    def seq(self, name: str) -> int:
        return self.accounts[name][1]

    def headers(self, name: str, key: str | None = None, time_zone: str | None = "Europe/London") -> dict[str, str]:
        headers = {"Authorization": f"Bearer device-{name}", "X-Starling-Extension": "1.0.0"}
        if key is not None:
            headers["Idempotency-Key"] = key
        if time_zone is not None:
            headers["X-Starling-Time-Zone"] = time_zone
        return headers


@pytest.fixture
def harness_factory(settings: Settings, database: Database):
    """Start my router on a real uvicorn server with a pool of fake engines.

    `start(accounts=("a", "b"), consent=True, passive=False, **pool_options)`; the bearer token
    of account `a` is `device-a` (authentication is replaced; see test_integration for the real one).
    """
    servers: list[LiveServer] = []

    def start(accounts: tuple[str, ...] = ("a",), *, consent: bool = True, passive: bool = False,
              fake_args: tuple[str, ...] = (), **pool_options: Any) -> Harness:
        devices, ids = {}, {}
        for seq, name in enumerate(accounts, start=1):
            account_id = insert_account(database, settings, seq, consent=consent, passive=passive)
            devices[f"device-{name}"] = device_for(account_id, seq)
            ids[name] = (account_id, seq)
        pool = make_pool(settings, database, fake_args=fake_args, **pool_options)
        server = LiveServer(build_app(settings, pool, devices)).__enter__()
        servers.append(server)
        return Harness(settings, database, pool, server, ids)

    yield start
    for server in servers:
        server.__exit__(None, None, None)


@pytest.fixture
def harness(harness_factory: Callable[..., Harness], fake_usage: FakeUsage, slots: SlotRecorder) -> Harness:
    return harness_factory(("a", "b"))
