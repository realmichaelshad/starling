"""Network doubles for the SSRF guard: a scripted resolver and a network backend serving canned HTTP.

`RecordingBackend` records the address every socket was opened to, the TLS server name, and the raw
request bytes, so tests can prove the connection went to the validated IP with the real Host header.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpcore

Responder = Callable[[str, int, bytes], "bytes | Awaitable[bytes]"]


class ScriptedResolver:
    """`answers[host]` is a list of successive answers; the last one repeats."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self._answers = {host: [list(answer) for answer in sequence] for host, sequence in answers.items()}
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        sequence = self._answers.get(host)
        if not sequence:
            raise OSError("unknown host in the test resolver")
        index = min(self.calls.count(host) - 1, len(sequence) - 1)
        return list(sequence[index])


def http_response(
    status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None, reason: str = "OK"
) -> bytes:
    merged = {"Content-Length": str(len(body)), "Connection": "close"}
    merged.update(headers or {})
    head = [f"HTTP/1.1 {status} {reason}", *(f"{name}: {value}" for name, value in merged.items())]
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


def request_line_and_host(raw: bytes) -> tuple[str, str | None]:
    lines = raw.split(b"\r\n")
    host = None
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"host":
            host = value.strip().decode("latin-1")
    return lines[0].decode("latin-1"), host


class _CannedStream(httpcore.AsyncNetworkStream):
    def __init__(self, backend: "RecordingBackend", address: str, port: int) -> None:
        self._backend = backend
        self._address = address
        self._port = port
        self._written = b""
        self._pending: list[bytes] | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._pending is None:
            self._backend.requests.append((self._address, self._written))
            result = self._backend.responder(self._address, self._port, self._written)
            if asyncio.iscoroutine(result):
                result = await result
            self._pending = [result[i:i + 8192] for i in range(0, len(result), 8192)]
        return self._pending.pop(0) if self._pending else b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._written += buffer

    async def aclose(self) -> None:
        return None

    async def start_tls(self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None):
        self._backend.tls_server_names.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.connections: list[tuple[str, int]] = []
        self.tls_server_names: list[str | None] = []
        self.requests: list[tuple[str, bytes]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connections.append((host, port))
        return _CannedStream(self, host, port)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("the world fetcher must never open unix sockets")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
