"""Engine pool: one engine process per active customer (CONTRACTS sections 3, 5, 7 and 14).

The pool is the only gateway component that talks to engines. It

* spawns an engine on demand (`ensure`) with a fresh 43-character worker token and a fresh
  services token signed for that customer, checks `/livez`, and replays the customer's
  capture spool before the engine serves anything else;
* keeps at most `ENGINE_MAX_LIVE` engines, evicting the least recently used idle engine
  and refusing (503) when every engine is busy;
* stops engines idle for `ENGINE_IDLE_SECONDS`, recycles engines over the soft memory
  limit once they are idle, and applies operator stop requests written by the CLI;
* treats unexpected exits and failed spawns as crashes: restarts back off 1 s, 5 s, 30 s,
  and three crashes within ten minutes open a circuit breaker (503 with Retry-After and an
  owner alert in `service_state`);
* forwards requests and streams over httpx (loopback TCP in development, the engine's unix
  socket in production) with the worker token and only the allowlisted headers.

Engines are keyed by account id and every call must also name the account's tenant_seq;
a mismatch is refused. Health endpoints must use `health()`, which never spawns.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import itertools
import json
import logging
import math
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import httpx

from ..config import Settings
from ..db import Database
from .spawner import EngineSecrets, SpawnError, Spawner, new_worker_token, validate_tenant_seq
from .spool import SPOOLABLE_PATHS, CaptureSpool, SpoolEntry

log = logging.getLogger(__name__)

ENGINE_ROUTES = frozenset({
    ("GET", "/livez"),
    ("POST", "/browser/assist"),
    ("POST", "/browser/assist/stream"),
    ("POST", "/snapshots/profile"),
    ("POST", "/snapshots/metrics"),
    ("POST", "/outcomes/record"),
    ("POST", "/images/feedback"),
    ("POST", "/preferences/confirm"),
    ("POST", "/own-material/list"),
    ("POST", "/own-material/add"),
    ("POST", "/own-material/remove"),
    ("GET", "/profile/export"),
})
STREAM_PATH = "/browser/assist/stream"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
TIME_ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,3}$")
MAX_STREAM_LINE_BYTES = 4 * 1024 * 1024
ALERT_KEY_PREFIX = "alert.engine_circuit."


# --------------------------------------------------------------------------- errors


class PoolError(Exception):
    """A request the pool cannot serve right now: a 503 `ai_unavailable` with Retry-After."""

    code = "ai_unavailable"
    http_status = 503

    def __init__(self, message: str, retry_after_s: int) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_s = max(1, int(retry_after_s))


class PoolBusy(PoolError):
    """Every engine slot belongs to an engine that is serving a request."""


class CircuitOpen(PoolError):
    """This customer's engine crashed too often within the breaker window."""


class EngineUnavailable(PoolError):
    """The engine could not be started or stopped answering."""


class EngineTimeout(EngineUnavailable):
    """The engine did not answer within the request's time limit."""


class EngineStreamBroken(EngineUnavailable):
    """The engine connection broke in the middle of a stream."""


