"""Admin diagnostics, the gateway client, the CLI commands and the services entrypoint."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import socket
import threading
import time

import httpx
import pytest

from starling_server.cli import build_parser
from starling_server.config import ConfigError
from starling_server.services import client, commands, main
from starling_server.services.app import create_app

from . import fake_openai as ai
from .conftest import PRIMARY_KEY, SECONDARY_KEY


def post_check(services):
    return services.client.post("/admin/ai-check", headers=services.auth())


# ----------------------------------------------------------------------------- admin endpoints


def test_ai_check_reports_each_distinct_model_with_masked_keys(make_services):
    services = make_services(overrides={"AI_MODEL_FAST": "gpt-5.6-terra"})
    response = post_check(services)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and len(body["results"]) == 2
    by_model = {result["model"]: result for result in body["results"]}
    assert by_model["gpt-5.6-luna"]["lanes"] == ["starling-quality", "starling-vision"]
    assert by_model["gpt-5.6-terra"]["lanes"] == ["starling-fast"]
    assert all(isinstance(result["latency_ms"], int) and result["error_code"] is None for result in body["results"])
    assert body["keys"]["primary"] == {"configured": True, "last4": PRIMARY_KEY[-4:]}
    assert body["keys"]["secondary"] == {"configured": False, "last4": None}
    assert PRIMARY_KEY not in response.text
    assert sorted(request.body["model"] for request in services.fake.requests) == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert [row["bucket"] for row in services.rows("SELECT bucket FROM ai_calls")] == ["admin", "admin"]


@pytest.mark.parametrize(
    ("step", "code", "hint"),
    [
        (ai.error(401, "invalid_api_key"), "ai_auth", "AI_API_KEY"),
        (ai.error(404, "model_not_found"), "ai_model", "AI_MODEL_QUALITY"),
        (ai.quota(), "ai_quota", "spend limit"),
        (ai.rate_limited(), "ai_rate_limited", "rate limiting"),
        (ai.server_error(503), "ai_unavailable", "AI_BASE_URL"),
        (ai.error(400, "invalid_request"), "bad_request", "AI_REASONING_EFFORT"),
    ],
)
def test_ai_check_gives_actionable_messages(services, step, code, hint):
    services.fake.queue(step)
    body = post_check(services).json()
    [result] = body["results"]
    assert body["ok"] is False and result["ok"] is False
    assert result["error_code"] == code and hint in result["message"]
    assert len(services.fake.requests) == 1  # no retries during a check


def test_ai_check_learns_unsupported_parameters_and_says_so(services):
    services.fake.queue(ai.unsupported("reasoning_effort"), ai.ok_json())
    [result] = post_check(services).json()["results"]
    assert result["ok"] is True and "reasoning_effort" in result["message"]


def test_ai_check_without_a_key_sends_nothing(make_services):
    services = make_services(overrides={"AI_API_KEY": None})
    [result] = post_check(services).json()["results"]
    assert result["error_code"] == "not_configured" and "AI_API_KEY" in result["message"]
    assert services.fake.requests == []


def test_ai_check_still_runs_while_paused_or_over_budget(services):
    services.set_service_state("ai_paused", "true")
    services.set_budget("global_day", services.budget_limit("global_day"))
    assert post_check(services).json()["ok"] is True


def test_admin_endpoints_accept_only_fresh_admin_tokens(services):
    account = services.add_account()
    for method, path in (("post", "/admin/ai-check"), ("get", "/admin/status")):
        call = getattr(services.client, method)
        assert call(path, headers=services.auth(account)).status_code == 401
        assert call(path).status_code == 401
    token = services.admin_token()
    services.clock.advance(301)
    assert services.client.get("/admin/status", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert services.client.get("/admin/status", headers=services.auth()).status_code == 200  # negative control
    assert services.fake.requests == []


def test_status_reports_budgets_keys_health_capabilities_and_news(make_services):
    services = make_services(overrides={"AI_API_KEY_SECONDARY": SECONDARY_KEY})
    account = services.add_account()
    services.fake.queue(ai.error(401, "invalid_api_key"), ai.ok_json(prompt=1000, cached=200, completion=500))
    assert services.press(account).status_code == 200
    response = services.client.get("/admin/status", headers=services.auth())
    assert response.status_code == 200
    status = response.json()
    assert status["budgets"]["global_day"]["cost_micros"] == 764
    assert status["budgets"]["global_day"]["limit_micros"] == services.settings.ai_daily_budget_micros
    assert status["budgets"]["global_month"]["limit_micros"] == services.settings.ai_monthly_budget_micros
    assert status["keys"]["primary"]["last4"] == PRIMARY_KEY[-4:] and status["keys"]["secondary"]["last4"] == SECONDARY_KEY[-4:]
    assert status["keys"]["active"] == "secondary"
    assert status["upstream"]["ok_count"] == 1 and status["upstream"]["last_error_code"] == "invalid_api_key"
    assert status["upstream"]["base_url"] == services.settings.ai_base_url
    assert status["paused"] is False and status["alerts"][0]["key"] == "alert:ai_key_rejected"
    assert "reasoning_effort" in status["capabilities"]["gpt-5.6-luna"]["params"]
    assert status["world"]["news"]["items"] == 0 and status["prices_configured"] == {"gpt-5.6-luna": True}
    assert status["ai_calls_today"]["ok"]["count"] == 1
    assert PRIMARY_KEY not in response.text and SECONDARY_KEY not in response.text


# ----------------------------------------------------------------------------- gateway client


def real_clock_services(make_services, **kwargs):
    return make_services(clock=time.time, monotonic=time.monotonic, sleep=asyncio.sleep, **kwargs)


def test_client_calls_the_admin_endpoints_with_a_fresh_admin_token(make_services):
    services = real_clock_services(make_services)
    transport = httpx.ASGITransport(app=services.app)
    status = asyncio.run(client.services_status(services.settings, transport=transport))
    assert status["ok"] is True and "budgets" in status
    check = asyncio.run(client.ai_check(services.settings, transport=httpx.ASGITransport(app=services.app)))
    assert check["ok"] is True and check["results"][0]["model"] == services.settings.ai_model_quality


def test_client_reports_failures_without_raising(services):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    down = asyncio.run(client.services_status(services.settings, transport=httpx.MockTransport(refuse)))
    assert down == {"ok": False, "error": {"code": "services_unreachable", "message": down["error"]["message"]}}
    unauthorized = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": {"code": "unauthorized", "message": "unknown services token"}}))
    assert asyncio.run(client.ai_check(services.settings, transport=unauthorized))["error"]["code"] == "unauthorized"
    garbage = httpx.MockTransport(lambda request: httpx.Response(500, text="oops"))
    assert asyncio.run(client.services_status(services.settings, transport=garbage))["error"]["code"] == "services_error"
    no_key = dataclasses.replace(services.settings, services_token_key=b"")
    assert asyncio.run(client.services_status(no_key))["error"]["code"] == "services_not_configured"


def test_client_sends_a_verifiable_admin_token_and_picks_the_transport(services):
    seen = {}

    def capture(request):
        seen["authorization"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    asyncio.run(client.services_status(services.settings, transport=httpx.MockTransport(capture)))
    from starling_server.services import tokens

    subject = tokens.verify(services.settings.services_token_key, seen["authorization"].split(" ", 1)[1], time.time())
    assert subject.is_admin and seen["url"] == "http://127.0.0.1:8766/admin/status"
    unix = dataclasses.replace(services.settings, services_url="unix:/run/starling/services.sock")
    base, transport = client._endpoint(unix, None)
    assert base == "http://services" and isinstance(transport, httpx.AsyncHTTPTransport)
    tcp = dataclasses.replace(services.settings, services_url="tcp:127.0.0.1:9999")
    assert client._endpoint(tcp, None)[0] == "http://127.0.0.1:9999"


# ----------------------------------------------------------------------------- CLI


def test_cli_registers_the_services_commands():
    parser = build_parser()
    assert parser.parse_args(["ai-check", "--json"]).json is True
    assert parser.parse_args(["services-status"]).func is commands._services_status


def test_ai_check_command_prints_results_and_exit_codes(services):
    lines: list[str] = []
    fake = ai.FakeOpenAI()
    assert commands.run_ai_check_command(services.settings, transport=fake.transport, out=lines.append) == 0
    text = "\n".join(lines)
    assert "OK" in text and PRIMARY_KEY[-4:] in text and PRIMARY_KEY not in text and "AI check passed." in text
    fake.queue(ai.error(401, "invalid_api_key"))
    lines.clear()
    assert commands.run_ai_check_command(services.settings, transport=fake.transport, out=lines.append) == 1
    assert "FAIL" in lines[0] or "FAIL" in "\n".join(lines)
    assert "AI_API_KEY" in "\n".join(lines)
    lines.clear()
    assert commands.run_ai_check_command(services.settings, as_json=True, transport=fake.transport, out=lines.append) == 0
    assert json.loads(lines[0])["results"][0]["ok"] is True


def test_services_status_command_prints_json(make_services):
    services = real_clock_services(make_services)
    lines: list[str] = []
    assert commands.run_status_command(services.settings, transport=httpx.ASGITransport(app=services.app), out=lines.append) == 0
    assert json.loads(lines[0])["ok"] is True


# ----------------------------------------------------------------------------- entrypoint


def test_listen_addresses_are_parsed_and_never_public():
    assert main.parse_listen("unix:/run/starling/services.sock") == main.Listen("unix", path="/run/starling/services.sock")
    assert main.parse_listen("tcp:127.0.0.1:8766") == main.Listen("tcp", host="127.0.0.1", port=8766)
    assert main.parse_listen("http://localhost:0") == main.Listen("tcp", host="127.0.0.1", port=0)
    assert main.parse_listen("tcp:[::1]:9000").host == "::1"
    for bad in ("tcp:0.0.0.0:8766", "tcp:10.0.0.5:8766", "tcp:127.0.0.1", "unix:", "8766", "tcp:127.0.0.1:99999"):
        with pytest.raises(ConfigError):
            main.parse_listen(bad)


def test_main_refuses_a_public_listen_address(services, monkeypatch, capsys):
    public = dataclasses.replace(services.settings, services_listen="tcp:0.0.0.0:8766")
    monkeypatch.setattr(main, "get_settings", lambda: public)
    monkeypatch.setattr(main, "configure_logging", lambda level: None)
    assert main.main() == 2
    assert "loopback" in capsys.readouterr().err


def test_serve_binds_loopback_prints_ready_and_answers(make_services):
    services = make_services()
    settings = dataclasses.replace(services.settings, services_listen="tcp:127.0.0.1:0")
    app = create_app(settings, upstream_transport=ai.FakeOpenAI().transport, start_background=False)
    lines: list[str] = []
    servers = []
    ready = threading.Event()

    def on_ready(line: str) -> None:
        lines.append(line)
        ready.set()

    thread = threading.Thread(
        target=lambda: asyncio.run(main.serve(settings, app=app, ready=on_ready, on_server=servers.append)), daemon=True
    )
    thread.start()
    assert ready.wait(15)
    assert lines[0].startswith("READY ")
    info = json.loads(lines[0][len("READY "):])
    assert info["pid"] == os.getpid() and info["listen"].startswith("tcp:127.0.0.1:")
    port = int(info["listen"].rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"GET /livez HTTP/1.1\r\nHost: services\r\nConnection: close\r\n\r\n")
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    assert data.startswith(b"HTTP/1.1 200") and b'{"ok":true}' in data
    servers[0].should_exit = True
    thread.join(15)
    assert not thread.is_alive()


@pytest.mark.skipif(os.name == "nt" or not hasattr(socket, "AF_UNIX"), reason="unix sockets need a POSIX system")
def test_unix_socket_is_created_with_mode_0660(tmp_path):
    sock, address = main.bind_socket(main.Listen("unix", path=str(tmp_path / "run" / "services.sock")))
    try:
        assert address.startswith("unix:") and oct(os.stat(address[5:]).st_mode & 0o777) == oct(0o660)
    finally:
        sock.close()


def test_json_log_lines_drop_tracebacks_and_messages_of_exceptions():
    formatter = main.JsonLogFormatter()
    try:
        raise ValueError("PROMPT-CANARY inside an exception")
    except ValueError:
        record = logging.LogRecord("starling", logging.ERROR, __file__, 1, "call failed (%s)", ("ValueError",), exc_info=__import__("sys").exc_info())
    line = formatter.format(record)
    assert json.loads(line)["error"] == "ValueError" and "PROMPT-CANARY" not in line
