"""Requests that need no account must not each cost a write on the one database writer.

Found by the security review. The control database has a single writer that every sign-in,
reservation and webhook waits for, and each transaction ends in a synced commit. Three things let
anybody on the internet queue one such transaction per request: every refused API call was recorded
for the owner dashboard; a caller already over a rate limit was still counted, in the database, on
every further request; and `/readyz` writes a probe row. A flood of any of them stalled everybody.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from starling_server.db import request_errors
from starling_server.security import ratelimit
from starling_server.system import routes as system_routes


class CountingWriter:
    """Stands in for `Database.writer`: keeps the jobs instead of running them."""

    def __init__(self) -> None:
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)

        class Done:
            def add_done_callback(self, _callback) -> None:
                return None

        return Done()


class StubDatabase:
    def __init__(self) -> None:
        self.writer = CountingWriter()


def fresh_table() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # The shape of migration 3's table: the upsert needs exactly this primary key.
    conn.execute(
        "CREATE TABLE request_errors (minute INTEGER, account_id TEXT, route TEXT, mode TEXT, code TEXT, http_status INTEGER, "
        "reserved INTEGER, count INTEGER, first_at INTEGER, last_at INTEGER, "
        "PRIMARY KEY (minute, account_id, route, mode, code, http_status, reserved))")
    return conn


def refuse(db, now: int, *, account: str | None = None, code: str = "signed_out") -> None:
    request_errors.record(db, route="/api/v1/assist", code=code, http_status=401, now=now, account_id=account)


def test_a_flood_of_refusals_queues_one_write_not_one_each():
    db = StubDatabase()
    for second in range(500):
        refuse(db, 1_800_000_000 + second % 50)
    assert len(db.writer.jobs) == 1, "500 refusals, one transaction waiting on the writer"

    conn = fresh_table()
    assert db.writer.jobs[0](conn) == 1, "and they are one row"
    row = conn.execute("SELECT count, first_at, last_at FROM request_errors").fetchone()
    assert (row["count"], row["first_at"], row["last_at"]) == (500, 1_800_000_000, 1_800_000_049)


def test_nothing_is_lost_between_flushes_and_a_quiet_server_writes_at_once():
    db, conn = StubDatabase(), fresh_table()
    refuse(db, 1_800_000_000)
    assert len(db.writer.jobs) == 1, "a single refusal is queued straight away, as it always was"
    db.writer.jobs.pop()(conn)
    refuse(db, 1_800_000_001)
    refuse(db, 1_800_000_002, account="acct-1", code="limit_reached")
    assert len(db.writer.jobs) == 1
    db.writer.jobs.pop()(conn)
    rows = {(r["account_id"], r["code"]): r["count"] for r in conn.execute("SELECT * FROM request_errors")}
    assert rows == {("", "signed_out"): 2, ("acct-1", "limit_reached"): 1}


def test_the_waiting_rows_are_bounded(monkeypatch):
    monkeypatch.setattr(request_errors, "MAX_PENDING_ROWS", 10)
    db = StubDatabase()
    for index in range(50):
        refuse(db, 1_800_000_000, account=f"acct-{index}")
    conn = fresh_table()
    assert db.writer.jobs[0](conn) == 10
    assert conn.execute("SELECT COUNT(*) FROM request_errors").fetchone()[0] == 10


def test_a_caller_over_the_limit_stops_costing_writes(database, monkeypatch):
    ratelimit.forget_refusals()
    rule, key = ratelimit.Rule("flood_probe", 3, 600), ratelimit.ip_key("203.0.113.0/24")
    writes = 0
    real = database.write_async

    async def counting(job):
        nonlocal writes
        writes += 1
        return await real(job)

    monkeypatch.setattr(database, "write_async", counting)

    async def flood() -> list[bool]:
        return [(await ratelimit.check(database, [(rule, key)], now=1_800_000_000)).allowed for _ in range(200)]

    allowed = asyncio.run(flood())
    assert allowed == [True, True, True] + [False] * 197
    assert writes == 4, "three allowed, one refusal learned, and 196 refusals answered from memory"

    async def later() -> bool:
        return (await ratelimit.check(database, [(rule, key)], now=1_800_000_000 + 600)).allowed

    assert asyncio.run(later()) is True, "the next window starts clean"
    other = asyncio.run(ratelimit.check(database, [(rule, ratelimit.ip_key("198.51.100.0/24"))], now=1_800_000_000))
    assert other.allowed is True, "and nobody else was affected"
    ratelimit.forget_refusals()


def test_a_refusal_still_says_how_long_to_wait(database):
    ratelimit.forget_refusals()
    rule, key = ratelimit.Rule("flood_wait", 1, 600), ratelimit.account_key("acct")

    async def run():
        await ratelimit.check(database, [(rule, key)], now=1_800_000_100)
        learned = await ratelimit.check(database, [(rule, key)], now=1_800_000_100)
        remembered = await ratelimit.check(database, [(rule, key)], now=1_800_000_400)
        return learned, remembered

    learned, remembered = asyncio.run(run())
    assert (learned.allowed, remembered.allowed) == (False, False)
    assert learned.retry_after_s == 500 and remembered.retry_after_s == 200
    ratelimit.forget_refusals()


def test_a_flood_of_readiness_probes_is_one_probe(client, monkeypatch):
    calls = 0
    real = system_routes.readiness_checks

    def counting(app):
        nonlocal calls
        calls += 1
        return real(app)

    monkeypatch.setattr(system_routes, "readiness_checks", counting)
    for _ in range(50):
        assert client.get("/readyz").status_code == 200
    assert calls == 1