class EngineMismatch(RuntimeError):
    """The caller named a tenant_seq that does not belong to the account. Always a bug."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class EngineHandle:
    account_id: str
    tenant_seq: int
    pid: int
    listen: str


@dataclass(frozen=True)
class CaptureResult:
    status: int
    body: bytes
    spooled: bool


@dataclass
class PoolOptions:
    max_live: int = 6
    idle_seconds: float = 1800.0
    livez_timeout_s: float = 5.0
    backoff_s: tuple[float, ...] = (1.0, 5.0, 30.0)
    breaker_crashes: int = 3
    breaker_window_s: float = 600.0
    tick_s: float = 5.0
    status_poll_s: float = 10.0
    busy_retry_after_s: int = 10
    unavailable_retry_after_s: int = 5
    stop_grace_s: float = 10.0
    request_timeout_s: float = 60.0
    capture_timeout_s: float = 30.0
    last_used_write_s: float = 30.0
    stream_total_s: float = 240.0
    # ✦ (`POST /assist`): the engine budgets 50 s and the extension waits 60 s, so a hung engine frees
    # the account's slot, and the customer hears why, before the extension gives up on its own.
    assist_total_s: float = 56.0
    stream_hard_wall_s: float = 300.0
    heartbeat_s: float = 10.0
    queued_notice_s: float = 0.25
    spool_max_bytes: int = 1024 * 1024

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> "PoolOptions":
        options = cls(max_live=settings.engine_max_live, idle_seconds=float(settings.engine_idle_seconds))
        for name, value in overrides.items():
            if not hasattr(options, name):
                raise TypeError(f"unknown engine pool option {name!r}")
            setattr(options, name, value)
        return options


@dataclass
class InflightRequest:
    """A metered request being served, findable by `GET/POST /requests/{key}`."""

    account_id: str
    tenant_seq: int
    idempotency_key: str
    reservation_id: str
    kind: str  # "assist" | "stream"
    cancel_requested: bool = False
    control: asyncio.Event = field(default_factory=asyncio.Event)


class InflightRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], InflightRequest] = {}

    def add(self, item: InflightRequest) -> None:
        self._items[(item.account_id, item.idempotency_key)] = item

    def get(self, account_id: str, idempotency_key: str) -> InflightRequest | None:
        return self._items.get((account_id, idempotency_key))

    def remove(self, item: InflightRequest) -> None:
        key = (item.account_id, item.idempotency_key)
        if self._items.get(key) is item:
            del self._items[key]

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class _Engine:
    account_id: str
    tenant_seq: int
    state: str = "starting"  # starting | running | stopping
    handle: EngineHandle | None = None
    client: httpx.AsyncClient | None = None
    inflight: int = 0
    last_used: float = 0.0
    last_used_written: float = 0.0
    use_order: int = 0  # strictly increasing; the clock can tick too coarsely to order uses
    started_at: int = 0
    ready: bool = False
    stop_expected: bool = False
    recycle: bool = False
    spawn_task: asyncio.Task | None = None
    stopped: asyncio.Event = field(default_factory=asyncio.Event)


MintToken = Callable[[bytes, str, int], str]


def _default_mint(key: bytes, tenant_id: str, now: int) -> str:
    """Services workstream's signer (CONTRACTS 4.3), imported when first needed."""
    tokens = importlib.import_module("starling_server.services.tokens")
    return tokens.mint_engine_token(key, tenant_id, now)


def _header(headers: dict[str, str] | None, name: str) -> str | None:
    lowered = name.lower()
    for key, value in (headers or {}).items():
        if key.lower() == lowered:
            return value
    return None


def clean_time_zone(value: str | None) -> str | None:
    """An IANA zone name from the browser, or None. Never forwarded unvalidated."""
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 64 or not TIME_ZONE_RE.match(candidate):
        return None
    return candidate


# --------------------------------------------------------------------------- pool


