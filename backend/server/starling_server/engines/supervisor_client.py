"""Production spawner: asks the root supervisor to start and stop engines (CONTRACTS section 5).

Newline-delimited JSON over `/run/starling/spawner.sock` (mode 0660, group `starling-gw`),
one connection per operation. Engine exits are not pushed by the supervisor; the pool
notices them by polling `status()` and when a forwarded request cannot connect.
Requests carry secrets, so neither payloads nor supervisor replies are ever logged.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from ..config import Settings
from .spawner import (
    EngineSecrets,
    EngineStatus,
    ExitCallback,
    SpawnedEngine,
    SpawnError,
    base_url_and_transport,
    engine_socket_path,
    unix_listen_path,
    validate_tenant_id,
    validate_tenant_seq,
    validate_worker_token,
)

DEFAULT_SOCKET = "/run/starling/spawner.sock"
MAX_RESPONSE_BYTES = 1024 * 1024
_UNSAFE_TEXT = re.compile(r"[^A-Za-z0-9 _.,:;()/'=-]")


class SupervisorClient:
    kind = "supervisor"
    reports_exits = False

    def __init__(
        self,
        settings: Settings,
        socket_path: str = DEFAULT_SOCKET,
        spawn_timeout_s: float = 45.0,
        call_timeout_s: float = 15.0,
    ) -> None:
        self._settings = settings
        self._socket_path = socket_path
        self._spawn_timeout_s = spawn_timeout_s
        self._call_timeout_s = call_timeout_s
        self._callback: ExitCallback | None = None

    def set_exit_callback(self, callback: ExitCallback | None) -> None:
        self._callback = callback

    def transport_for(self, listen: str) -> tuple[str, httpx.AsyncBaseTransport | None]:
        unix_listen_path(listen)  # production engines listen on unix sockets only
        return base_url_and_transport(listen)

    async def spawn(self, tenant_seq: int, tenant_id: str, secrets: EngineSecrets) -> SpawnedEngine:
        seq = validate_tenant_seq(tenant_seq)
        try:
            validate_tenant_id(tenant_id)
            validate_worker_token(secrets.worker_token)
        except ValueError as exc:
            raise SpawnError(str(exc)) from exc
        response = await self._call(
            {
                "op": "spawn",
                "tenant_seq": seq,
                "tenant_id": tenant_id,
                "secrets": {"worker_token": secrets.worker_token, "services_token": secrets.services_token},
            },
            self._spawn_timeout_s,
        )
        pid, listen = response.get("pid"), response.get("listen")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise SpawnError("engine supervisor returned no valid pid")
        if listen != "unix:" + engine_socket_path(self._settings.run_dir, seq):
            raise SpawnError("engine supervisor returned an unexpected socket path")
        return SpawnedEngine(seq, tenant_id, pid, listen, int(time.time()))

    async def stop(self, tenant_seq: int, grace_s: float = 10.0) -> None:
        seq = validate_tenant_seq(tenant_seq)
        grace = max(0.0, min(float(grace_s), 60.0))
        await self._call({"op": "stop", "tenant_seq": seq, "grace_s": grace}, grace + self._call_timeout_s)

    async def delete_data(self, tenant_seq: int) -> None:
        seq = validate_tenant_seq(tenant_seq)
        await self._call({"op": "delete_data", "tenant_seq": seq}, 120.0)

    async def status(self) -> list[EngineStatus]:
        response = await self._call({"op": "status"}, self._call_timeout_s)
        engines = response.get("engines")
        if not isinstance(engines, list):
            raise SpawnError("engine supervisor returned no engine list")
        result = []
        for item in engines:
            if not isinstance(item, dict):
                continue
            try:
                seq = validate_tenant_seq(item.get("tenant_seq"))
            except ValueError:
                continue
            pid, rss, started = item.get("pid"), item.get("rss_mb"), item.get("started_at")
            if isinstance(pid, bool) or not isinstance(pid, int):
                continue
            result.append(EngineStatus(
                tenant_seq=seq,
                pid=pid,
                rss_mb=rss if isinstance(rss, int) and not isinstance(rss, bool) else None,
                started_at=started if isinstance(started, int) and not isinstance(started, bool) else 0,
                over_soft_limit=item.get("over_soft_limit") is True,
            ))
        return result

    async def is_running(self, tenant_seq: int) -> bool:
        return any(item.tenant_seq == tenant_seq for item in await self.status())

    async def close(self) -> None:
        return None

    async def _call(self, payload: dict, timeout: float) -> dict:
        op = payload.get("op")
        opener = getattr(asyncio, "open_unix_connection", None)
        if opener is None:
            raise SpawnError("unix sockets are not available on this platform")
        try:
            reader, writer = await asyncio.wait_for(opener(self._socket_path, limit=MAX_RESPONSE_BYTES), 5.0)
        except (OSError, asyncio.TimeoutError) as exc:
            raise SpawnError("engine supervisor is not reachable") from exc
        try:
            writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.LimitOverrunError) as exc:
            raise SpawnError(f"engine supervisor did not answer {op}") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        try:
            response = json.loads(line)
        except ValueError as exc:
            raise SpawnError(f"engine supervisor sent an invalid reply to {op}") from exc
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise SpawnError(f"engine supervisor sent an invalid reply to {op}")
        if not response["ok"]:
            reason = _UNSAFE_TEXT.sub("", str(response.get("error") or "unknown error"))[:200]
            raise SpawnError(f"engine supervisor refused {op}: {reason}")
        return response
