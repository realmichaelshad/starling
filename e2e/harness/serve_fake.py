"""Entrypoint for the fake providers: `python -m e2e.harness.serve_fake <openai|stripe|google>`.

Binds the port named by `E2E_PORT` on 127.0.0.1, then prints one line on stdout,
`READY {"listen": "http://127.0.0.1:<port>", "pid": <int>}`, the same shape the services
process uses, so the harness waits for all of them the same way.

The fake Stripe also binds `E2E_TLS_PORT` and serves the same application there over TLS with
a throwaway certificate: the hosted Checkout and Portal URLs must be https, because the
gateway refuses a session URL that is not. That listener runs on a second thread, so the main
thread keeps uvicorn's signal handling and one CTRL_BREAK still stops the process.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import threading
from pathlib import Path

import uvicorn


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0x7FFFFFFB), 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


def _config(app, **extra) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        log_config=None,
        log_level=os.environ.get("E2E_LOG_LEVEL", "warning"),
        access_log=os.environ.get("E2E_ACCESS_LOG") == "1",
        lifespan="on",
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=5,
        **extra,
    )


def build(kind: str, base_url: str, pages_base_url: str | None):
    if kind == "openai":
        from .fake_openai import create_app

        return create_app(api_key=os.environ["E2E_AI_API_KEY"])
    if kind == "stripe":
        from .fake_stripe import create_app

        return create_app(
            api_key=os.environ["E2E_STRIPE_API_KEY"],
            webhook_secret=os.environ["E2E_STRIPE_WEBHOOK_SECRET"],
            gateway_url=os.environ["E2E_GATEWAY_URL"],
            base_url=base_url,
            pages_base_url=pages_base_url or base_url,
        )
    if kind == "google":
        from .fake_google import create_app

        return create_app(
            client_id=os.environ["E2E_GOOGLE_CLIENT_ID"],
            client_secret=os.environ["E2E_GOOGLE_CLIENT_SECRET"],
            base_url=base_url,
        )
    raise SystemExit(f"unknown fake: {kind!r}")


def _serve_tls(app, sock: socket.socket, context: ssl.SSLContext) -> threading.Thread:
    def run() -> None:
        config = _config(app)
        config.load()
        config.ssl = context  # uvicorn hands this straight to loop.create_server
        uvicorn.Server(config).run(sockets=[sock])

    thread = threading.Thread(target=run, name="fake-stripe-pages", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m e2e.harness.serve_fake <openai|stripe|google>", file=sys.stderr)
        return 2
    kind = argv[0]
    sock = _bind(int(os.environ["E2E_PORT"]))
    base_url = f"http://127.0.0.1:{int(sock.getsockname()[1])}"
    ready: dict[str, object] = {"listen": base_url, "pid": os.getpid()}

    tls_sock = None
    pages_base_url = None
    if kind == "stripe":
        tls_sock = _bind(int(os.environ["E2E_TLS_PORT"]))
        pages_base_url = f"https://127.0.0.1:{int(tls_sock.getsockname()[1])}"
        ready["pages"] = pages_base_url

    app = build(kind, base_url, pages_base_url)
    if tls_sock is not None:
        from .tls import server_context

        _serve_tls(app, tls_sock, server_context(Path(os.environ["E2E_TLS_DIR"])))

    server = uvicorn.Server(_config(app))
    original_startup = server.startup

    async def startup(sockets=None):  # type: ignore[override]
        await original_startup(sockets=sockets)
        print("READY " + json.dumps(ready, separators=(",", ":")), flush=True)

    server.startup = startup  # type: ignore[assignment]
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
