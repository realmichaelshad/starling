"""W4: one metered web search per press, through the proxy's upstream, never over a plan's ceiling."""
from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import date

import pytest

from starling_server.config import ConfigError
from starling_server.services.world import search as ws

from . import fake_openai as ai
from .conftest import NOW, SECONDARY_KEY, chat_body

QUERY = "Example Labs released Model X today with a 400k context window"
TODAY = date(2026, 9, 15)


def error_code(response) -> str:
    return response.json()["error"]["code"]


def search(services, account, *, query=QUERY, request_id=None, background=False, body=None, token=None):
    headers = {"Authorization": f"Bearer {token or services.token(account.id)}"}
    if background:
        headers["X-Starling-Bucket"] = "background"
    else:
        headers["X-Starling-Request-Id"] = request_id or services.add_reservation(account)
    return services.client.post("/v1/search", json=body if body is not None else {"query": query}, headers=headers)


def search_calls(services):
    return [request for request in services.fake.requests if request.path.endswith("/responses")]


def token_cost(services, inputs=3_200, cached=0, outputs=90):
    price = services.settings.ai_pricing.price_for(services.settings.web_search_model, TODAY)
    return price.cost_micros(inputs, cached, outputs)


TOOL_FEE = 10_000   # $10.00 per 1,000 calls


