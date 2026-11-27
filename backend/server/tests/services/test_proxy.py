"""AI proxy: validation, the exact upstream request, retries, key fallback, streaming and log hygiene."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
from datetime import date

import pytest

from starling_server.services.upstream import BACKOFF_CAP_S

from . import fake_openai as ai
from .conftest import PRIMARY_KEY, PROMPT, SECONDARY_KEY, chat_body

SYSTEM_TEXT = "You are a helpful writing assistant."
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def sha32(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def image_body(url: str, role: str = "user") -> dict:
    return chat_body(messages=[{"role": role, "content": [{"type": "text", "text": "Describe it."}, {"type": "image_url", "image_url": {"url": url, "detail": "low"}}]}])


def calls(services):
    return services.rows("SELECT * FROM ai_calls ORDER BY id")


# ----------------------------------------------------------------------------- request shape


def test_upstream_request_shape_without_streaming(services):
    account = services.add_account()
    body = chat_body(temperature=0.7, top_p=0.9, max_tokens=5000)
    response = services.press(account, body)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Fake completion text."
    [sent] = services.fake.requests
    settings = services.settings
    assert sent.method == "POST" and sent.path == "/v1/chat/completions"
    assert sent.bearer == PRIMARY_KEY
    assert sent.body == {
        "model": settings.ai_model_quality,
        "messages": body["messages"],
        "max_completion_tokens": settings.ai_max_output_tokens,
        "n": 1,
        "reasoning_effort": settings.ai_reasoning_effort,
        "safety_identifier": sha32(account.id),
        "prompt_cache_key": sha32(f"{account.id}:starling-quality"),
    }


def test_upstream_request_shape_when_streaming_includes_usage(services):
    account = services.add_account()
    response = services.press(account, chat_body(stream=True, max_completion_tokens=50, lane="starling-fast"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in response.text.split("\n") if line]
    assert lines[-1] == "data: [DONE]"
    assert any('"usage"' in line for line in lines)
    [sent] = services.fake.requests
    assert sent.body["stream"] is True and sent.body["stream_options"] == {"include_usage": True}
    assert sent.body["max_completion_tokens"] == 50
    assert sent.body["prompt_cache_key"] == sha32(f"{account.id}:starling-fast")
    for absent in ("temperature", "top_p", "tools", "logprobs", "tool_choice"):
        assert absent not in sent.body
    [row] = calls(services)
    assert (row["status"], row["estimated"], row["input_tokens"], row["output_tokens"]) == ("ok", 0, 120, 30)


@pytest.mark.parametrize("effort,allowance", [("low", 2048), ("minimal", 512), ("medium", 4096), ("high", 8192)])
def test_a_reasoning_effort_adds_its_allowance_on_top_of_the_visible_cap(make_services, effort, allowance):
    """Reasoning tokens share max_completion_tokens: the engine's cap covers only the visible answer."""
    services = make_services(overrides={"AI_REASONING_EFFORT": effort})
    account = services.add_account()
    assert services.press(account, chat_body(max_tokens=220)).status_code == 200
    sent = services.fake.requests[-1].body
    assert sent["reasoning_effort"] == effort
    assert sent["max_completion_tokens"] == 220 + allowance


def test_no_reasoning_means_no_allowance(services):
    account = services.add_account()
    assert services.settings.ai_reasoning_effort in ("", "none")
    assert services.press(account, chat_body(max_tokens=220)).status_code == 200
    assert services.fake.requests[-1].body["max_completion_tokens"] == 220


def test_the_allowance_goes_when_the_model_rejects_reasoning_effort(make_services):
    services = make_services(overrides={"AI_REASONING_EFFORT": "low"})
    account = services.add_account()
    services.fake.queue(ai.unsupported("reasoning_effort"), ai.ok_json())
    assert services.press(account, chat_body(max_tokens=220)).status_code == 200
    assert services.press(account, chat_body(max_tokens=220)).status_code == 200
    last = services.fake.requests[-1].body
    assert "reasoning_effort" not in last and last["max_completion_tokens"] == 220