class EnginePool:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        spawner: Spawner,
        *,
        options: PoolOptions | None = None,
        spool: CaptureSpool | None = None,
        mint_services_token: MintToken | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.db = db
        self.spawner = spawner
        self.options = options or PoolOptions.from_settings(settings)
        self.spool = spool or CaptureSpool(settings.data_dir / "spool", self.options.spool_max_bytes)
        self.inflight = InflightRegistry()
        self.spawn_count = 0
        self._mint = mint_services_token or _default_mint
        self._clock = clock
        self._monotonic = monotonic
        self._engines: dict[str, _Engine] = {}
        self._use_order = itertools.count(1)
        self._crashes: dict[str, deque[float]] = {}
        self._next_spawn_at: dict[str, float] = {}
        self._deleting: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._capacity_lock: asyncio.Lock | None = None
        self._maintenance: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_status_poll = 0.0
        self._started = False
        self._closed = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self, *, maintenance: bool = True) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._capacity_lock = asyncio.Lock()
        self.spawner.set_exit_callback(self._on_spawner_exit)
        await self._db_write(lambda conn: conn.execute(
            "UPDATE engines SET state = 'stopped', pid = NULL, listen = NULL WHERE state != 'stopped'"))
        if not self.spawner.reports_exits:
            # Engines kept by the supervisor across a gateway restart are unreachable (their
            # worker tokens died with the old process): stop them.
            try:
                leftovers = await self.spawner.status()
            except SpawnError as exc:
                log.warning("could not list engines at startup: %s", exc)
                leftovers = []
            for item in leftovers:
                log.warning("stopping engine %s left over from a previous gateway process", item.tenant_seq)
                with contextlib.suppress(SpawnError):
                    await self.spawner.stop(item.tenant_seq, self.options.stop_grace_s)
        if maintenance:
            self._maintenance = asyncio.create_task(self._maintenance_loop(), name="engine-pool-maintenance")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._maintenance is not None:
            self._maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._maintenance
        await asyncio.gather(
            *(self._stop_entry(entry, "shutdown") for entry in list(self._engines.values())),
            return_exceptions=True,
        )
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.spawner.close()

    def _check_open(self) -> None:
        if self._closed:
            raise EngineUnavailable("the engine pool is shutting down", self.options.unavailable_retry_after_s)

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ ensure and spawn

    async def ensure(self, account_id: str, tenant_seq: int, *, allow_evict: bool = True) -> EngineHandle:
        """Return a running engine for the account, spawning one if needed (prewarm)."""
        entry = await self._ready_entry(account_id, tenant_seq, allow_evict=allow_evict)
        assert entry.handle is not None
        return entry.handle

    async def _ready_entry(self, account_id: str, tenant_seq: int, *, allow_evict: bool = True) -> _Engine:
        seq = validate_tenant_seq(tenant_seq)
        if not self._started:
            await self.start()
        for _attempt in range(5):
            self._check_open()
            if account_id in self._deleting:
                raise EngineUnavailable("this customer's data is being deleted", self.options.unavailable_retry_after_s)
            entry = self._engines.get(account_id)
            if entry is not None and entry.tenant_seq != seq:
                raise EngineMismatch("tenant_seq does not belong to this account's engine")
            if entry is None:
                await self._start_spawn(account_id, seq, allow_evict)
            elif entry.state == "running" and entry.ready:
                return entry
            elif entry.state == "starting" and entry.spawn_task is not None:
                await asyncio.shield(entry.spawn_task)
            else:
                await entry.stopped.wait()
        raise EngineUnavailable("the engine could not be started", self.options.unavailable_retry_after_s)

    async def _start_spawn(self, account_id: str, seq: int, allow_evict: bool) -> None:
        self._check_breaker(account_id)
        assert self._capacity_lock is not None
        async with self._capacity_lock:
            if account_id in self._engines:
                return  # started by a concurrent caller; the caller's loop picks it up
            if any(other.tenant_seq == seq for other in self._engines.values()):
                raise EngineMismatch("tenant_seq already belongs to another account's engine")
            victim = None
            if len(self._engines) >= self.options.max_live:
                if allow_evict:
                    victim = self._lru_idle()
                if victim is None:
                    raise PoolBusy("every engine is busy", self.options.busy_retry_after_s)
                victim.state = "stopping"
                victim.stop_expected = True
            entry = _Engine(account_id=account_id, tenant_seq=seq, last_used=self._monotonic())
            self._engines[account_id] = entry
            entry.spawn_task = self._track(asyncio.create_task(self._spawn(entry, victim), name=f"engine-spawn-{seq}"))

    def _lru_idle(self) -> _Engine | None:
        idle = [e for e in self._engines.values() if e.state == "running" and e.inflight == 0]
        return min(idle, key=lambda e: e.use_order, default=None)

    async def _spawn(self, entry: _Engine, victim: _Engine | None) -> EngineHandle:
        seq = entry.tenant_seq
        try:
            if victim is not None:
                # The idle engine exits while this one starts: the person waiting for a first draft
                # should not also wait out someone else's shutdown (up to `stop_grace_s`). For that
                # moment the machine holds one engine more than ENGINE_MAX_LIVE; the one leaving
                # does no work, and `close` waits for it like any other.
                log.info("evicting idle engine %s to start engine %s", victim.tenant_seq, seq)
                self._track(asyncio.create_task(self._do_stop(victim, "evicted"), name=f"engine-evict-{victim.tenant_seq}"))
            delay = self._next_spawn_at.get(entry.account_id, 0.0) - self._monotonic()
            if delay > 0:
                log.info("engine %s restarts in %.1f seconds (crash backoff)", seq, delay)
                await asyncio.sleep(delay)
            now = int(self._clock())
            worker_token = new_worker_token()
            services_token = self._mint(self.settings.services_token_key, entry.account_id, now)
            self.spawn_count += 1
            spawned = await self.spawner.spawn(seq, entry.account_id, EngineSecrets(worker_token, services_token))
            base_url, transport = self.spawner.transport_for(spawned.listen)
            entry.client = httpx.AsyncClient(
                base_url=base_url,
                transport=transport,
                headers={"Authorization": f"Bearer {worker_token}"},
                timeout=httpx.Timeout(self.options.request_timeout_s, connect=5.0),
                trust_env=False,  # never route worker tokens through proxy settings
                follow_redirects=False,
                # Engines listen on a Unix socket or plain loopback HTTP, never TLS: skip building a
                # certificate store for it (~0.25 s per start, on the loop every customer shares).
                verify=not base_url.startswith("http://"),
            )
            await self._check_livez(entry)
            entry.handle = EngineHandle(entry.account_id, seq, spawned.pid, spawned.listen)
            entry.started_at = now
            await self._replay_spool(entry)  # marks the engine running and ready
            entry.last_used = self._monotonic()
            entry.use_order = next(self._use_order)
            await self._db_write(lambda conn: self._upsert_running(conn, entry, now))
            log.info("engine %s started (pid %s)", seq, spawned.pid)
            return entry.handle
        except asyncio.CancelledError:
            await self._abort_spawn(entry, "cancelled", count_crash=False)
            raise
        except Exception as exc:
            crash = isinstance(exc, (SpawnError, httpx.HTTPError))
            reason = str(exc) if isinstance(exc, SpawnError) else exc.__class__.__name__
            log.warning("engine %s failed to start: %s", seq, reason)
            await self._abort_spawn(entry, reason, count_crash=crash)
            if isinstance(exc, PoolError):
                raise
            raise EngineUnavailable("the engine could not be started", self.options.unavailable_retry_after_s) from exc

    async def _check_livez(self, entry: _Engine) -> None:
        assert entry.client is not None
        deadline = self._monotonic() + self.options.livez_timeout_s
        while True:
            try:
                response = await entry.client.get("/livez", timeout=2.0)
                if response.status_code == 200 and response.json().get("ok") is True:
                    return
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            if self._monotonic() >= deadline:
                raise SpawnError("engine did not pass its /livez check")
            await asyncio.sleep(0.1)

    async def _abort_spawn(self, entry: _Engine, reason: str, *, count_crash: bool) -> None:
        entry.state = "stopping"
        entry.stop_expected = True
        if entry.client is not None:
            with contextlib.suppress(Exception):
                await entry.client.aclose()
        with contextlib.suppress(Exception):
            await self.spawner.stop(entry.tenant_seq, 0.0)
        if self._engines.get(entry.account_id) is entry:
            del self._engines[entry.account_id]
        entry.stopped.set()
        if count_crash:
            await self._record_crash(entry.account_id, entry.tenant_seq, reason)

    # ------------------------------------------------------------------ crashes and breaker

    def _crash_times(self, account_id: str) -> deque[float]:
        history = self._crashes.setdefault(account_id, deque())
        cutoff = self._monotonic() - self.options.breaker_window_s
        while history and history[0] < cutoff:
            history.popleft()
        return history

    def breaker_retry_after(self, account_id: str) -> int | None:
        """Seconds until this customer's circuit breaker closes, or None when it is closed."""
        history = self._crash_times(account_id)
        needed = self.options.breaker_crashes
        if len(history) < needed:
            return None
        remaining = self.options.breaker_window_s - (self._monotonic() - history[-needed])
        return max(1, math.ceil(remaining))

    def _check_breaker(self, account_id: str) -> None:
        retry_after = self.breaker_retry_after(account_id)
        if retry_after is not None:
            raise CircuitOpen("this customer's engine keeps crashing", retry_after)

    async def _record_crash(self, account_id: str, tenant_seq: int, reason: str) -> None:
        history = self._crash_times(account_id)
        history.append(self._monotonic())
        count = len(history)
        backoff = self.options.backoff_s
        delay = backoff[min(count, len(backoff)) - 1] if backoff else 0.0
        self._next_spawn_at[account_id] = self._monotonic() + delay
        await self._db_write(lambda conn: conn.execute(
            "UPDATE engines SET state = 'crashed', pid = NULL, listen = NULL, restarts = restarts + 1 "
            "WHERE account_id = ?", (account_id,)))
        if count >= self.options.breaker_crashes:
            retry_after = self.breaker_retry_after(account_id) or 1
            log.error("engine %s crashed %s times within %.0f seconds; circuit breaker open for %s seconds",
                      tenant_seq, count, self.options.breaker_window_s, retry_after)
            await self._write_alert(account_id, tenant_seq, count, retry_after)
        else:
            log.warning("engine %s crashed (%s); next start after %.1f seconds", tenant_seq, reason, delay)

    async def _write_alert(self, account_id: str, tenant_seq: int, crashes: int, retry_after: int) -> None:
        now = int(self._clock())
        value = json.dumps({
            "kind": "engine_circuit_open",
            "severity": "warning",
            "account_id": account_id,
            "tenant_seq": tenant_seq,
            "crashes": crashes,
            "window_s": int(self.options.breaker_window_s),
            "opened_at": now,
            "retry_after_s": retry_after,
        }, separators=(",", ":"))
        await self._db_write(lambda conn: conn.execute(
            "INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (ALERT_KEY_PREFIX + str(tenant_seq), value, now)))

    def _on_spawner_exit(self, tenant_seq: int, returncode: int | None, reason: str) -> None:
        entry = next((e for e in self._engines.values() if e.tenant_seq == tenant_seq), None)
        if entry is None or entry.stop_expected or entry.state != "running":
            return
        self._track(asyncio.ensure_future(self._handle_crash(entry, f"{reason} with code {returncode}")))

    async def _handle_crash(self, entry: _Engine, reason: str) -> None:
        if self._engines.get(entry.account_id) is not entry or entry.stop_expected:
            return
        entry.state = "stopping"
        entry.stop_expected = True
        del self._engines[entry.account_id]
        log.warning("engine %s stopped unexpectedly (%s)", entry.tenant_seq, reason)
        if entry.client is not None:
            with contextlib.suppress(Exception):
                await entry.client.aclose()
        with contextlib.suppress(Exception):
            await self.spawner.stop(entry.tenant_seq, 0.0)
        entry.stopped.set()
        await self._record_crash(entry.account_id, entry.tenant_seq, reason)

    async def _on_transport_error(self, entry: _Engine) -> None:
        if entry.state != "running" or entry.stop_expected:
            return
        try:
            running = await self.spawner.is_running(entry.tenant_seq)
        except SpawnError:
            return
        if not running:
            await self._handle_crash(entry, "connection lost")

    # ------------------------------------------------------------------ forwarding

    def _claim_engine(self, entry: _Engine) -> None:
        entry.inflight += 1
        entry.last_used = self._monotonic()
        entry.use_order = next(self._use_order)

    def _release_engine(self, entry: _Engine) -> None:
        entry.inflight -= 1
        entry.last_used = self._monotonic()
        entry.use_order = next(self._use_order)
        if entry.inflight == 0 and entry.recycle and entry.state == "running":
            self._track(asyncio.ensure_future(self._stop_entry(entry, "memory recycle")))

    @contextlib.asynccontextmanager
    async def _claim(self, account_id: str, tenant_seq: int) -> AsyncIterator[_Engine]:
        entry = await self._ready_entry(account_id, tenant_seq)
        self._claim_engine(entry)  # synchronously after the check: nothing can evict it in between
        try:
            yield entry
        finally:
            self._release_engine(entry)

    def _request_headers(self, headers: dict[str, str] | None, body: bytes | None) -> dict[str, str]:
        """Only the contract headers reach an engine (CONTRACTS 3.2); the client's own
        Authorization and every other header are dropped."""
        out: dict[str, str] = {}
        request_id = _header(headers, "X-Starling-Request-Id")
        if request_id:
            if not REQUEST_ID_RE.match(request_id):
                raise ValueError("invalid X-Starling-Request-Id")
            out["X-Starling-Request-Id"] = request_id
        time_zone = clean_time_zone(_header(headers, "X-Starling-Time-Zone"))
        if time_zone:
            out["X-Starling-Time-Zone"] = time_zone
        if body:
            out["Content-Type"] = "application/json"
        return out

    async def request(
        self,
        account_id: str,
        tenant_seq: int,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
        timeout_s: float,
    ) -> tuple[int, bytes]:
        """Forward one allowlisted request; returns (status, body)."""
        method = method.upper()
        if (method, path) not in ENGINE_ROUTES or path == STREAM_PATH:
            raise ValueError(f"{method} {path} is not a forwardable engine route")
        forwarded = self._request_headers(headers, body)
        async with self._claim(account_id, tenant_seq) as entry:
            assert entry.client is not None
            try:
                response = await entry.client.request(
                    method, path, content=body or None, headers=forwarded,
                    timeout=httpx.Timeout(timeout_s, connect=5.0))
            except httpx.TimeoutException as exc:
                raise EngineTimeout("the engine did not answer in time", self.options.unavailable_retry_after_s) from exc
            except httpx.HTTPError as exc:
                await self._on_transport_error(entry)
                raise EngineUnavailable("the engine stopped answering", self.options.unavailable_retry_after_s) from exc
            return response.status_code, response.content

    async def capture(
        self, account_id: str, tenant_seq: int, path: str, body: bytes, headers: dict[str, str]
    ) -> CaptureResult:
        """Forward a capture to the account's running engine, or spool it. Never spawns."""
        if path not in SPOOLABLE_PATHS:
            raise ValueError(f"{path} is not a capture route")
        seq = validate_tenant_seq(tenant_seq)
        if not self._started:
            await self.start()
        forwarded = self._request_headers(headers, body)
        time_zone = forwarded.get("X-Starling-Time-Zone")
        async with self.spool.lock(seq):
            if account_id in self._deleting:
                return CaptureResult(202, _queued(False), spooled=True)
            entry = self._engines.get(account_id)
            if entry is not None and entry.tenant_seq != seq:
                raise EngineMismatch("tenant_seq does not belong to this account's engine")
            if entry is None or entry.state != "running" or not entry.ready or entry.client is None:
                stored = await self.spool.append(seq, SpoolEntry.create(path, body, time_zone))
                return CaptureResult(202, _queued(stored), spooled=True)
            self._claim_engine(entry)
        try:
            response = await entry.client.post(path, content=body, headers=forwarded,
                                               timeout=self.options.capture_timeout_s)
        except httpx.TimeoutException as exc:
            raise EngineTimeout("the engine did not answer in time", self.options.unavailable_retry_after_s) from exc
        except httpx.HTTPError:
            await self._on_transport_error(entry)
            async with self.spool.lock(seq):
                stored = await self.spool.append(seq, SpoolEntry.create(path, body, time_zone))
            return CaptureResult(202, _queued(stored), spooled=True)
        finally:
            self._release_engine(entry)
        return CaptureResult(response.status_code, response.content, spooled=False)

    async def stream(
        self, account_id: str, tenant_seq: int, path: str, body: bytes, headers: dict[str, str]
    ) -> AsyncIterator[dict]:
        """Forward a streaming request and yield parsed engine NDJSON events (CONTRACTS 3.4).

        The engine stays claimed (never reaped or evicted) until the stream ends or the
        iterator is closed. Malformed lines are skipped; a non-200 answer yields one
        synthetic `error` event; a broken connection raises EngineStreamBroken.
        """
        if path != STREAM_PATH:
            raise ValueError(f"{path} is not the engine stream route")
        forwarded = self._request_headers(headers, body)
        async with self._claim(account_id, tenant_seq) as entry:
            assert entry.client is not None
            timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=10.0)
            try:
                async with entry.client.stream("POST", path, content=body, headers=forwarded, timeout=timeout) as response:
                    if response.status_code != 200:
                        yield {"event": "error", "error": {"code": "engine_http_error", "status": response.status_code}}
                        return
                    async with contextlib.aclosing(_ndjson_lines(response)) as lines:
                        async for line in lines:
                            try:
                                event = json.loads(line)
                            except (ValueError, UnicodeDecodeError):
                                log.warning("engine %s sent a malformed stream line", entry.tenant_seq)
                                continue
                            if isinstance(event, dict):
                                yield event
            except httpx.HTTPError as exc:
                await self._on_transport_error(entry)
                raise EngineStreamBroken("the engine connection broke", self.options.unavailable_retry_after_s) from exc

    async def cancel(self, account_id: str, tenant_seq: int, request_id: str) -> bool:
        """Ask the account's running engine to stop one request. Never spawns.
        Returns True when the engine acknowledged the cancel."""
        if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
            raise ValueError("invalid request id")
        entry = self._engines.get(account_id)
        if entry is None:
            return False
        if entry.tenant_seq != tenant_seq:
            raise EngineMismatch("tenant_seq does not belong to this account's engine")
        if entry.state != "running" or entry.client is None:
            return False
        try:
            response = await entry.client.post(f"/internal/requests/{request_id}/cancel", timeout=5.0)
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            payload = response.json()
        except ValueError:
            return True
        return not (isinstance(payload, dict) and payload.get("cancelled") is False)

    # ------------------------------------------------------------------ stop and delete

    async def stop(self, account_id: str, tenant_seq: int) -> None:
        entry = self._engines.get(account_id)
        if entry is None:
            return
        if entry.tenant_seq != tenant_seq:
            raise EngineMismatch("tenant_seq does not belong to this account's engine")
        await self._stop_entry(entry, "stopped")

    async def delete_data(self, account_id: str, tenant_seq: int) -> None:
        """Stop the engine and delete the customer's data folder, spool and registry row."""
        seq = validate_tenant_seq(tenant_seq)
        if not self._started:
            await self.start()
        self._deleting.add(account_id)
        try:
            entry = self._engines.get(account_id)
            if entry is not None:
                if entry.tenant_seq != seq:
                    raise EngineMismatch("tenant_seq does not belong to this account's engine")
                await self._stop_entry(entry, "deleted")
            async with self.spool.lock(seq):
                await self.spool.delete(seq)
                await self.spawner.delete_data(seq)
            await self._db_write(lambda conn: conn.execute("DELETE FROM engines WHERE account_id = ?", (account_id,)))
            self._crashes.pop(account_id, None)
            self._next_spawn_at.pop(account_id, None)
            log.info("engine %s data deleted", seq)
        finally:
            self._deleting.discard(account_id)

    async def _stop_entry(self, entry: _Engine, reason: str) -> None:
        if entry.state == "stopping":
            await entry.stopped.wait()
            return
        if entry.state == "starting" and entry.spawn_task is not None and not entry.spawn_task.done():
            entry.spawn_task.cancel()
            with contextlib.suppress(BaseException):
                await entry.spawn_task
            return
        entry.state = "stopping"
        entry.stop_expected = True
        await self._do_stop(entry, reason)

    async def _do_stop(self, entry: _Engine, reason: str) -> None:
        try:
            await self.spawner.stop(entry.tenant_seq, self.options.stop_grace_s)
        except SpawnError as exc:
            log.warning("engine %s did not stop cleanly: %s", entry.tenant_seq, exc)
        finally:
            if entry.client is not None:
                with contextlib.suppress(Exception):
                    await entry.client.aclose()
            if self._engines.get(entry.account_id) is entry:
                del self._engines[entry.account_id]
            entry.stopped.set()
        if reason != "deleted":
            now = int(self._clock())
            await self._db_write(lambda conn: conn.execute(
                "UPDATE engines SET state = 'stopped', pid = NULL, listen = NULL, last_used_at = ? "
                "WHERE account_id = ?", (now, entry.account_id)))
        log.info("engine %s stopped (%s)", entry.tenant_seq, reason)

    # ------------------------------------------------------------------ prewarm and health

    def prewarm(self, account_id: str, tenant_seq: int) -> None:
        """Start the account's engine in the background if a slot is free (never evicts).

        An engine that is already running counts as used: someone reading X with the extension open
        is about to press ✦, and stopping their engine for idleness would make that press cold.
        """
        running = self._engines.get(account_id)
        if running is not None and running.state == "running" and running.tenant_seq == tenant_seq:
            running.last_used = self._monotonic()
            running.use_order = next(self._use_order)
            return

        async def run() -> None:
            try:
                await self.ensure(account_id, tenant_seq, allow_evict=False)
            except (PoolError, SpawnError, EngineMismatch) as exc:
                log.info("prewarm of engine %s skipped: %s", tenant_seq, exc.__class__.__name__)
            except Exception:
                log.exception("prewarm of engine %s failed", tenant_seq)

        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._track(loop.create_task(run()))
        elif self._loop is not None and not self._loop.is_closed():
            target = self._loop
            target.call_soon_threadsafe(lambda: self._track(target.create_task(run())))

    def health(self) -> dict[str, int]:
        """Counts for health and readiness endpoints. Never spawns or contacts engines."""
        states = [entry.state for entry in self._engines.values()]
        return {
            "running": states.count("running"),
            "starting": states.count("starting"),
            "stopping": states.count("stopping"),
            "busy": sum(1 for entry in self._engines.values() if entry.inflight),
            "max_live": self.options.max_live,
        }

    def snapshot(self) -> list[dict[str, Any]]:
        now = self._monotonic()
        return [
            {
                "tenant_seq": entry.tenant_seq,
                "state": entry.state,
                "pid": entry.handle.pid if entry.handle else None,
                "inflight": entry.inflight,
                "idle_s": round(now - entry.last_used, 1),
                "ready": entry.ready,
            }
            for entry in self._engines.values()
        ]

    def is_live(self, account_id: str) -> bool:
        entry = self._engines.get(account_id)
        return entry is not None and entry.state == "running" and entry.ready

    # ------------------------------------------------------------------ spool replay

    async def _replay_spool(self, entry: _Engine) -> None:
        assert entry.client is not None
        seq = entry.tenant_seq
        async with self.spool.lock(seq):
            entries = await self.spool.read(seq)
            delivered = 0
            for item in entries:
                headers = {"Content-Type": "application/json"}
                time_zone = clean_time_zone(item.time_zone)
                if time_zone:
                    headers["X-Starling-Time-Zone"] = time_zone
                try:
                    response = await entry.client.post(item.path, content=item.body, headers=headers,
                                                       timeout=self.options.capture_timeout_s)
                except httpx.HTTPError:
                    break
                if response.status_code >= 500:
                    break
                if response.status_code >= 400:
                    log.warning("engine %s rejected a spooled capture (%s); dropped", seq, response.status_code)
                delivered += 1
            if entries or self.spool.size(seq):
                await self.spool.replace(seq, entries[delivered:])
            if entries:
                log.info("engine %s replayed %s of %s spooled captures", seq, delivered, len(entries))
            entry.state = "running"
            entry.ready = True

    # ------------------------------------------------------------------ maintenance

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(self.options.tick_s)
            try:
                await self.maintenance_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("engine pool maintenance failed")

    async def maintenance_tick(self) -> None:
        """Idle reaping, exit and memory polling, operator stop requests, last-used writes."""
        now = self._monotonic()
        idle = [
            entry for entry in self._engines.values()
            if entry.state == "running" and entry.inflight == 0 and now - entry.last_used >= self.options.idle_seconds
        ]
        if idle:
            await asyncio.gather(*(self._stop_entry(entry, "idle") for entry in idle), return_exceptions=True)
        if now - self._last_status_poll >= self.options.status_poll_s:
            self._last_status_poll = now
            await self._poll_status()
        await self._apply_stop_requests()
        await self._write_last_used()

    async def _poll_status(self) -> None:
        if not self._engines:
            return
        try:
            statuses = {item.tenant_seq: item for item in await self.spawner.status()}
        except SpawnError as exc:
            log.warning("could not read engine status: %s", exc)
            return
        memory: list[tuple[int, str]] = []
        for entry in list(self._engines.values()):
            if entry.state != "running":
                continue
            status = statuses.get(entry.tenant_seq)
            if status is None:
                if not self.spawner.reports_exits:
                    await self._handle_crash(entry, "no longer running")
                continue
            if status.rss_mb is not None:
                memory.append((int(status.rss_mb), entry.account_id))
            over = status.over_soft_limit or (
                status.rss_mb is not None and status.rss_mb >= self.settings.engine_memory_soft_mb)
            if over and not entry.recycle:
                entry.recycle = True
                log.warning("engine %s is over the soft memory limit; restarting it when idle", entry.tenant_seq)
                if entry.inflight == 0:
                    await self._stop_entry(entry, "memory recycle")
        if memory:  # `engines-status` and the owner dashboard read this column
            await self._db_write(lambda conn: conn.executemany(
                "UPDATE engines SET rss_mb = ? WHERE account_id = ?", memory))

    async def _apply_stop_requests(self) -> None:
        try:
            with self.db.read() as conn:
                accounts = [row[0] for row in conn.execute("SELECT account_id FROM engines WHERE state = 'stop_requested'")]
        except (sqlite3.Error, RuntimeError):
            return
        for account_id in accounts:
            entry = self._engines.get(account_id)
            if entry is not None:
                log.info("engine %s stop requested by an operator", entry.tenant_seq)
                await self._stop_entry(entry, "operator stop")
            await self._db_write(lambda conn, target=account_id: conn.execute(
                "UPDATE engines SET state = 'stopped', pid = NULL, listen = NULL "
                "WHERE account_id = ? AND state = 'stop_requested'", (target,)))

    async def _write_last_used(self) -> None:
        now_mono, now_wall = self._monotonic(), self._clock()
        updates = []
        for entry in self._engines.values():
            if entry.state == "running" and entry.last_used - entry.last_used_written >= self.options.last_used_write_s:
                entry.last_used_written = entry.last_used
                updates.append((int(now_wall - (now_mono - entry.last_used)), entry.account_id))
        if updates:
            await self._db_write(lambda conn: conn.executemany(
                "UPDATE engines SET last_used_at = ? WHERE account_id = ?", updates))

    # ------------------------------------------------------------------ registry rows

    @staticmethod
    def _upsert_running(conn: sqlite3.Connection, entry: _Engine, now: int) -> None:
        assert entry.handle is not None
        conn.execute(
            "INSERT INTO engines (account_id, tenant_seq, pid, listen, state, started_at, last_used_at, restarts) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?, 0) "
            "ON CONFLICT(account_id) DO UPDATE SET tenant_seq = excluded.tenant_seq, pid = excluded.pid, "
            "listen = excluded.listen, state = 'running', started_at = excluded.started_at, "
            "last_used_at = excluded.last_used_at",
            (entry.account_id, entry.tenant_seq, entry.handle.pid, entry.handle.listen, now, now),
        )

    async def _db_write(self, fn: Callable[[sqlite3.Connection], Any]) -> None:
        """Registry rows are bookkeeping for operators; a failed write never fails a request."""
        try:
            await self.db.write_async(fn)
        except sqlite3.IntegrityError:
            log.debug("engine registry write skipped (constraint)")
        except Exception:
            log.exception("engine registry write failed")


def _queued(stored: bool) -> bytes:
    return json.dumps({"ok": True, "queued": stored}).encode("utf-8")


async def _ndjson_lines(response: httpx.Response) -> AsyncIterator[bytes]:
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            line = bytes(buffer[:index]).strip()
            del buffer[: index + 1]
            if line:
                yield line
        if len(buffer) > MAX_STREAM_LINE_BYTES:
            raise EngineStreamBroken("the engine sent an oversized stream line", 5)
    if buffer.strip():
        yield bytes(buffer).strip()