def test_a_search_goes_to_the_responses_endpoint_with_the_web_search_tool(services):
    account = services.add_account("free")
    response = search(services, account)
    assert response.status_code == 200, response.text
    [sent] = search_calls(services)
    assert sent.path == "/v1/responses" and sent.bearer == "test-primary-k3y-aaaa1111"
    body = sent.body
    assert body["model"] == services.settings.web_search_model
    assert body["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert body["include"] == ["web_search_call.action.sources"]
    assert body["store"] is False, "a search answer is not kept at the provider"
    assert body["reasoning"] == {"effort": "low"}, "effort none gives lower-quality search results"
    assert body["max_tool_calls"] == 1
    assert body["max_output_tokens"] == ws.VISIBLE_OUTPUT_TOKENS + 2048
    assert QUERY in body["input"] and "untrusted" in body["instructions"]
    assert body["safety_identifier"] != account.id and len(body["safety_identifier"]) == 32


def test_the_answer_comes_back_clean_with_the_sentence_each_source_supports(services):
    account = services.add_account("free")
    data = search(services, account).json()
    assert data["searches"] == 1 and data["queries"] == ["Example Labs Model X release"]
    assert "](" not in data["answer"] and "http" not in data["answer"], "citation markup is not part of the answer"
    assert data["answer"].startswith("Example Labs released Model X on September 14, 2026")
    assert data["sources"] == [
        {"title": "Example Labs releases Model X", "url": "https://news.example.com/model-x",
         "snippet": "Example Labs released Model X on September 14, 2026, with a 400,000-token context window."},
        {"title": "Pricing - Example Labs", "url": "https://example.com/pricing",
         "snippet": "Its API price is $1.25 per million input tokens, according to the company's pricing page."},
    ]


def test_a_search_is_metered_as_ai_cost_with_the_tool_fee(services):
    account = services.add_account("free")
    reservation = services.add_reservation(account)
    assert search(services, account, request_id=reservation).status_code == 200
    expected = token_cost(services) + TOOL_FEE
    assert expected == 10_748   # 3,200 x $0.20/M + 90 x $1.20/M + $0.01: "about a cent a search"
    [row] = services.rows("SELECT * FROM ai_calls")
    assert (row["lane"], row["bucket"], row["model"], row["reservation_id"], row["status"]) == (
        "starling-search", "press", services.settings.web_search_model, reservation, "ok")
    assert (row["input_tokens"], row["output_tokens"], row["estimated"], row["cost_micros"]) == (3_200, 90, 0, expected)
    counters = services.one("SELECT cost_micros FROM usage_counters WHERE account_id = ? AND period_id = ?",
                            (account.id, account.period_id))
    assert counters["cost_micros"] == expected, "the account's spend includes the search"
    held = services.one("SELECT cost_micros, ai_calls FROM usage_reservations WHERE id = ?", (reservation,))
    assert (held["cost_micros"], held["ai_calls"]) == (expected, 1)
    for scope in ("global_day", "global_month"):
        budget = services.one("SELECT cost_micros FROM budget_counters WHERE scope = ?", (scope,))
        assert budget["cost_micros"] == expected, f"{scope} counts the search"


def test_every_search_the_model_made_is_billed(services):
    account = services.add_account("free")
    services.fake.queue(ai.web_search_ok(searches=2))
    assert search(services, account).status_code == 200
    [row] = services.rows("SELECT cost_micros FROM ai_calls")
    assert row["cost_micros"] == token_cost(services) + 2 * TOOL_FEE


def test_an_answer_without_usage_is_estimated_and_never_free(services):
    account = services.add_account("free")
    services.fake.queue(ai.web_search_ok(include_usage=False))
    assert search(services, account).status_code == 200
    [row] = services.rows("SELECT estimated, input_tokens, cost_micros FROM ai_calls")
    assert row["estimated"] == 1 and row["input_tokens"] >= ws.ESTIMATED_SEARCH_INPUT_TOKENS["low"]
    assert row["cost_micros"] > TOOL_FEE


def test_one_search_per_press(services):
    account = services.add_account("free")
    reservation = services.add_reservation(account)
    assert search(services, account, request_id=reservation).status_code == 200
    again = search(services, account, request_id=reservation)
    assert again.status_code == 429 and error_code(again) == "search_cap_exceeded"
    assert len(search_calls(services)) == 1, "the refused search never reached the provider"


def test_the_daily_cap_per_account(make_services):
    services = make_services(overrides={"WEB_SEARCH_PER_ACCOUNT_DAY": "2"})
    account = services.add_account("pro")
    assert [search(services, account).status_code for _ in range(3)] == [200, 200, 429]
    other = services.add_account("pro")
    assert search(services, other).status_code == 200, "the cap is per account"
    services.clock.advance(86_400)
    assert search(services, account).status_code == 200, "a new UTC day"


def test_a_daily_cap_of_zero_means_no_searches(make_services):
    none = make_services(overrides={"WEB_SEARCH_PER_ACCOUNT_DAY": "0"})
    refused = search(none, none.add_account("pro"))
    assert refused.status_code == 429 and error_code(refused) == "search_cap_exceeded"
    assert search_calls(none) == []


def test_a_search_never_takes_an_account_over_its_plan_ceiling(services):
    account = services.add_account("free")
    service = services.app.state.world.search
    reserve = service.reserve_micros(len(QUERY), TODAY)
    room = account.ceiling_micros * (100 - ws.DRAFT_ROOM_PCT) // 100
    services.set_spend(account, cost_micros=room - reserve + 1)
    refused = search(services, account)
    assert refused.status_code == 429 and error_code(refused) == "search_cap_exceeded"
    assert search_calls(services) == []
    # The draft the press was for still runs: the search took nothing.
    assert services.press(account).status_code == 200
    services.set_spend(account, cost_micros=room - reserve)
    assert search(services, account).status_code == 200
    spent = services.one("SELECT cost_micros FROM usage_counters WHERE account_id = ?", (account.id,))["cost_micros"]
    assert spent < account.ceiling_micros


def test_the_reserve_really_bounds_a_search(services):
    """The ceiling promise rests on the reserve being the most one search can cost."""
    service = services.app.state.world.search
    reserve = service.reserve_micros(ws.MAX_QUERY_CHARS, TODAY)
    worst = token_cost(services, inputs=ws.WORST_INPUT_TOKENS["low"] + 200,
                       outputs=ws.VISIBLE_OUTPUT_TOKENS + 2048) + ws.WORST_TOOL_CALLS * TOOL_FEE
    assert reserve >= worst
    assert reserve < 30_000, "and it stays about two to three cents, so small plans still get searches"


def test_searches_stop_at_their_share_of_the_plan(services):
    account = services.add_account("free")
    statuses = [search(services, account).status_code for _ in range(7)]
    allowed = statuses.count(200)
    assert statuses[:allowed] == [200] * allowed and set(statuses[allowed:]) == {429}
    spent_on_search = services.one(
        "SELECT SUM(cost_micros) AS total FROM ai_calls WHERE lane = 'starling-search'")["total"]
    assert spent_on_search <= account.ceiling_micros * ws.SEARCH_SHARE_PCT // 100
    assert allowed == 5, "Free ($0.30) gets about five searches a period"


def test_a_search_counts_toward_the_per_request_call_cap(make_services):
    services = make_services(overrides={"AI_MAX_CALLS_PER_REQUEST": "1"})
    account = services.add_account("pro")
    reservation = services.add_reservation(account)
    assert services.chat(account, chat_body(), request_id=reservation).status_code == 200
    refused = search(services, account, request_id=reservation)
    assert refused.status_code == 429 and error_code(refused) == "call_cap_exceeded"


def test_owner_pause_and_global_budgets_stop_search(services):
    account = services.add_account("pro")
    services.set_service_state("ai_paused", "true")
    paused = search(services, account)
    assert paused.status_code == 503 and error_code(paused) == "budget_exhausted"
    services.set_service_state("ai_paused", None)
    services.set_budget("global_day", services.budget_limit("global_day"))
    assert error_code(search(services, account)) == "budget_exhausted"
    assert search_calls(services) == []


def test_only_a_customer_press_may_search(services):
    account = services.add_account("pro")
    background = search(services, account, background=True)
    assert background.status_code == 400 and error_code(background) == "bad_request"
    admin = services.client.post("/v1/search", json={"query": QUERY}, headers=services.auth())
    assert admin.status_code == 401
    suspended = services.add_account(status="suspended")
    assert search(services, suspended).status_code == 401
    assert search_calls(services) == []


@pytest.mark.parametrize("body", [{"query": "short"}, {"query": "x" * 301}, {"query": 12}, {},
                                  {"query": QUERY, "extra": 1}, {"query": QUERY, "timeout_s": 0}])
def test_bad_queries_are_refused(services, body):
    response = search(services, services.add_account("pro"), body=body)
    assert response.status_code == 400 and error_code(response) == "bad_request"
    assert search_calls(services) == []


def test_search_switched_off_is_refused(make_services):
    off = make_services(overrides={"WEB_SEARCH": "false"})
    response = search(off, off.add_account("pro"))
    assert response.status_code == 503 and error_code(response) == "search_unavailable"
    assert search_calls(off) == []
    assert ws.ensure_search_valid(dataclasses.replace(off.settings, env="production")) == []


def test_unpriced_search_is_refused_and_production_will_not_start(make_services, settings, tmp_path):
    unpriced_file = tmp_path / "ai_pricing.json"
    pricing = json.loads(open(settings.ai_pricing_file, encoding="utf-8").read())
    pricing.pop("tools")
    unpriced_file.write_text(json.dumps(pricing), encoding="utf-8")
    unpriced = make_services(overrides={"AI_PRICING_FILE": str(unpriced_file)})
    response = search(unpriced, unpriced.add_account("pro"))
    assert response.status_code == 503 and error_code(response) == "search_unavailable"
    assert unpriced.one("SELECT key FROM service_state WHERE key = 'alert:ai_web_search_unpriced'")
    assert search_calls(unpriced) == []
    assert ws.configuration_problems(unpriced.settings), "the process reports it at start"
    with pytest.raises(ConfigError):
        ws.ensure_search_valid(dataclasses.replace(unpriced.settings, env="production"))


def test_the_key_fallback_applies_to_search(make_services):
    services = make_services(overrides={"AI_API_KEY_SECONDARY": SECONDARY_KEY})
    services.fake.queue(ai.error(401, "invalid_api_key"), ai.web_search_ok())
    assert search(services, services.add_account("pro")).status_code == 200
    assert [request.bearer for request in search_calls(services)] == ["test-primary-k3y-aaaa1111", SECONDARY_KEY]
    assert services.one("SELECT key FROM service_state WHERE key = 'alert:ai_key_rejected'")


def test_a_model_that_refuses_an_optional_field_is_asked_once_without_it(services):
    account = services.add_account("pro")
    services.fake.queue(ai.unsupported("max_tool_calls"), ai.web_search_ok())
    assert search(services, account).status_code == 200
    first, second = search_calls(services)
    assert "max_tool_calls" in first.body and "max_tool_calls" not in second.body
    assert search(services, account).status_code == 200
    assert "max_tool_calls" not in search_calls(services)[-1].body, "remembered for the process"


def test_parse_ignores_sources_that_are_not_web_links():
    payload = {"output": [
        {"type": "web_search_call", "action": {"type": "search", "query": "q",
                                               "sources": [{"type": "url", "url": "javascript:alert(1)"}]}},
        {"type": "message", "content": [{"type": "output_text", "text": "A claim. ([x](ftp://x))",
                                         "annotations": [{"type": "url_citation", "start_index": 9, "end_index": 23,
                                                          "url": "ftp://x", "title": "x"}]}]},
    ], "usage": {"input_tokens": 10, "output_tokens": 2}}
    parsed = ws.parse_response(payload)
    assert parsed.sources == [] and parsed.search_calls == 1 and parsed.usage == (10, 0, 2)


def test_tool_prices_have_an_effective_date(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({"models": [], "tools": [
        {"tool": "web_search", "effective_from": "2026-09-22", "per_1k_calls": 10.0},
        {"tool": "web_search", "effective_from": "2027-01-01", "per_1k_calls": 12.5},
    ]}), encoding="utf-8")
    prices = ws.load_tool_prices(path)
    assert ws.tool_price_for(prices, "web_search", date(2026, 9, 21)) is None
    assert ws.tool_price_for(prices, "web_search", date(2026, 12, 31)).micros_per_call == 10_000
    assert ws.tool_price_for(prices, "web_search", date(2027, 1, 2)).micros_per_call == 12_500
    path.write_text(json.dumps({"tools": [{"tool": "web_search"}]}), encoding="utf-8")
    with pytest.raises(ConfigError):
        ws.load_tool_prices(path)


def test_the_shipped_price_file_prices_the_tool(settings):
    [price] = [p for p in ws.load_tool_prices(settings.ai_pricing_file) if p.tool == "web_search"]
    assert price.micros_per_call == 10_000 and price.effective_from == date(2026, 9, 14)
    assert ws.configuration_problems(settings) == []


def test_nothing_is_logged_but_counts(services, caplog):
    account = services.add_account("pro")
    with caplog.at_level("INFO"):
        assert search(services, account).status_code == 200
    text = " ".join(record.getMessage() for record in caplog.records)
    assert QUERY not in text and "example.com" not in text and account.id not in text
    assert uuid.UUID(account.id)


# --------------------------------------------------------------------------- background topic research
# API-native pass (2026-09-22): the engine's topic research job asks for one search at a time as
# BACKGROUND work (`X-Starling-Bucket: background`, purpose "topics"), metered inside the account's
# background share, with its own daily cap; a press may also ask for a topics search. (Background
# calls resolve the account's CURRENT period through the entitlements, so these use accounts whose
# own period is that one: the default, free, with its ceiling set per test.)

TOPICS = {"query": "What is new in the last few days in strength training and nutrition?", "purpose": "topics"}


def paid(services, plan="starter", *, ceiling_micros=None):
    """A paid account whose subscription the entitlements really resolve (background search is
    for paid plans only): its period row plus the mirrored subscription that owns it."""
    account = services.add_account(plan, ceiling_micros=ceiling_micros)
    period = services.one("SELECT subscription_id, period_start, period_end FROM entitlement_periods WHERE id = ?",
                          (account.period_id,))
    services.db.write(lambda conn: conn.execute(
        "INSERT INTO subscriptions (subscription_id, account_id, customer_id, status, price_id, plan_code, "
        "current_period_start, current_period_end, livemode, synced_at) VALUES (?, ?, ?, 'active', NULL, ?, ?, ?, 0, ?)",
        (period["subscription_id"], account.id, "cus_" + period["subscription_id"], plan,
         period["period_start"], period["period_end"], int(services.clock()))))
    return account


def refusal(response) -> dict:
    assert response.status_code == 429, response.text
    error = response.json()["error"]
    assert error["code"] == "search_cap_exceeded" and error["reason"] in ws.REASONS, error
    return error


def test_a_background_search_is_topic_research_metered_as_background(services):
    account = paid(services, "pro")
    response = search(services, account, background=True, body=TOPICS)
    assert response.status_code == 200, response.text
    [sent] = search_calls(services)
    assert "new developments" in sent.body["instructions"]
    assert sent.body["input"].startswith("Subject area to research:")
    [row] = services.rows("SELECT * FROM ai_calls")
    assert (row["lane"], row["bucket"], row["reservation_id"], row["status"]) == (
        "starling-search", "background", None, "ok")
    counters = services.one("SELECT cost_micros, background_micros FROM usage_counters WHERE account_id = ?",
                            (account.id,))
    assert counters["background_micros"] == row["cost_micros"] == counters["cost_micros"] > 0


def test_a_background_search_must_be_topic_research(services):
    account = services.add_account("pro")
    claim = search(services, account, background=True, body={"query": QUERY, "purpose": "claim"})
    assert claim.status_code == 400 and error_code(claim) == "bad_request"
    unknown = search(services, account, background=True, body={"query": QUERY, "purpose": "anything"})
    assert unknown.status_code == 400
    assert search_calls(services) == []


def test_a_press_may_ask_for_topics_too(services):
    account = services.add_account("pro")
    response = search(services, account, body=TOPICS)
    assert response.status_code == 200
    [row] = services.rows("SELECT bucket, lane FROM ai_calls")
    assert (row["bucket"], row["lane"]) == ("press", "starling-search")


def test_background_searches_have_their_own_daily_cap(make_services):
    services = make_services(overrides={"WEB_SEARCH_BACKGROUND_PER_ACCOUNT_DAY": "2"})
    assert services.settings.web_search_background_per_account_day == 2, "a real setting (A4)"
    account = paid(services, ceiling_micros=500_000_000)   # a pace of one search every few minutes
    statuses = []
    for number in range(3):
        # Distinct queries (the topics cache would answer a repeat for free) an hour apart (the pace).
        body = {**TOPICS, "query": f"{TOPICS['query']} ({number})"}
        statuses.append(search(services, account, background=True, body=body).status_code)
        services.clock.advance(3_600)
    assert statuses == [200, 200, 429]
    assert search(services, account).status_code == 200, "the press cap is separate"
    services.clock.advance(86_400)
    assert search(services, account, background=True, body=TOPICS).status_code == 200, "a new UTC day"


def test_background_search_stays_inside_the_background_share(services):
    account = paid(services, ceiling_micros=1_000_000)
    service = services.app.state.world.search
    expected = service.expected_micros(len(TOPICS["query"]), TODAY)
    allowance = 1_000_000 * 10 // 100 * ws.BACKGROUND_SEARCH_SHARE_PCT // 100      # 60% of the 10% share
    services.set_spend(account, cost_micros=allowance - expected + 1, background_micros=allowance - expected + 1)
    refused = search(services, account, background=True, body=TOPICS)
    assert refused.status_code == 429 and error_code(refused) == "search_cap_exceeded"
    assert search_calls(services) == []
    services.set_spend(account, cost_micros=allowance - expected, background_micros=allowance - expected)
    assert search(services, account, background=True, body=TOPICS).status_code == 200
    # A press still has its own room: the background share is only background's.
    assert services.press(account).status_code == 200


def test_background_search_never_takes_an_account_over_its_ceiling(services):
    account = paid(services, ceiling_micros=2_000_000)
    service = services.app.state.world.search
    reserve = service.reserve_micros(len(TOPICS["query"]), TODAY)
    room = 2_000_000 * (100 - ws.DRAFT_ROOM_PCT) // 100
    services.set_spend(account, cost_micros=room - reserve + 1)
    refused = search(services, account, background=True, body=TOPICS)
    assert refused.status_code == 429 and error_code(refused) == "search_cap_exceeded"
    assert search_calls(services) == []


def test_background_and_press_searches_share_the_search_share(services):
    """A plan's search share (25% of its ceiling) counts both kinds of search."""
    account = paid(services, ceiling_micros=100_000)
    service = services.app.state.world.search
    conn_value = services.db.write(lambda conn: service._searched_micros(conn, account.id, account.period_id))
    assert conn_value == 0
    assert search(services, account, background=True, body=TOPICS).status_code in (200, 429)
    searched = services.db.write(lambda conn: service._searched_micros(conn, account.id, account.period_id))
    background_rows = services.rows("SELECT cost_micros FROM ai_calls WHERE bucket = 'background'")
    assert searched == sum(row["cost_micros"] for row in background_rows)


# --------------------------------------------------------------------------- product review U5, U6, A3


def test_free_accounts_get_no_background_search(services):
    """U5: a Free period's background share is about one search, so Free gets its topics from the
    shared news pool and a press's own search instead."""
    account = services.add_account("free", ceiling_micros=50_000_000)
    error = refusal(search(services, account, background=True, body=TOPICS))
    assert error["reason"] == "background" and 0 < error["retry_after_s"] <= 86_400
    assert search_calls(services) == [], "never reaches the provider"
    assert search(services, account, body=TOPICS).status_code == 200, "a Free press may still search"


def test_background_searches_are_paced_to_last_the_period(services):
    """U5: runs per day = the background search budget left / one search's cost / days left."""
    account = paid(services, ceiling_micros=3_000_000)
    service = services.app.state.world.search
    expected = service.expected_micros(len(TOPICS["query"]) + 4, TODAY)
    room = 3_000_000 * 10 // 100 * ws.BACKGROUND_SEARCH_SHARE_PCT // 100
    first = {**TOPICS, "query": TOPICS["query"] + " (1)"}
    assert search(services, account, background=True, body=first).status_code == 200
    period_left = services.one("SELECT period_end FROM entitlement_periods WHERE id = ?",
                               (account.period_id,))["period_end"] - int(services.clock())
    spent = services.one("SELECT background_micros FROM usage_counters WHERE account_id = ?", (account.id,))
    searches_left = (room - spent["background_micros"]) / expected
    interval = period_left / searches_left
    assert interval > 3_600, "the pace is slower than the engine's own interval here"
    second = {**TOPICS, "query": TOPICS["query"] + " (2)"}
    error = refusal(search(services, account, background=True, body=second))
    assert error["reason"] == "background" and abs(error["retry_after_s"] - interval) < 120, (error, interval)
    services.clock.advance(int(error["retry_after_s"]) + 1)
    assert search(services, account, background=True, body=second).status_code == 200, "the next slot opens"


def test_every_refusal_says_why_and_when(make_services):
    """U6: `reason` and `retry_after_s` in the body, not only in the message."""
    services = make_services(overrides={"WEB_SEARCH_PER_ACCOUNT_DAY": "1"})
    account = services.add_account("pro")
    reservation = services.add_reservation(account)
    assert search(services, account, request_id=reservation).status_code == 200
    again = refusal(search(services, account, request_id=reservation))
    assert again["reason"] == "per_press" and again["retry_after_s"] is None
    daily = refusal(search(services, account))
    midnight = 86_400 - int(services.clock()) % 86_400
    assert daily["reason"] == "daily" and daily["retry_after_s"] == midnight
    other = services.add_account("free", ceiling_micros=10_000)
    share = refusal(search(services, other))
    period_end = services.one("SELECT period_end FROM entitlement_periods WHERE id = ?", (other.period_id,))["period_end"]
    assert share["reason"] == "plan_share" and share["retry_after_s"] == period_end - int(services.clock())


def test_a_search_that_runs_out_of_time_is_search_timeout(services):
    """A3: the search's own deadline is its own code, never mistaken for the provider being down."""
    account = services.add_account("pro")
    services.fake.queue(ai.timeout(), ai.timeout())
    response = search(services, account)
    assert response.status_code == 504 and error_code(response) == "search_timeout", response.text
    assert response.json()["error"]["reason"] == "timeout"


def test_a_topics_search_is_shared_for_an_hour_and_a_claim_check_never_is(services):
    """U5: one topics search serves every account asking the same query for 60 minutes, free."""
    first, second = paid(services), services.add_account("free")
    assert search(services, first, background=True, body=TOPICS).status_code == 200
    shared = search(services, second, body=TOPICS)
    assert shared.status_code == 200 and shared.json().get("cached") is True
    assert len(search_calls(services)) == 1, "answered from the cache"
    assert services.rows("SELECT * FROM ai_calls WHERE account_id = ?", (second.id,)) == [], "and not metered"
    services.clock.advance(ws.TOPICS_CACHE_TTL_S)
    assert search(services, second, body=TOPICS).json().get("cached") is not True, "an hour later: a new search"
    assert search(services, second).status_code == 200 and search(services, second).status_code == 200
    assert len(search_calls(services)) == 4, "claim checks are never cached"