def test_sampling_parameters_are_forwarded_only_when_the_model_allows_them(make_services, tmp_path):
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({"models": [{"model": "gpt-4.1-mini", "effective_from": "2026-01-01",
                                               "input_per_mtok": 0.4, "cached_input_per_mtok": 0.1, "output_per_mtok": 1.6}]}))
    lanes = {name: "gpt-4.1-mini" for name in ("AI_MODEL_QUALITY", "AI_MODEL_FAST", "AI_MODEL_VISION")}
    services = make_services(overrides={"AI_PRICING_FILE": str(pricing), **lanes})
    account = services.add_account()
    assert services.press(account, chat_body(temperature=0.4, top_p=0.5)).status_code == 200
    sent = services.fake.requests[-1].body
    assert sent["model"] == "gpt-4.1-mini" and sent["temperature"] == 0.4 and sent["top_p"] == 0.5


def test_unsupported_parameter_is_dropped_once_and_remembered(services):
    account = services.add_account()
    services.fake.queue(ai.unsupported("reasoning_effort"), ai.ok_json())
    assert services.press(account).status_code == 200
    first, second = services.fake.requests
    assert "reasoning_effort" in first.body and "reasoning_effort" not in second.body
    assert [(row["status"], row["error_code"]) for row in calls(services)] == [("error", "unsupported_parameter"), ("ok", None)]
    assert services.press(account).status_code == 200
    assert "reasoning_effort" not in services.fake.requests[-1].body  # remembered for the process


def test_unsupported_parameter_retry_happens_only_once_and_never_for_required_fields(services):
    account = services.add_account()
    services.fake.queue(ai.unsupported("reasoning_effort"), ai.unsupported("safety_identifier"))
    response = services.press(account)
    assert response.status_code == 400 and response.json()["error"]["code"] == "bad_request"
    assert len(services.fake.requests) == 2
    services.fake.queue(ai.unsupported("max_completion_tokens"))
    assert services.press(account).status_code == 400
    assert len(services.fake.requests) == 3


# ----------------------------------------------------------------------------- retries


def test_rate_limit_retry_waits_for_retry_after(services):
    account = services.add_account()
    services.fake.queue(ai.rate_limited(retry_after="3"), ai.ok_json())
    assert services.press(account).status_code == 200
    assert len(services.fake.requests) == 2
    assert services.clock.sleeps == [3.0]


def test_retry_after_beyond_the_deadline_is_not_waited_for(services):
    account = services.add_account()
    services.fake.queue(ai.rate_limited(retry_after="120"))
    response = services.press(account)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ai_rate_limited"
    assert response.headers["retry-after"] == "120"
    assert len(services.fake.requests) == 1 and services.clock.sleeps == []


def test_rate_limits_give_up_after_two_retries(services):
    account = services.add_account()
    services.fake.queue(ai.rate_limited(), ai.rate_limited(), ai.rate_limited(), ai.ok_json())
    response = services.press(account)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_rate_limited"
    assert response.headers["retry-after"]
    assert len(services.fake.requests) == 3 and len(services.clock.sleeps) == 2


def test_server_errors_are_retried_with_growing_backoff(services):
    account = services.add_account()
    services.fake.queue(ai.server_error(500), ai.server_error(503), ai.server_error(502))
    response = services.press(account)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_unavailable"
    assert len(services.fake.requests) == 3
    first, second = services.clock.sleeps
    assert 0.25 <= first <= 0.5 and 0.5 <= second <= 1.0 and second <= BACKOFF_CAP_S
    services.fake.queue(ai.server_error(504), ai.ok_json())
    assert services.press(account).status_code == 200


def test_timeouts_are_retried_and_the_timed_out_attempt_is_estimated(services):
    account = services.add_account()
    services.fake.queue(ai.timeout(), ai.ok_json())
    assert services.press(account).status_code == 200
    timed_out, ok = calls(services)
    assert (timed_out["status"], timed_out["error_code"], timed_out["estimated"]) == ("error", "timeout", 1)
    assert timed_out["input_tokens"] == math.ceil((len(SYSTEM_TEXT) + len(PROMPT)) / 4)
    assert ok["status"] == "ok"


@pytest.mark.parametrize(
    "code",
    ["insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
     "project_spend_limit_exceeded", "organization_usage_limit_exceeded"],
)
def test_quota_and_spend_limit_errors_are_never_retried(services, code):
    account = services.add_account()
    services.fake.queue(ai.quota(code), ai.ok_json())
    response = services.press(account)
    assert response.status_code == 502 and response.json()["error"]["code"] == "ai_quota"
    assert len(services.fake.requests) == 1 and services.clock.sleeps == []


def test_model_not_found_maps_to_ai_model(services):
    account = services.add_account()
    services.fake.queue(ai.error(404, "model_not_found", type_="invalid_request_error"))
    response = services.press(account)
    assert response.status_code == 502 and response.json()["error"]["code"] == "ai_model"
    assert len(services.fake.requests) == 1


