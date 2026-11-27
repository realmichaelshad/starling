#!/usr/bin/env python3
"""Fake engine worker for gateway tests (standard library only).

Implements the engine worker contract of CONTRACTS section 3 closely enough to test the
engine pool without the real engine:

* reads exactly one JSON line of secrets on stdin;
* listens on `STARLING_LISTEN` (`tcp:127.0.0.1:0`, or `unix:<path>` on POSIX) and prints
  `READY {"listen", "pid", "engine_version"}`;
* answers only requests carrying `Authorization: Bearer <worker_token>`; everything else
  gets an empty 404;
* serves `/livez`, `/browser/assist`, `/browser/assist/stream` (scripted NDJSON chosen by
  the request body's `fake` object), `/internal/requests/{id}/cancel`, the capture and
  own-material routes (echo) and `/profile/export`;
* writes marker files into `STARLING_APP_DIR` (`marker.json`, `env.json`, `stdin.json`,
  `served.jsonl`, `cancels.jsonl`) so tests can see which data folder served a request.

Command-line switches simulate broken engines: `--no-ready`, `--exit-before-ready CODE`,
`--ready-delay S`, `--ready-listen VALUE`, `--log-canary`, `--ignore-term`.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ENGINE_VERSION = "0.0.0-fake"
LOG_CANARY = "CANARY-DRAFT-TEXT-7f3a"
CAPTURE_PATHS = {
    "/snapshots/profile", "/snapshots/metrics", "/outcomes/record", "/images/feedback",
    "/preferences/confirm", "/own-material/list", "/own-material/add", "/own-material/remove",
}
CANCEL_RE = re.compile(r"^/internal/requests/([A-Za-z0-9._:-]{1,128})/cancel$")

STATE: dict = {}
WRITE_LOCK = threading.Lock()
CANCELS: dict[str, threading.Event] = {}
CANCELS_LOCK = threading.Lock()


def app_dir() -> Path:
    return Path(os.environ["STARLING_APP_DIR"])


def append_json(name: str, payload: dict) -> None:
    line = json.dumps(payload, sort_keys=True) + "\n"
    with WRITE_LOCK:
        with open(app_dir() / name, "a", encoding="utf-8") as fh:
            fh.write(line)


def served_by() -> dict:
    return {"tenant_id": STATE["tenant_id"], "app_dir": str(app_dir()), "pid": os.getpid()}


def cancel_event(request_id: str | None, create: bool = True) -> threading.Event | None:
    if not request_id:
        return threading.Event() if create else None
    with CANCELS_LOCK:
        event = CANCELS.get(request_id)
        if event is None and create:
            event = CANCELS[request_id] = threading.Event()
        return event


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "engine"
    sys_version = ""

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def address_string(self) -> str:
        return "local"

    # ------------------------------------------------------------------ plumbing

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + STATE["worker_token"]
        return hmac.compare_digest(supplied.encode("latin-1", "replace"), expected.encode("latin-1"))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _deny(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body: dict | None) -> None:
        fake = body.get("fake") if isinstance(body, dict) and isinstance(body.get("fake"), dict) else {}
        nonce = body.get("nonce") if isinstance(body, dict) else None
        append_json("served.jsonl", {
            "at": time.time(),
            "method": self.command,
            "path": self.path,
            "request_id": self.headers.get("X-Starling-Request-Id"),
            "time_zone": self.headers.get("X-Starling-Time-Zone"),
            "nonce": nonce if nonce is not None else fake.get("nonce"),
            "tenant_id": STATE["tenant_id"],
            "pid": os.getpid(),
        })

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:
        if not self._authorized():
            self._deny()
            return
        if self.path == "/livez":
            self._json(200, {"ok": True})
        elif self.path == "/profile/export":
            self._record(None)
            self._json(200, {"ok": True, "posts": [], "served_by": served_by()})
        else:
            self._deny()

    def do_POST(self) -> None:
        if not self._authorized():
            self._deny()
            return
        raw = self._read_body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        cancel = CANCEL_RE.match(self.path)
        if cancel:
            event = cancel_event(cancel.group(1), create=False)
            found = event is not None
            if event is not None:
                event.set()
            append_json("cancels.jsonl", {"at": time.time(), "request_id": cancel.group(1), "found": found})
            self._json(200, {"ok": True, "cancelled": found})
            return
        if self.path == "/browser/assist":
            self._record(body)
            self._assist(body)
        elif self.path == "/browser/assist/stream":
            self._record(body)
            self._stream(body)
        elif self.path in CAPTURE_PATHS:
            self._record(body)
            if isinstance(body.get("fake_status"), int):
                self._json(body["fake_status"], {"detail": str(body.get("fake_detail", "rejected"))})
            else:
                self._json(200, {"ok": True, "path": self.path, "echo": body, "served_by": served_by()})
        else:
            self._deny()

    def _assist(self, body: dict) -> None:
        fake = body.get("fake") if isinstance(body.get("fake"), dict) else {}
        script = fake.get("script", "ok")
        request_id = self.headers.get("X-Starling-Request-Id")
        if body.get("mode") == "schedule":
            self._json(200, {"ok": True, "schedule": body.get("schedule"), "served_by": served_by()})
            return
        if script == "crash":
            os._exit(7)
        if script == "error_status":
            self._json(500, {"detail": "engine failure"})
            return
        if script == "reject":
            self._json(422, {"detail": "invalid request"})
            return
        if script == "slow":
            time.sleep(float(fake.get("delay_s", 1.0)))
        if script == "hang":
            event = cancel_event(request_id)
            event.wait(float(fake.get("hang_s", 60.0)))
            self._json(200, {"ok": False, "cancelled": True, "served_by": served_by()})
            return
        if script == "refused":  # the engine passes on a services refusal (CONTRACTS 4.1)
            answer = {"ok": False, "error": "refused upstream", "error_code": str(fake.get("code", "ai_quota")),
                      "served_by": served_by()}
            if fake.get("retry_after_s"):
                answer["retry_after_s"] = int(fake["retry_after_s"])
            self._json(200, answer)
            return
        draft = "" if script == "empty" else str(fake.get("draft", "A usable draft."))
        self._json(200, {
            "ok": True,
            "decision": {"action": "reply", "rationale": "r", "reasons": [], "likely_reaction": "",
                         "confidence": "directional", "draft": draft},
            "served_by": served_by(),
        })

    def _stream(self, body: dict) -> None:
        fake = body.get("fake") if isinstance(body.get("fake"), dict) else {}
        script = fake.get("script", "tokens")
        tokens = [str(t) for t in fake.get("tokens", ["Hel", "lo"])]
        request_id = self.headers.get("X-Starling-Request-Id")
        event = cancel_event(request_id)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(payload: dict | bytes) -> None:
            data = payload if isinstance(payload, bytes) else (json.dumps(payload) + "\n").encode("utf-8")
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        def send_tokens() -> None:
            for token in tokens:
                send({"event": "token", "text": token})
                time.sleep(float(fake.get("token_delay_s", 0.02)))

        def final(response: dict) -> None:
            response.setdefault("served_by", served_by())
            send({"event": "final", "response": response})

        try:
            time.sleep(float(fake.get("start_delay_s", 0)))
            if fake.get("emit_status", True):
                send({"event": "status", "stage": "thinking"})
            time.sleep(float(fake.get("silent_s", 0)))
            if script == "tokens":
                send_tokens()
                final({"ok": True, "answer": str(fake.get("answer", "".join(tokens)))})
            elif script == "empty":
                final({"ok": True, "answer": ""})
            elif script == "tokens_then_withheld":  # a preview, then Starling's checks withhold it
                send_tokens()
                final({"ok": True, "withheld": True, "answer": ""})
            elif script == "error":
                send_tokens() if fake.get("tokens_first") else None
                error = {"code": str(fake.get("code", "ai_unavailable")), "message": "upstream failed"}
                if fake.get("retry_after_s"):
                    error["retry_after_s"] = int(fake["retry_after_s"])
                send({"event": "error", "error": error})
            elif script in ("hang", "tokens_then_hang"):
                if script == "tokens_then_hang":
                    send_tokens()
                cancelled = event.wait(float(fake.get("hang_s", 60.0)))
                time.sleep(float(fake.get("cancel_delay_s", 0)))
                append_json("stream_end.jsonl", {"at": time.time(), "request_id": request_id, "cancelled": cancelled})
                final({"ok": False, "cancelled": True} if cancelled else {"ok": True, "answer": "late"})
            elif script == "crash":
                send_tokens()
                os._exit(7)
            elif script == "garbage":
                send(b"this is not json\n")
                final({"ok": True, "answer": "after garbage"})
            elif script == "eof":
                send_tokens()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            append_json("client_gone.jsonl", {"at": time.time(), "request_id": request_id})
            self.close_connection = True
        finally:
            if request_id:
                with CANCELS_LOCK:
                    CANCELS.pop(request_id, None)


if hasattr(socketserver, "UnixStreamServer"):  # not available on Windows builds

    class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True


def identity() -> dict:
    info: dict = {"pid": os.getpid()}
    if hasattr(os, "getuid"):
        old = os.umask(0)
        os.umask(old)
        info.update({"uid": os.getuid(), "gid": os.getgid(), "groups": sorted(os.getgroups()), "umask": old})
        try:
            status = Path("/proc/self/status").read_text()
            match = re.search(r"^NoNewPrivs:\s*(\d)", status, re.M)
            info["no_new_privs"] = int(match.group(1)) if match else None
        except OSError:
            info["no_new_privs"] = None
    return info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-ready", action="store_true")
    parser.add_argument("--exit-before-ready", type=int)
    parser.add_argument("--ready-delay", type=float, default=0.0)
    parser.add_argument("--ready-listen")
    parser.add_argument("--log-canary", action="store_true")
    parser.add_argument("--ignore-term", action="store_true")
    args = parser.parse_args()

    secrets_line = sys.stdin.readline()
    secrets = json.loads(secrets_line)
    STATE.update(worker_token=secrets["worker_token"], tenant_id=secrets["tenant_id"])
    directory = app_dir()
    directory.mkdir(parents=True, exist_ok=True)
    services_token = str(secrets.get("services_token", ""))
    (directory / "stdin.json").write_text(json.dumps({
        "keys": sorted(secrets), "tenant_id": secrets["tenant_id"],
        "worker_token_len": len(secrets["worker_token"]),
        "services_token_subject": services_token.split(".")[1] if services_token.count(".") >= 4 else None,
        "services_token_sha256": hashlib.sha256(services_token.encode()).hexdigest(),
    }))
    (directory / "env.json").write_text(json.dumps(dict(os.environ), sort_keys=True))
    (directory / "identity.json").write_text(json.dumps(identity()))
    (directory / "marker.json").write_text(json.dumps({
        "tenant_id": secrets["tenant_id"], "pid": os.getpid(), "started_at": time.time(), "app_dir": str(directory),
    }))

    if args.exit_before_ready is not None:
        return args.exit_before_ready
    if args.no_ready:
        while True:
            time.sleep(1)

    listen = os.environ.get("STARLING_LISTEN", "tcp:127.0.0.1:0")
    if listen.startswith("unix:"):
        path = listen[len("unix:"):]
        if os.path.exists(path):
            os.unlink(path)
        server = UnixHTTPServer(path, Handler)
        actual = listen
    else:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        actual = f"tcp:127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    stop = threading.Event()

    def on_signal(signum, frame) -> None:
        if not args.ignore_term:
            stop.set()

    for name in ("SIGTERM", "SIGBREAK", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    time.sleep(args.ready_delay)
    print("READY " + json.dumps({"listen": args.ready_listen or actual, "pid": os.getpid(),
                                 "engine_version": ENGINE_VERSION}), flush=True)
    if args.log_canary:
        print(json.dumps({"level": "info", "event": "draft_finished", "draft": LOG_CANARY}), flush=True)

    while not stop.wait(0.1):
        pass
    server.shutdown()
    server.server_close()
    (directory / "stopped.json").write_text(json.dumps({"at": time.time(), "pid": os.getpid()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
