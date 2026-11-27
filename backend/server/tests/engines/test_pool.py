"""EnginePool: spawn once, headers, idle reaping, LRU eviction, crash backoff and breaker,
spool replay, recycling, deletion and shutdown. Every guard has a negative control."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from starling_server.engines.pool import (
    ALERT_KEY_PREFIX,
    CircuitOpen,
    EngineMismatch,
    EngineUnavailable,
    PoolBusy,
)
from starling_server.engines.spawner import EngineStatus
from starling_server.engines.spool import CaptureSpool, SpoolEntry

from .support import insert_account, make_pool, run, served

SLOW = json.dumps({"mode": "context", "fake": {"script": "slow", "delay_s": 1.5}}).encode()
CRASH = json.dumps({"mode": "context", "fake": {"script": "crash"}}).encode()


def engine_row(database, account_id):
    with database.read() as conn:
        row = conn.execute("SELECT * FROM engines WHERE account_id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


async def eventually(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def test_concurrent_ensure_spawns_one_engine_and_records_it(settings, database):
    a = insert_account(database, settings, 1)

    async def scenario():
        pool = make_pool(settings, database)
        await pool.start(maintenance=False)
        try:
            handles = await asyncio.gather(*(pool.ensure(a, 1) for _ in range(5)))
            return handles, pool.spawn_count, pool.health(), engine_row(database, a)
        finally:
            await pool.close()

    handles, spawns, health, row = run(scenario())
    assert spawns == 1 and len({handle.pid for handle in handles}) == 1
    assert health["running"] == 1 and health["max_live"] == settings.engine_max_live
    assert row["state"] == "running" and row["tenant_seq"] == 1 and row["pid"] == handles[0].pid
    assert engine_row(database, a)["state"] == "stopped"  # close() stopped it


def test_requests_carry_only_contract_headers_and_other_routes_are_refused(settings, database):
    a = insert_account(database, settings, 1)
    insert_account(database, settings, 2)

    async def scenario():
        pool = make_pool(settings, database)
        await pool.start(maintenance=False)
        try:
            status, _ = await pool.request(
                a, 1, "POST", "/own-material/list", b'{"nonce":"h1"}',
                {"x-starling-time-zone": "Europe/Paris", "Authorization": "Bearer stolen", "Cookie": "c=1",
                 "X-Starling-Request-Id": "res-1"}, 10)
            bad_zone, _ = await pool.request(a, 1, "POST", "/own-material/list", b'{"nonce":"h2"}',
                                            {"X-Starling-Time-Zone": "../../etc/passwd"}, 10)
            with pytest.raises(ValueError):
                await pool.request(a, 1, "POST", "/import", b"{}", {}, 5)
            with pytest.raises(ValueError):
                await pool.request(a, 1, "DELETE", "/data", b"", {}, 5)
            with pytest.raises(EngineMismatch):
                await pool.request(a, 2, "GET", "/livez", b"", {}, 5)
            return status, bad_zone
        finally:
            await pool.close()

    status, bad_zone = run(scenario())
    assert status == 200 and bad_zone == 200
    first, second = [line for line in served(settings, 1) if line["path"] == "/own-material/list"]
    assert first["time_zone"] == "Europe/Paris" and first["request_id"] == "res-1" and first["nonce"] == "h1"
    assert second["time_zone"] is None


def test_idle_engines_are_reaped_but_busy_engines_are_not(settings, database):
    a = insert_account(database, settings, 1)
    b = insert_account(database, settings, 2)

    async def scenario():
        pool = make_pool(settings, database, idle_seconds=0.4, tick_s=0.1)
        await pool.start()
        try:
            await pool.ensure(a, 1)
            await pool.ensure(b, 2)
            busy = asyncio.create_task(pool.request(b, 2, "POST", "/browser/assist", SLOW, {}, 10))
            await asyncio.sleep(1.1)
            during = (pool.is_live(a), pool.is_live(b))
            status, _ = await busy
            reaped_after = await eventually(lambda: not pool.is_live(b), 5)
            return during, status, reaped_after
        finally:
            await pool.close()

    (a_live, b_live), status, reaped_after = run(scenario())
    assert a_live is False  # idle: reaped
    assert b_live is True and status == 200  # busy: kept
    assert reaped_after is True  # control: reaped once idle
    assert engine_row(database, a)["state"] == "stopped"


def test_least_recently_used_idle_engine_is_evicted_and_503_when_all_are_busy(settings, database):
    a, b, c = (insert_account(database, settings, seq) for seq in (1, 2, 3))

    async def scenario():
        pool = make_pool(settings, database, max_live=2)
        await pool.start(maintenance=False)
        try:
            await pool.ensure(a, 1)
            await pool.ensure(b, 2)
            await pool.request(a, 1, "POST", "/own-material/list", b"{}", {}, 10)  # a is now the most recent
            await pool.ensure(c, 3)
            after_eviction = (pool.is_live(a), pool.is_live(b), pool.is_live(c))
            first = asyncio.create_task(pool.request(a, 1, "POST", "/browser/assist", SLOW, {}, 10))
            second = asyncio.create_task(pool.request(c, 3, "POST", "/browser/assist", SLOW, {}, 10))
            await asyncio.sleep(0.4)
            with pytest.raises(PoolBusy) as busy:
                await pool.ensure(b, 2)
            await first
            await second
            await pool.ensure(b, 2)  # control: allowed again once an engine is idle
            return after_eviction, busy.value, pool.is_live(b), pool.health()
        finally:
            await pool.close()

    after_eviction, busy, b_live, health = run(scenario())
    assert after_eviction == (True, False, True)
    assert busy.http_status == 503 and busy.code == "ai_unavailable" and busy.retry_after_s >= 1
    assert b_live is True and health["running"] == 2


def test_an_evicted_engine_exits_while_the_new_one_starts(settings, database):
    """The person waiting for a first draft does not also wait out someone else's engine shutting down."""
    a, b, c = (insert_account(database, settings, seq) for seq in (1, 2, 3))

    async def scenario():
        pool = make_pool(settings, database, max_live=2)
        await pool.start(maintenance=False)
        release = asyncio.Event()
        real_stop = pool.spawner.stop

        async def slow_stop(tenant_seq, grace_s=10.0):
            if tenant_seq == 2:
                await release.wait()  # the evicted engine takes its time to exit
            await real_stop(tenant_seq, grace_s)

        pool.spawner.stop = slow_stop
        try:
            await pool.ensure(a, 1)
            await pool.ensure(b, 2)
            await pool.request(a, 1, "POST", "/own-material/list", b"{}", {}, 10)  # b is the least recent
            # Sequential stopping would wait here for ever: the gate opens only after this returns.
            await asyncio.wait_for(pool.ensure(c, 3), timeout=20)
            during = (pool.is_live(b), pool.is_live(c), pool.health()["stopping"])
            release.set()
            gone = await eventually(lambda: pool.health()["stopping"] == 0, 10)
            return during, gone, pool.health()
        finally:
            release.set()
            await pool.close()

    (b_live, c_live, stopping), gone, health = run(scenario())
    assert c_live is True and b_live is False and stopping == 1
    assert gone is True and health["running"] == 2
    assert engine_row(database, b)["state"] == "stopped"


