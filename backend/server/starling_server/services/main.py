"""Entrypoint of the services process: `python -m starling_server.services.main`.

Binds `STARLING_SERVICES_LISTEN`: `unix:/path` (the socket is created with mode 0660) or a loopback
`tcp:127.0.0.1:port` (port 0 picks a free port). After startup it prints one line on stdout:
`READY {"listen": "<unix:...|tcp:127.0.0.1:PORT>", "pid": <int>}`. Logs are JSON lines on stderr and
carry no prompts, outputs, keys or tokens (tracebacks are reduced to the exception type).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import sys
from dataclasses import dataclass
from typing import Any, Callable

import uvicorn

from ..config import ConfigError, Settings, ensure_valid, get_settings

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
SOCKET_MODE = 0o660
UVICORN_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")


@dataclass(frozen=True)
class Listen:
    kind: str  # "unix" | "tcp"
    path: str | None = None
    host: str | None = None
    port: int | None = None


def parse_listen(value: str) -> Listen:
    text = (value or "").strip()
    if text.startswith("unix:"):
        path = text[len("unix:"):]
        if not path:
            raise ConfigError("STARLING_SERVICES_LISTEN unix: needs a socket path")
        return Listen("unix", path=path)
    if text.startswith("tcp:"):
        address = text[len("tcp:"):]
    elif text.startswith("http://"):
        address = text[len("http://"):].rstrip("/")
    else:
        raise ConfigError("STARLING_SERVICES_LISTEN must be unix:/path or tcp:127.0.0.1:port")
    host, separator, port_text = address.rpartition(":")
    host = host.strip("[]")
    if not separator or not host or not port_text.isdigit() or int(port_text) > 65535:
        raise ConfigError("STARLING_SERVICES_LISTEN must be unix:/path or tcp:127.0.0.1:port")
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            "STARLING_SERVICES_LISTEN must be a unix socket or a loopback TCP address; the services process is never public"
        )
    return Listen("tcp", host="127.0.0.1" if host == "localhost" else host, port=int(port_text))


def bind_socket(listen: Listen) -> tuple[socket.socket, str]:
    if listen.kind == "unix":
        if not hasattr(socket, "AF_UNIX"):
            raise ConfigError("unix sockets are not available on this platform; use tcp:127.0.0.1:port")
        path = str(listen.path)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise ConfigError(f"{path} exists and is not a socket")
            os.unlink(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o117)
        try:
            sock.bind(path)
        finally:
            os.umask(previous)
        os.chmod(path, SOCKET_MODE)
        return sock, f"unix:{path}"
    family = socket.AF_INET6 if ":" in str(listen.host) else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((str(listen.host), int(listen.port or 0)))
    host, port = sock.getsockname()[:2]
    return sock, f"tcp:{host}:{port}"


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level if isinstance(logging.getLevelName(level), int) else "INFO")
    for noisy in ("httpx", "httpcore", "anthropic", "httpx2", "httpcore2", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print_ready(line: str) -> None:
    print(line, flush=True)


async def serve(
    settings: Settings,
    *,
    app: Any = None,
    ready: Callable[[str], None] | None = None,
    on_server: Callable[[uvicorn.Server], None] | None = None,
) -> int:
    from .app import create_app

    listen = parse_listen(settings.services_listen)
    application = app if app is not None else create_app(settings)
    sock, address = bind_socket(listen)
    level = settings.log_level.lower()
    config = uvicorn.Config(
        application,
        log_level=level if level in UVICORN_LEVELS else "info",
        log_config=None,
        access_log=False,
        lifespan="on",
        proxy_headers=False,
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=10,
    )
    server = uvicorn.Server(config)
    if on_server is not None:
        on_server(server)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started and not task.done():
            await asyncio.sleep(0.05)
        if task.done():
            await task
            return 1
        (ready or _print_ready)("READY " + json.dumps({"listen": address, "pid": os.getpid()}, separators=(",", ":")))
        await task
        return 0
    finally:
        sock.close()


def main(argv: list[str] | None = None) -> int:
    try:
        settings = get_settings()
        configure_logging(settings.log_level)
        ensure_valid(settings, "services")
        parse_listen(settings.services_listen)
    except ConfigError as exc:
        print(f"services: {exc}", file=sys.stderr, flush=True)
        return 2
    try:
        return asyncio.run(serve(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