# ----------------------------------------------------------------------------- keys


def test_rejected_primary_key_falls_back_to_the_secondary_and_raises_an_alert(make_services):
    services = make_services(overrides={"AI_API_KEY_SECONDARY": SECONDARY_KEY})
    account = services.add_account()
    services.fake.queue(ai.error(401, "invalid_api_key", type_="invalid_request_error"), ai.ok_json())
    assert services.press(account).status_code == 200
    assert [request.bearer for request in services.fake.requests] == [PRIMARY_KEY, SECONDARY_KEY]
    alert = services.one("SELECT value_json FROM service_state WHERE key = 'alert:ai_key_rejected'")
    assert alert is not None
    value = json.loads(alert["value_json"])
    assert value["key"] == "primary" and value["retried_with"] == "secondary" and value["upstream_status"] == 401
    assert PRIMARY_KEY not in alert["value_json"] and SECONDARY_KEY not in alert["value_json"]
    assert services.press(account).status_code == 200
    assert services.fake.requests[-1].bearer == SECONDARY_KEY  # the secondary stays first for a while
    services.clock.advance(601)
    assert services.press(account).status_code == 200
    assert services.fake.requests[-1].bearer == PRIMARY_KEY


def test_both_keys_rejected_is_ai_auth(make_services):
    services = make_services(overrides={"AI_API_KEY_SECONDARY": SECONDARY_KEY})
    account = services.add_account()
    services.fake.queue(ai.error(401, "invalid_api_key"), ai.error(403, "invalid_api_key"))
    response = services.press(account)
    assert response.status_code == 502 and response.json()["error"]["code"] == "ai_auth"
    assert len(services.fake.requests) == 2


def test_rejected_key_without_secondary_is_not_retried_but_alerts(services):
    account = services.add_account()
    services.fake.queue(ai.error(401, "invalid_api_key"), ai.ok_json())
    response = services.press(account)
    assert response.status_code == 502 and response.json()["error"]["code"] == "ai_auth"
    assert len(services.fake.requests) == 1
    alert = json.loads(services.one("SELECT value_json FROM service_state WHERE key = 'alert:ai_key_rejected'")["value_json"])
    assert alert["retried_with"] is None


# ----------------------------------------------------------------------------- estimation


def test_aborted_stream_without_usage_is_estimated_and_flagged(services):
    account = services.add_account()
    services.fake.queue(ai.ok_sse(pieces=("abcd" * 10,), include_usage=False, done=True, fail_after=1))
    response = services.press(account, chat_body(stream=True))
    assert response.status_code == 200
    assert "the AI stream was interrupted" in response.text
    [row] = calls(services)
    expected_input = math.ceil((len(SYSTEM_TEXT) + len(PROMPT)) / 4)
    assert (row["estimated"], row["input_tokens"], row["output_tokens"]) == (1, expected_input, 10)
    assert (row["status"], row["error_code"]) == ("error", "stream_interrupted")
    price = services.settings.ai_pricing.price_for(row["model"], date(2026, 9, 15))
    assert row["cost_micros"] == price.cost_micros(expected_input, 0, 10) > 0


def test_json_answer_without_usage_is_estimated(services):
    account = services.add_account()
    services.fake.queue(ai.ok_json(content="x" * 21, include_usage=False))
    assert services.press(account).status_code == 200
    [row] = calls(services)
    assert (row["status"], row["estimated"], row["output_tokens"]) == ("ok", 1, 6)


