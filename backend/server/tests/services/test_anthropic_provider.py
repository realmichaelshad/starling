"""AI_PROVIDER=anthropic: the Messages API behind the proxy's OpenAI-shaped contract.

Every test runs against fake_anthropic (an httpx2 transport, or a loopback server); nothing reaches
a real provider. The engine-facing side is the same OpenAI Chat Completions subset as ever.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
from datetime import date, datetime, timezone

import anthropic
import pytest

from starling_server.config import load_settings, validate
from starling_server.services import anthropic_upstream as claude
from starling_server.services.anthropic_upstream import CONTINUE_TURN, JSON_OBJECT_INSTRUCTION, OPENING_TURN
from starling_server.services.proxy import reasoning_allowance

from ..conftest import base_env
from . import fake_anthropic as fa
from .conftest import PRIMARY_KEY, PROMPT, SECONDARY_KEY, chat_body

# The claude-haiku-5-5 price row starts on 2026-10-30; these tests run after it.
NOW = int(datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc).timestamp())
TODAY = date(2026, 11, 2)
BASE_URL = "https://claude-provider.invalid"
MODEL = "claude-haiku-5-5"
SYSTEM_TEXT = "You are a helpful writing assistant."
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["act", "wait"], "description": "act or wait."},
        "text": {"type": "string", "maxLength": 280, "description": "The draft."},
        "confidence": {"type": "number", "description": "From 0 to 1."},
    },
    "required": ["verdict", "text", "confidence"],
    "additionalProperties": False,
}


def sha32(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def calls(services):
    return services.rows("SELECT * FROM ai_calls ORDER BY id")


def sse_events(text: str) -> list:
    """The `data:` payloads of an SSE body, JSON-decoded except `[DONE]`."""
    events = []
    for line in text.split("\n"):
        if line.startswith("data:"):
            data = line[5:].strip()
            events.append(data if data == "[DONE]" else json.loads(data))
    return events


@pytest.fixture
def make_claude(make_services):
    def factory(overrides=None, fake=None, now=NOW, **kwargs):
        environment = {"AI_PROVIDER": "anthropic", "AI_BASE_URL": BASE_URL, **(overrides or {})}
        return make_services(overrides=environment, fake=fake or fa.FakeAnthropic(), now=now, **kwargs)

    return factory


@pytest.fixture
def services(make_claude):
    return make_claude()


# ----------------------------------------------------------------------------- settings


def test_provider_defaults_switch_with_ai_provider(tmp_path):
    openai = load_settings(base_env(tmp_path / "a"))
    assert (openai.ai_provider, openai.ai_model_quality, openai.ai_base_url) == (
        "openai", "gpt-5.6-luna", "https://api.openai.com/v1")
    assert openai.web_search_enabled is True and openai.legal.ai_subprocessors == "OpenAI"
    env = base_env(tmp_path / "b")
    env["AI_PROVIDER"] = "anthropic"
    settings = load_settings(env)
    assert settings.ai_base_url == "https://api.anthropic.com"
    assert (settings.ai_model_quality, settings.ai_model_fast, settings.ai_model_vision) == (MODEL, MODEL, MODEL)
    assert settings.web_search_enabled is False and settings.legal.ai_subprocessors == "Anthropic"
    assert not [p for p in validate(settings, "services") if "anthropic" in p.lower() or "price" in p]
    env["AI_PROVIDER"] = "gemini"
    with pytest.raises(Exception, match="AI_PROVIDER"):
        load_settings(env)


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"WEB_SEARCH": "true"}, "WEB_SEARCH must be false"),
        ({"AI_BASE_URL": "https://api.openai.com/v1"}, "AI_BASE_URL points at OpenAI"),
        ({"AI_MODEL_FAST": "gpt-5.6-luna"}, "AI_MODEL_FAST is 'gpt-5.6-luna', which is not a Claude model"),
        ({"AI_REASONING_EFFORT": "turbo"}, "AI_REASONING_EFFORT must be one of"),
        ({"AI_MAX_INPUT_TOKENS_PER_CALL": "100000"}, "AI_MAX_INPUT_TOKENS_PER_CALL must be at most 60000"),
        ({"AI_MODEL_QUALITY": "claude-unpriced-9"}, "no price configured for AI model 'claude-unpriced-9'"),
    ],
)
def test_validate_refuses_what_the_anthropic_path_cannot_do(tmp_path, override, fragment):
    env = {**base_env(tmp_path / "data"), "AI_PROVIDER": "anthropic", **override}
    problems = validate(load_settings(env), "services")
    assert any(fragment in problem for problem in problems), problems


def test_production_settings_for_anthropic_validate_cleanly(tmp_path):
    env = {
        **base_env(tmp_path / "data"),
        "STARLING_ENV": "production",
        "STARLING_ISOLATION": "os",
        "STARLING_PUBLIC_URL": "https://app.example.com",
        "STARLING_SERVICES_TOKEN_KEY": base64.urlsafe_b64encode(b"s" * 32).decode(),
        "AI_PROVIDER": "anthropic",
        "AI_API_KEY": "sk-ant-dummy",
        "AI_REASONING_EFFORT": "low",
    }
    assert validate(load_settings(env), "services") == []
    env["AI_BASE_URL"] = "http://127.0.0.1:9/"
    assert "AI_BASE_URL must use https in production" in validate(load_settings(env), "services")


def test_the_claude_haiku_5_5_price_row(tmp_path):
    pricing = load_settings(base_env(tmp_path / "data")).ai_pricing
    price = pricing.price_for(MODEL, date(2026, 10, 30))
    assert (price.input_micros_per_mtok, price.cached_input_micros_per_mtok, price.output_micros_per_mtok) == (
        100_000, 10_000, 500_000)
    assert price.source == "https://www.anthropic.com/pricing"
    assert pricing.price_for(MODEL, date(2026, 10, 29)) is None
    assert price.cost_micros(1_200, 200, 500) == 352  # 1,000 x $0.10/M + 200 x $0.01/M + 500 x $0.50/M


# ----------------------------------------------------------------------------- translation (pure)


def test_turns_are_shaped_for_the_messages_api():
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Rules one."},
            {"role": "assistant", "content": "Earlier answer."},
            {"role": "user", "content": "First."},
            {"role": "system", "content": [{"type": "text", "text": "Rules two."}]},
            {"role": "user", "content": [{"type": "text", "text": "Second."}, {"type": "text", "text": "   "}]},
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "A last word."},
        ],
        "max_completion_tokens": 300,
        "n": 1,
        "temperature": 0.4,
        "top_p": 0.9,
        "prompt_cache_key": "k",
        "stream_options": {"include_usage": True},
    }
    params = claude.translate_request(body).params
    assert params["system"] == "Rules one.\n\nRules two."
    assert params["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": OPENING_TURN}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Earlier answer."}]},
        {"role": "user", "content": [{"type": "text", "text": "First."}, {"type": "text", "text": "Second."}]},
        {"role": "assistant", "content": [{"type": "text", "text": "A last word."}]},
        {"role": "user", "content": [{"type": "text", "text": CONTINUE_TURN}]},
    ]
    for dropped in ("n", "temperature", "top_p", "top_k", "prompt_cache_key", "stream_options", "stream",
                    "thinking", "metadata", "output_config"):
        assert dropped not in params
    # No reasoning_effort sent: the model's own default effort (medium) and its allowance.
    assert params["max_tokens"] == 300 + reasoning_allowance("medium")


def test_instructions_alone_become_the_user_turn():
    params = claude.translate_request({"model": MODEL, "messages": [{"role": "system", "content": "Say OK."}],
                                       "max_completion_tokens": 16}).params
    assert "system" not in params
    assert params["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Say OK."}]}]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('Here it is: {"a": [1, 2]} as asked.', '{"a": [1, 2]}'),
        ("not json at all", "not json at all"),
    ],
)
def test_a_json_object_answer_is_trimmed_to_its_object(answer, expected):
    assert claude.json_only(answer) == expected


# ----------------------------------------------------------------------------- non-streaming


def test_a_draft_call_becomes_one_messages_call_and_an_openai_answer(services):
    account = services.add_account()
    response = services.press(account, chat_body(max_tokens=220, temperature=0.7, top_p=0.9))
    assert response.status_code == 200
    answer = response.json()
    assert answer["object"] == "chat.completion" and answer["model"] == MODEL
    assert answer["choices"] == [{"index": 0, "message": {"role": "assistant", "content": "Fake Claude text."},
                                  "finish_reason": "stop"}]
    assert answer["usage"] == {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
                               "prompt_tokens_details": {"cached_tokens": 0}}
    [sent] = services.fake.requests
    assert sent.method == "POST" and sent.url == f"{BASE_URL}/v1/messages"  # AI_BASE_URL is honoured
    assert sent.bearer == PRIMARY_KEY and sent.headers["anthropic-version"]
    assert sent.headers["x-stainless-retry-count"] == "0"
    assert sent.body == {
        "model": MODEL,
        "max_tokens": 220 + reasoning_allowance("low"),  # AI_REASONING_EFFORT none -> low, which may think
        "system": SYSTEM_TEXT,
        "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}],
        "output_config": {"effort": "low"},
        "metadata": {"user_id": sha32(account.id)},
    }
    [row] = calls(services)
    assert (row["model"], row["status"], row["input_tokens"], row["output_tokens"], row["estimated"]) == (
        MODEL, "ok", 120, 30, 0)
    price = services.settings.ai_pricing.price_for(MODEL, TODAY)
    assert row["cost_micros"] == price.cost_micros(120, 0, 30) > 0


def test_cache_tokens_are_metered_at_their_prices(services):
    account = services.add_account()
    services.fake.queue(fa.reply("ok", input_tokens=1_000, cache_read=200, output_tokens=500))
    response = services.press(account)
    assert response.json()["usage"] == {"prompt_tokens": 1_200, "completion_tokens": 500, "total_tokens": 1_700,
                                        "prompt_tokens_details": {"cached_tokens": 200}}
    [row] = calls(services)
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"], row["cost_micros"]) == (
        1_200, 200, 500, 352)  # 1,000 x $0.10/M + 200 x $0.01/M + 500 x $0.50/M
    assert services.one("SELECT cost_micros FROM usage_counters WHERE account_id = ?", (account.id,)) == {"cost_micros": 352}
    services.fake.queue(fa.reply("ok", input_tokens=100, cache_write=40, output_tokens=0))
    services.press(account)
    assert calls(services)[-1]["input_tokens"] == 150  # a cache write counts at 1.25 input tokens


def test_structured_output_is_requested_and_the_json_comes_back_as_the_content(services):
    account = services.add_account()
    draft = {"verdict": "act", "text": "Ship it.", "confidence": 0.8}
    services.fake.queue(fa.reply(json.dumps(draft)))
    response_format = {"type": "json_schema", "json_schema": {"name": "starling_draft", "strict": True,
                                                              "schema": DRAFT_SCHEMA}}
    response = services.press(account, chat_body(response_format=response_format))
    assert response.status_code == 200
    assert json.loads(response.json()["choices"][0]["message"]["content"]) == draft  # thinking block dropped
    [sent] = services.fake.requests
    fmt = sent.body["output_config"]["format"]
    assert fmt == {"type": "json_schema", "schema": anthropic.transform_schema(DRAFT_SCHEMA)}
    text_field = fmt["schema"]["properties"]["text"]
    assert "maxLength" not in text_field and "{maxLength: 280}" in text_field["description"]  # moved, not enforced
    assert fmt["schema"]["required"] == ["verdict", "text", "confidence"]
    assert fmt["schema"]["additionalProperties"] is False


def test_json_object_mode_asks_in_the_system_prompt_and_trims_the_answer(services):
    account = services.add_account()
    services.fake.queue(fa.reply('```json\n{"emerging": [], "summary": "quiet"}\n```'))
    response = services.press(account, chat_body(response_format={"type": "json_object"}, lane="starling-fast"))
    assert json.loads(response.json()["choices"][0]["message"]["content"]) == {"emerging": [], "summary": "quiet"}
    [sent] = services.fake.requests
    assert sent.body["system"] == f"{SYSTEM_TEXT}\n\n{JSON_OBJECT_INSTRUCTION}"
    assert "format" not in sent.body["output_config"]


def test_an_image_becomes_a_base64_image_block_on_the_vision_lane(services):
    account = services.add_account()
    url = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    body = chat_body(lane="starling-vision", messages=[{"role": "user", "content": [
        {"type": "text", "text": "Describe it."}, {"type": "image_url", "image_url": {"url": url, "detail": "low"}}]}])
    assert services.press(account, body).status_code == 200
    [sent] = services.fake.requests
    assert sent.body["model"] == services.settings.ai_model_vision == MODEL
    assert sent.body["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "Describe it."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(PNG).decode()}},
    ]}]
    [row] = calls(services)
    assert row["lane"] == "starling-vision" and row["status"] == "ok"


@pytest.mark.parametrize(
    ("setting", "effort"),
    [("none", "low"), ("minimal", "low"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "xhigh")],
)
def test_reasoning_effort_becomes_output_config_effort_with_its_thinking_allowance(make_claude, setting, effort):
    services = make_claude(overrides={"AI_REASONING_EFFORT": setting})
    account = services.add_account()
    assert services.press(account, chat_body(max_tokens=220)).status_code == 200
    sent = services.fake.requests[-1].body
    assert sent["output_config"] == {"effort": effort}
    assert sent["max_tokens"] == 220 + reasoning_allowance(effort)
    for absent in ("thinking", "temperature", "top_p", "top_k"):
        assert absent not in sent  # adaptive thinking, the model's default; no sampling parameters


def test_a_cut_answer_is_finish_reason_length(services):
    account = services.add_account()
    services.fake.queue(fa.reply("Half a dra", stop_reason="max_tokens"))
    choice = services.press(account).json()["choices"][0]
    assert (choice["message"]["content"], choice["finish_reason"]) == ("Half a dra", "length")


# ----------------------------------------------------------------------------- streaming


def test_a_stream_is_relayed_as_openai_chunks_with_usage(services):
    account = services.add_account()
    services.fake.queue(fa.reply(("Fake ", "stream ", "text."), input_tokens=300, cache_read=100, output_tokens=40))
    response = services.press(account, chat_body(stream=True, lane="starling-fast", max_completion_tokens=50))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "THINKING-NOT-RELAYED" not in response.text
    events = sse_events(response.text)
    assert events[-1] == "[DONE]"
    chunks = [event for event in events[:-1] if event["choices"]]
    assert all(event["object"] == "chat.completion.chunk" and event["model"] == MODEL for event in events[:-1])
    assert "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks) == "Fake stream text."
    assert chunks[-1]["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    assert events[-2]["choices"] == [] and events[-2]["usage"] == {
        "prompt_tokens": 400, "completion_tokens": 40, "total_tokens": 440, "prompt_tokens_details": {"cached_tokens": 100}}
    [sent] = services.fake.requests
    assert sent.body["stream"] is True and sent.body["max_tokens"] == 50 + reasoning_allowance("low")
    assert "stream_options" not in sent.body
    [row] = calls(services)
    assert (row["status"], row["estimated"], row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (
        "ok", 0, 400, 100, 40)
    price = services.settings.ai_pricing.price_for(MODEL, TODAY)
    assert row["cost_micros"] == price.cost_micros(400, 100, 40)


def test_an_overload_as_the_stream_opens_is_retried(services):
    account = services.add_account()
    services.fake.queue(fa.stream_error_event("overloaded_error"), fa.reply(("Second ", "try.")))
    response = services.press(account, chat_body(stream=True))
    assert "".join(e["choices"][0]["delta"].get("content", "") for e in sse_events(response.text)[:-1]
                   if e["choices"]) == "Second try."
    assert len(services.fake.requests) == 2 and len(services.clock.sleeps) == 1
    assert [(row["status"], row["error_code"]) for row in calls(services)] == [("error", "overloaded_error"), ("ok", None)]


def test_an_interrupted_stream_is_estimated_and_flagged(services):
    account = services.add_account()
    services.fake.queue(fa.reply(("abcd" * 10,), thinking=False, fail_after=4))
    response = services.press(account, chat_body(stream=True))
    assert response.status_code == 200
    assert "the AI stream was interrupted" in response.text
    [row] = calls(services)
    assert (row["status"], row["error_code"], row["estimated"]) == ("error", "stream_interrupted", 1)
    assert row["input_tokens"] == math.ceil((len(SYSTEM_TEXT) + len(PROMPT)) / 4) and row["output_tokens"] == 10


# ----------------------------------------------------------------------------- refusals


def test_a_refusal_is_the_engines_bad_request_and_is_metered(services):
    account = services.add_account()
    services.fake.queue(fa.refusal(input_tokens=150, output_tokens=7))
    response = services.press(account)
    assert response.status_code == 400
    assert response.json()["error"] == {"code": "bad_request", "message": "the AI model declined to answer this request"}
    assert len(services.fake.requests) == 1 and services.clock.sleeps == []  # never retried
    [row] = calls(services)
    assert (row["status"], row["error_code"], row["input_tokens"], row["output_tokens"], row["estimated"]) == (
        "error", "refusal", 150, 7, 0)
    assert row["cost_micros"] == services.settings.ai_pricing.price_for(MODEL, TODAY).cost_micros(150, 0, 7) > 0


def test_a_refusal_in_a_stream_ends_with_an_error_event(services):
    account = services.add_account()
    services.fake.queue(fa.refusal("Partial ", input_tokens=150, output_tokens=7))
    response = services.press(account, chat_body(stream=True))
    assert response.status_code == 200
    events = sse_events(response.text)
    assert "[DONE]" not in events
    assert events[-1] == {"error": {"code": "bad_request", "message": "the AI model declined to answer this request"}}
    assert events[-2]["usage"]["prompt_tokens"] == 150
    [row] = calls(services)
    assert (row["status"], row["error_code"], row["input_tokens"], row["output_tokens"], row["estimated"]) == (
        "error", "refusal", 150, 7, 0)


# ----------------------------------------------------------------------------- errors, keys, retries


def test_a_rejected_key_falls_back_to_the_secondary_and_raises_an_alert(make_claude):
    services = make_claude(overrides={"AI_API_KEY_SECONDARY": SECONDARY_KEY})
    account = services.add_account()
    services.fake.queue(fa.error(401, "authentication_error", "invalid x-api-key"), fa.reply())
    assert services.press(account).status_code == 200
    assert [request.bearer for request in services.fake.requests] == [PRIMARY_KEY, SECONDARY_KEY]
    alert = json.loads(services.one("SELECT value_json FROM service_state WHERE key = 'alert:ai_key_rejected'")["value_json"])
    assert (alert["key"], alert["retried_with"], alert["upstream_status"]) == ("primary", "secondary", 401)
    assert services.press(account).status_code == 200
    assert services.fake.requests[-1].bearer == SECONDARY_KEY  # the secondary stays first for a while


def test_keys_rejected_everywhere_is_ai_auth(make_claude):
    services = make_claude(overrides={"AI_API_KEY_SECONDARY": SECONDARY_KEY})
    account = services.add_account()
    services.fake.queue(fa.error(401, "authentication_error"), fa.error(403, "permission_error"))
    response = services.press(account)
    assert response.status_code == 502 and response.json()["error"]["code"] == "ai_auth"
    assert len(services.fake.requests) == 2


def test_a_rate_limit_is_retried_after_retry_after(services):
    account = services.add_account()
    services.fake.queue(fa.rate_limited(retry_after="3"), fa.reply())
    assert services.press(account).status_code == 200
    assert len(services.fake.requests) == 2 and services.clock.sleeps == [3.0]
    services.fake.queue(fa.rate_limited(), fa.rate_limited(), fa.rate_limited())
    response = services.press(account)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_rate_limited"
    assert response.headers["retry-after"]


@pytest.mark.parametrize("step", [fa.overloaded(), fa.server_error(500), fa.server_error(503),
                                  fa.connect_error(), fa.timeout()])
def test_overloads_server_errors_and_network_failures_are_retried(services, step):
    account = services.add_account()
    services.fake.queue(step, fa.reply())
    assert services.press(account).status_code == 200
    assert len(services.fake.requests) == 2 and len(services.clock.sleeps) == 1


def test_a_timed_out_attempt_is_estimated_and_a_refused_connection_is_free(services):
    account = services.add_account()
    services.fake.queue(fa.timeout(), fa.connect_error(), fa.reply())
    assert services.press(account).status_code == 200
    timed_out, refused, ok = calls(services)
    assert (timed_out["error_code"], timed_out["estimated"]) == ("timeout", 1) and timed_out["input_tokens"] > 0
    assert (refused["error_code"], refused["input_tokens"]) == ("network_error", 0)
    assert ok["status"] == "ok"


def test_server_errors_give_up_after_two_retries(services):
    account = services.add_account()
    services.fake.queue(fa.overloaded(), fa.overloaded(), fa.overloaded(), fa.reply())
    response = services.press(account)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_unavailable"
    assert len(services.fake.requests) == 3


@pytest.mark.parametrize(
    ("step", "status", "code"),
    [
        (fa.error(400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API."), 502, "ai_quota"),
        (fa.error(402, "billing_error", "Payment required."), 502, "ai_quota"),
        (fa.error(404, "not_found_error", "model: claude-haiku-5-5"), 502, "ai_model"),
        (fa.error(413, "request_too_large", "Request exceeds the maximum size"), 413, "input_too_large"),
        (fa.error(400, "invalid_request_error", "messages: text content blocks must be non-empty"), 400, "bad_request"),
    ],
)
def test_errors_that_retrying_cannot_fix_are_answered_at_once(services, step, status, code):
    account = services.add_account()
    services.fake.queue(step, fa.reply())
    response = services.press(account)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert len(services.fake.requests) == 1 and services.clock.sleeps == []


def test_models_without_a_price_are_refused_before_any_call(make_claude):
    services = make_claude(overrides={"AI_MODEL_QUALITY": "claude-unpriced-9"})
    account = services.add_account()
    response = services.press(account)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_unavailable"
    assert services.fake.requests == []


# ----------------------------------------------------------------------------- the rest of the services


def test_ai_check_and_status_name_the_provider(services):
    response = services.client.post("/admin/ai-check", headers=services.auth())
    body = response.json()
    assert body["ok"] is True and body["provider"] == "anthropic" and body["base_url"] == BASE_URL
    [result] = body["results"]
    assert result["model"] == MODEL and result["lanes"] == ["starling-quality", "starling-fast", "starling-vision"]
    [sent] = services.fake.requests
    assert sent.body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Reply with the single word OK."}]}]
    assert sent.body["max_tokens"] == 16 + reasoning_allowance("low")  # room to think before the one word
    status = services.client.get("/admin/status", headers=services.auth()).json()
    assert status["upstream"]["provider"] == "anthropic" and status["prices_configured"] == {MODEL: True}


def test_web_search_is_never_attempted_on_the_anthropic_path(make_claude):
    services = make_claude(overrides={"WEB_SEARCH": "true"})  # refused by validate in production
    account = services.add_account()
    response = services.client.post("/v1/search", json={"query": "Example Labs Model X release"},
                                    headers={**services.auth(account), "X-Starling-Request-Id": services.add_reservation(account)})
    assert response.status_code == 503 and response.json()["error"]["code"] == "search_unavailable"
    assert services.fake.requests == []


def test_the_news_digest_is_written_through_claude(make_claude, tmp_path):
    from .test_world import FakeHttp, standard_routes

    # The news fixtures are dated September 2026, so this test keeps the harness clock and prices
    # claude-haiku-5-5 from an earlier date in its own pricing file.
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({"models": [{"model": MODEL, "effective_from": "2026-01-01", "input_per_mtok": 0.10,
                                               "cached_input_per_mtok": 0.01, "output_per_mtok": 0.50}]}))
    from .conftest import NOW as SEPTEMBER

    services = make_claude(overrides={"AI_PRICING_FILE": str(pricing)}, now=SEPTEMBER, world_http=FakeHttp(standard_routes()))
    asyncio.run(services.app.state.world.pool.refresh())
    account = services.add_account()
    services.fake.queue(fa.reply("Example Labs released a new model [1]."))
    response = services.client.post("/v1/summary", json={"query_id": "ai-model-releases"}, headers=services.auth(account))
    assert response.status_code == 200
    assert response.json()["answer"] == "Example Labs released a new model [1]."
    [sent] = services.fake.requests
    assert sent.body["system"].startswith("You write a short news digest")
    assert "Topic: AI model release" in sent.body["messages"][0]["content"][0]["text"]
    assert sent.body["metadata"] == {"user_id": sha32("starling-world-summary")}
    [row] = services.rows("SELECT bucket, lane, model, status FROM ai_calls")
    assert row == {"bucket": "system", "lane": "starling-fast", "model": MODEL, "status": "ok"}


def test_a_real_socket_to_a_local_fake_through_ai_base_url(make_claude):
    fake = fa.FakeAnthropic()
    with fa.serve_loopback(fake) as base_url:
        services = make_claude(overrides={"AI_BASE_URL": base_url}, fake=fake, upstream_transport=None)
        account = services.add_account()
        plain = services.press(account)
        assert plain.status_code == 200 and plain.json()["choices"][0]["message"]["content"] == "Fake Claude text."
        streamed = services.press(account, chat_body(stream=True))
        assert sse_events(streamed.text)[-1] == "[DONE]"
    assert [request.path for request in fake.requests] == ["/v1/messages", "/v1/messages"]
    assert all(request.bearer == PRIMARY_KEY for request in fake.requests)
    assert [row["status"] for row in calls(services)] == ["ok", "ok"]


def test_logs_and_database_never_hold_prompts_outputs_or_keys(make_claude, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    services = make_claude(overrides={"AI_API_KEY_SECONDARY": SECONDARY_KEY})
    account = services.add_account()
    prompt = "PROMPT-CANARY-c1a0 quarterly roadmap"
    output = "OUTPUT-CANARY-77de"
    services.fake.queue(
        fa.error(401, "authentication_error", f"rejected while reading {prompt}"),
        fa.reply(output),
        fa.rate_limited(retry_after="1"),
        fa.reply((output,)),
        fa.error(400, "invalid_request_error", f"rejected: {prompt}"),
        fa.refusal(output),
    )
    assert services.press(account, chat_body(content=prompt)).status_code == 200
    assert output in services.press(account, chat_body(content=prompt, stream=True)).text
    assert services.press(account, chat_body(content=prompt)).status_code == 400
    assert services.press(account, chat_body(content=prompt)).status_code == 400
    captured = capsys.readouterr()
    for secret in (prompt, "PROMPT-CANARY", output, PRIMARY_KEY, SECONDARY_KEY):
        assert secret not in caplog.text
        assert secret not in captured.out and secret not in captured.err
    with services.db.read() as conn:
        for table in ("ai_calls", "service_state", "budget_counters", "usage_counters"):
            dump = json.dumps([dict(row) for row in conn.execute(f"SELECT * FROM {table}")])
            for secret in (prompt, output, PRIMARY_KEY, SECONDARY_KEY):
                assert secret not in dump


def test_the_openai_path_is_unchanged_by_default(make_services):
    services = make_services()
    assert services.settings.ai_provider == "openai"
    account = services.add_account()
    assert services.press(account).status_code == 200
    [sent] = services.fake.requests
    assert sent.path == "/v1/chat/completions" and sent.body["model"] == "gpt-5.6-luna"