def test_crashes_restart_with_backoff_then_open_the_circuit_breaker(settings, database):
    a = insert_account(database, settings, 1)
    b = insert_account(database, settings, 2)

    async def scenario():
        pool = make_pool(settings, database, backoff_s=(1.0, 2.0, 3.0), breaker_crashes=3, breaker_window_s=120.0)
        await pool.start(maintenance=False)

        async def crash() -> float:
            with pytest.raises(EngineUnavailable):
                await pool.request(a, 1, "POST", "/browser/assist", CRASH, {}, 10)
            assert await eventually(lambda: a not in pool._engines)
            return time.monotonic()

        try:
            started = time.monotonic()
            await pool.ensure(a, 1)
            cold_start = time.monotonic() - started
            crashed_at = await crash()
            await pool.ensure(a, 1)
            first_restart = time.monotonic() - crashed_at
            crashed_at = await crash()
            await pool.ensure(a, 1)
            second_restart = time.monotonic() - crashed_at
            await crash()
            with pytest.raises(CircuitOpen) as opened:
                await pool.ensure(a, 1)
            await pool.ensure(b, 2)  # other customers are unaffected
            with database.read() as conn:
                alert = conn.execute("SELECT value_json FROM service_state WHERE key = ?",
                                     (ALERT_KEY_PREFIX + "1",)).fetchone()
            return cold_start, first_restart, second_restart, opened.value, alert, pool.is_live(b), engine_row(database, a)
        finally:
            await pool.close()

    cold_start, first_restart, second_restart, opened, alert, b_live, row = run(scenario())
    assert cold_start < 0.9  # control: without a crash there is no wait
    assert first_restart >= 0.95
    assert second_restart >= 1.95
    assert opened.http_status == 503 and 1 <= opened.retry_after_s <= 120
    assert alert is not None and json.loads(alert["value_json"])["kind"] == "engine_circuit_open"
    assert b_live is True
    assert row["restarts"] == 3 and row["state"] == "crashed"


def test_captures_spool_without_spawning_and_replay_in_order_before_other_requests(settings, database):
    a = insert_account(database, settings, 1)

    async def scenario():
        pool = make_pool(settings, database)
        await pool.start(maintenance=False)
        try:
            spooled = []
            for n in range(3):
                body = json.dumps({"nonce": f"cap-{n}"}).encode()
                spooled.append(await pool.capture(a, 1, "/snapshots/metrics", body, {"X-Starling-Time-Zone": "Asia/Tokyo"}))
            spawns_before = pool.spawn_count
            size_before = pool.spool.size(1)
            status, _ = await pool.request(a, 1, "POST", "/own-material/list", b'{"nonce":"after"}', {}, 10)
            direct = await pool.capture(a, 1, "/outcomes/record", b'{"nonce":"direct"}', {})
            return spooled, spawns_before, size_before, status, direct, pool.spool.size(1)
        finally:
            await pool.close()

    spooled, spawns_before, size_before, status, direct, size_after = run(scenario())
    assert all(item.spooled and item.status == 202 for item in spooled)
    assert spawns_before == 0 and size_before > 0
    assert status == 200 and size_after == 0
    lines = served(settings, 1)
    assert [line["nonce"] for line in lines] == ["cap-0", "cap-1", "cap-2", "after", "direct"]
    assert [line["time_zone"] for line in lines[:3]] == ["Asia/Tokyo"] * 3
    assert direct.spooled is False and direct.status == 200  # control: a running engine gets it directly