# ----------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "body",
    [
        chat_body(tools=[{"type": "function"}]),
        chat_body(logprobs=True),
        chat_body(lane="gpt-5.6-luna"),
        chat_body(messages=[{"role": "tool", "content": "x"}]),
        chat_body(messages=[{"role": "user", "content": "x", "name": "someone"}]),
        chat_body(messages=[]),
        chat_body(messages=[{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}]),
        {**chat_body(), "stream": "yes"},
        chat_body(max_tokens=0),
        chat_body(max_completion_tokens=True),
        chat_body(temperature=3),
        chat_body(response_format={"type": "xml"}),
        image_body("https://example.com/cat.png"),
        image_body("http://169.254.169.254/latest/meta-data"),
        image_body("ftp://example.com/cat.png"),
        image_body(data_uri("image/png", PNG), role="system"),
        image_body(data_uri("image/svg+xml", b"<svg/>")),
        image_body(data_uri("image/png", JPEG)),
    ],
)
def test_invalid_bodies_are_refused_before_any_upstream_call(services, body):
    account = services.add_account()
    response = services.press(account, body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert services.fake.requests == []


def test_data_uri_images_with_matching_bytes_are_accepted(services):
    account = services.add_account()
    url = data_uri("image/jpeg", JPEG)
    assert services.press(account, image_body(url)).status_code == 200
    assert services.fake.requests[0].body["messages"][0]["content"][1] == {"type": "image_url", "image_url": {"url": url, "detail": "low"}}


def test_images_over_five_megabytes_are_refused(services):
    account = services.add_account()
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 1 - 8)
    response = services.press(account, image_body(data_uri("image/png", big)))
    assert response.status_code == 413 and response.json()["error"]["code"] == "input_too_large"
    exactly = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 - 8)
    assert services.press(account, image_body(data_uri("image/png", exactly))).status_code == 200


def test_estimated_input_above_the_per_call_limit_is_refused(make_services):
    services = make_services(overrides={"AI_MAX_INPUT_TOKENS_PER_CALL": "256"})
    account = services.add_account()
    response = services.press(account, chat_body(content="y" * 2000))
    assert response.status_code == 413 and response.json()["error"]["code"] == "input_too_large"
    assert services.fake.requests == []
    assert services.press(account, chat_body(content="y" * 800)).status_code == 200
    image = image_body(data_uri("image/png", PNG))
    assert services.press(account, image).status_code == 413  # the fixed per-image estimate counts


def test_metering_headers_and_tokens_are_required(services):
    account = services.add_account()
    reservation = services.add_reservation(account)
    assert services.chat(account).status_code == 400
    assert services.chat(account, request_id=reservation, background=True).status_code == 400
    assert services.chat(account, headers={"X-Starling-Bucket": "system"}).status_code == 400
    assert services.chat(None, request_id=reservation).status_code == 401
    assert services.chat(token="svc1.admin.1.2.3", request_id=reservation).status_code == 401
    assert services.chat(token=services.admin_token(), request_id=reservation).status_code == 401
    assert services.fake.requests == []
    assert services.chat(account, request_id=reservation).status_code == 200  # negative control


def test_models_without_a_price_are_refused(make_services):
    services = make_services(overrides={"AI_MODEL_QUALITY": "unpriced-model"})
    account = services.add_account()
    response = services.press(account)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_unavailable"
    assert services.fake.requests == []
    assert services.press(account, chat_body(lane="starling-fast")).status_code == 200


# ----------------------------------------------------------------------------- hygiene


def test_logs_and_database_never_hold_prompts_outputs_keys_or_tokens(make_services, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    services = make_services(overrides={"AI_API_KEY_SECONDARY": SECONDARY_KEY})
    account = services.add_account()
    token = services.token(account.id)
    prompt = "PROMPT-CANARY-7f3a quarterly roadmap"
    output = "OUTPUT-CANARY-91bc"
    services.fake.queue(
        ai.error(401, "invalid_api_key"),
        ai.ok_json(content=output),
        ai.rate_limited(retry_after="1"),
        ai.ok_sse(pieces=(output,)),
        ai.error(400, "invalid_prompt", message=f"rejected: {prompt}"),
    )
    assert services.chat(body=chat_body(content=prompt), token=token, request_id=services.add_reservation(account)).status_code == 200
    streamed = services.chat(body=chat_body(content=prompt, stream=True), token=token, request_id=services.add_reservation(account))
    assert output in streamed.text
    assert services.chat(body=chat_body(content=prompt), token=token, request_id=services.add_reservation(account)).status_code == 400
    services.chat(body=chat_body(content=prompt, tools=[]), token=token, request_id=services.add_reservation(account))
    captured = capsys.readouterr()
    for secret in (prompt, "PROMPT-CANARY", output, PRIMARY_KEY, SECONDARY_KEY, token):
        assert secret not in caplog.text
        assert secret not in captured.out and secret not in captured.err
    with services.db.read() as conn:
        for table in ("ai_calls", "service_state", "budget_counters", "usage_counters", "usage_reservations"):
            dump = json.dumps([dict(row) for row in conn.execute(f"SELECT * FROM {table}")])
            for secret in (prompt, output, PRIMARY_KEY, SECONDARY_KEY, token):
                assert secret not in dump