def test_spool_is_size_capped_by_dropping_the_oldest_entries(tmp_path):
    async def scenario():
        spool = CaptureSpool(tmp_path / "spool", max_bytes=800)
        for n in range(30):
            body = json.dumps({"nonce": n, "pad": "x" * 40}).encode()
            assert await spool.append(1, SpoolEntry.create("/outcomes/record", body, None))
        entries = await spool.read(1)
        too_big = await spool.append(2, SpoolEntry.create("/outcomes/record", b'{"pad":"' + b"y" * 900 + b'"}', None))
        return [json.loads(entry.body)["nonce"] for entry in entries], spool.size(1), too_big

    nonces, size, too_big = run(scenario())
    assert nonces == sorted(nonces) and nonces[-1] == 29 and nonces[0] > 0
    assert size <= 800 and too_big is False
    with pytest.raises(ValueError):
        SpoolEntry.create("/browser/assist", b"{}", None)


def test_engine_over_the_soft_memory_limit_is_recycled_once_idle(settings, database):
    a = insert_account(database, settings, 1)

    async def scenario():
        pool = make_pool(settings, database, status_poll_s=0.0)
        await pool.start(maintenance=False)
        try:
            handle = await pool.ensure(a, 1)
            busy = asyncio.create_task(pool.request(a, 1, "POST", "/browser/assist", SLOW, {}, 10))
            await asyncio.sleep(0.3)

            async def over_limit():
                return [EngineStatus(1, handle.pid, settings.engine_memory_soft_mb + 1, 0, over_soft_limit=True)]

            pool.spawner.status = over_limit
            await pool.maintenance_tick()
            still_live_while_busy = pool.is_live(a)
            await busy
            recycled = await eventually(lambda: not pool.is_live(a), 10)
            return still_live_while_busy, recycled
        finally:
            await pool.close()

    still_live_while_busy, recycled = run(scenario())
    assert still_live_while_busy is True
    assert recycled is True


def test_delete_data_removes_folder_spool_and_row_for_that_account_only(settings, database):
    a = insert_account(database, settings, 1)
    b = insert_account(database, settings, 2)

    async def scenario():
        pool = make_pool(settings, database)
        await pool.start(maintenance=False)
        try:
            await pool.ensure(a, 1)
            await pool.ensure(b, 2)
            await pool.stop(a, 1)
            queued = await pool.capture(a, 1, "/outcomes/record", b'{"nonce":"pending"}', {})
            spool_file = pool.spool.path_for(1)
            had_spool = spool_file.exists()
            await pool.delete_data(a, 1)
            return queued, had_spool, spool_file.exists(), pool.is_live(b)
        finally:
            await pool.close()

    queued, had_spool, spool_left, b_live = run(scenario())
    assert queued.spooled and had_spool and not spool_left
    assert not (settings.tenants_dir / "1").exists()
    assert engine_row(database, a) is None
    assert (settings.tenants_dir / "2" / "marker.json").exists() and b_live is True


def test_health_and_snapshot_never_spawn_and_prewarm_never_evicts(settings, database):
    a = insert_account(database, settings, 1)
    b = insert_account(database, settings, 2)

    async def scenario():
        pool = make_pool(settings, database, max_live=1)
        await pool.start(maintenance=False)
        try:
            health, snapshot = pool.health(), pool.snapshot()
            no_spawns = pool.spawn_count
            await pool.ensure(a, 1)
            pool.prewarm(b, 2)
            await asyncio.sleep(0.5)
            evicted = not pool.is_live(a)
            await pool.stop(a, 1)
            pool.prewarm(b, 2)
            warmed = await eventually(lambda: pool.is_live(b), 10)
            return health, snapshot, no_spawns, evicted, warmed
        finally:
            await pool.close()

    health, snapshot, no_spawns, evicted, warmed = run(scenario())
    assert no_spawns == 0 and health["running"] == 0 and snapshot == []
    assert evicted is False
    assert warmed is True  # control: prewarm does start an engine when a slot is free


def test_close_stops_every_engine(settings, database):
    a = insert_account(database, settings, 1)
    b = insert_account(database, settings, 2)

    async def scenario():
        pool = make_pool(settings, database)
        await pool.start()
        await pool.ensure(a, 1)
        await pool.ensure(b, 2)
        running_before = len(await pool.spawner.status())
        await pool.close()
        with pytest.raises(EngineUnavailable):
            await pool.ensure(a, 1)
        return running_before, await pool.spawner.status()

    running_before, running_after = run(scenario())
    assert running_before == 2 and running_after == []
    assert (settings.tenants_dir / "1" / "stopped.json").exists() or engine_row(database, a)["state"] == "stopped"
