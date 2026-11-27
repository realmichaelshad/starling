"""Journey 3: drafting through a real engine worker (local, mocked providers).

A real engine process is spawned for the customer, it calls the services proxy, the proxy
calls the fake OpenAI, and a usable draft comes back. The journey checks the stream contract
of CONTRACTS 6.4 line by line, that exactly one credit is committed, that all three views of
the account agree, and that every `ai_calls` row equals the fake's own token counts priced
with `config/ai_pricing.json`.
"""
from __future__ import annotations

import pytest

from e2e.harness.paths import AI_PRICING_FILE
from e2e.tests.client import Account, Control, assist_body, stream_body
from e2e.tests.pricing import cost_micros, tool_fee_micros

COMPOSER_DRAFT = (
    "I spent the week replacing our retry logic and the only thing that mattered was the "
    "timeout, not the backoff."
)
MODEL = "gpt-5.6-luna"


def assert_stream_contract(result, key: str) -> None:
    """CONTRACTS 6.4: `accepted` first, then tokens, then exactly one `final`."""
    assert result.status == 200, result.events
    assert result.headers.get("content-type", "").startswith("application/x-ndjson")
    assert result.headers.get("cache-control") == "no-store, no-transform"
    assert result.headers.get("x-accel-buffering") == "no"
    first = result.events[0]
    assert first == {"event": "status", "stage": "accepted", "request_id": key}, first
    assert result.error is None, result.error
    assert len(result.of("final")) == 1, [event.get("event") for event in result.events]
    assert result.of("token"), "no tokens were streamed"
    assert result.usage is not None
    kinds = [event.get("event") for event in result.events]
    assert kinds.index("usage") < kinds.index("final"), kinds
    response = result.final["response"]
    assert response["ok"] is True
    draft = (response.get("decision") or {}).get("draft")
    assert (draft or response.get("answer") or "").strip(), "the final response carries no usable text"


def assert_charged_once(harness: Control, account: Account, key: str, before: dict) -> dict:
    """One credit committed, and the page, the session and the database agreeing."""
    reservation = harness.one(
        "SELECT * FROM usage_reservations WHERE account_id = ? AND idempotency_key = ?", (account.id, key)
    )
    assert reservation is not None, "no reservation was recorded"
    assert reservation["state"] == "committed", reservation["state"]
    assert reservation["credits"] == 1 and reservation["exposure_at"] is not None
    ledger = harness.one("SELECT * FROM credit_ledger WHERE reservation_id = ?", (reservation["id"],))
    assert ledger is not None and ledger["kind"] == "commit" and ledger["amount"] == 1

    counters = harness.counters(account.id)
    assert counters["committed"] == before["committed"] + 1
    assert counters["reserved"] == 0

    session = account.device.session()
    page = account.site.account_usage()
    assert session["usage"]["used"] == counters["committed"]
    assert page["usage"]["used"] == counters["committed"]
    assert page["usage"]["remaining"] == session["usage"]["remaining"]
    return reservation


def assert_costs_match_the_provider(harness: Control, account: Account, reservation_id: str) -> None:
    """Every `ai_calls` row equals a call the fake really answered, priced from the price file.

    A web search row (lane `starling-search`, W4) is its tokens at the model's price plus the
    tool's per-call fee: the stand-in makes exactly one search call per request.
    """
    calls = harness.ai_calls(reservation_id=reservation_id)
    assert calls, "the draft recorded no AI call"
    provider = harness.ai_requests()["calls"]
    answered = [call for call in provider if call["outcome"] in ("json", "stream")]
    counted = {(call["prompt_tokens"], call["completion_tokens"]) for call in answered}
    searched = {(call["prompt_tokens"], call["completion_tokens"]) for call in provider if call["outcome"] == "search"}
    total = 0
    for row in calls:
        assert row["status"] == "ok", row
        assert row["model"] == MODEL and row["bucket"] == "press"
        assert row["estimated"] == 0, "the provider reported usage, so nothing should be estimated"
        search = row["lane"] == "starling-search"
        assert (row["input_tokens"], row["output_tokens"]) in (searched if search else counted), (row, sorted(counted))
        expected = cost_micros(
            AI_PRICING_FILE, MODEL,
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
            cached_tokens=row["cached_input_tokens"],
        ) + (tool_fee_micros(AI_PRICING_FILE) if search else 0)
        assert row["cost_micros"] == expected, (row["cost_micros"], expected, row)
        total += expected
    reservation = harness.one("SELECT cost_micros, ai_calls FROM usage_reservations WHERE id = ?", (reservation_id,))
    assert reservation["cost_micros"] == total
    assert reservation["ai_calls"] == len(calls)
    counters = harness.counters(account.id)
    assert counters["cost_micros"] >= total


# --------------------------------------------------------------------------- the journey


def test_a_metered_request_without_consent_is_refused(accounts, consent_version):
    account = accounts("drafting", with_device=True)
    if account.extras.get("consented"):
        pytest.skip("this account already accepted the disclosure in an earlier test")
    response = account.device.post("/assist/stream", stream_body("chat", "Draft me something."))
    assert response.status_code == 403 and response.json()["error"]["code"] == "consent_required"


def test_consent_is_recorded_for_the_current_version(ready_account, harness: Control, consent_version):
    account = ready_account("drafting")
    session = account.device.session()
    assert session["consent"]["required_version"] == consent_version
    assert session["consent"]["accepted_version"] == consent_version
    row = harness.one(
        "SELECT * FROM consents WHERE account_id = ? AND version = ?", (account.id, consent_version)
    )
    assert row is not None and row["revoked_at"] is None
    refused = account.device.post("/consent", {"version": "1999-01-01.1", "passive_learning": False})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "bad_request"


def test_chat_streams_a_real_draft_and_charges_one_credit(ready_account, harness: Control, fresh_key):
    account = ready_account("drafting")
    before = harness.counters(account.id)
    key = fresh_key()

    result = account.device.stream(
        stream_body("chat", "Write me a post about shipping a small feature this week."), key=key
    )

    assert_stream_contract(result, key)
    assert result.text.strip(), "no visible tokens reached the client"
    reservation = assert_charged_once(harness, account, key, before)
    assert reservation["mode"] == "chat" and reservation["route"] == "/assist/stream"
    assert_costs_match_the_provider(harness, account, reservation["id"])
    assert result.usage["used"] == harness.counters(account.id)["committed"]


def test_enhance_composer_streams_a_rewrite_of_the_writers_own_draft(ready_account, harness: Control, fresh_key):
    account = ready_account("drafting")
    before = harness.counters(account.id)
    key = fresh_key()

    result = account.device.stream(
        stream_body(
            "enhance_composer",
            composer={"action": "post", "draft_text": COMPOSER_DRAFT, "intent": "enhance"},
        ),
        key=key,
    )

    assert_stream_contract(result, key)
    reservation = assert_charged_once(harness, account, key, before)
    assert reservation["mode"] == "enhance_composer"
    assert_costs_match_the_provider(harness, account, reservation["id"])


def test_the_engine_really_ran_in_its_own_process_with_its_own_data_folder(ready_account, harness: Control, stack):
    account = ready_account("drafting")
    engine = harness.one("SELECT * FROM engines WHERE account_id = ?", (account.id,))
    assert engine is not None and engine["state"] == "running" and engine["pid"] > 0
    assert engine["listen"].startswith("tcp:127.0.0.1:")
    from pathlib import Path

    folder = Path(stack["data_dir"]) / "tenants" / str(engine["tenant_seq"])
    assert folder.is_dir(), folder
    assert any(folder.iterdir()), "the engine wrote nothing to its own folder"


def test_assist_context_charges_once_and_schedule_does_not(ready_account, harness: Control, fresh_key):
    account = ready_account("drafting")
    before = harness.counters(account.id)

    context_key = fresh_key()
    context = account.device.post(
        "/assist",
        assist_body(
            "context",
            context={
                "author": "A Developer",
                "author_handle": "adeveloper",
                "text": "We cut our build time in half by deleting two steps nobody ran.",
                "visibility": "public",
            },
        ),
        key=context_key,
    )
    assert context.status_code == 200, context.text[:400]
    body = context.json()
    assert "usage" in body
    reservation = harness.one(
        "SELECT * FROM usage_reservations WHERE account_id = ? AND idempotency_key = ?", (account.id, context_key)
    )
    assert reservation["route"] == "/assist" and reservation["mode"] == "context"
    assert reservation["state"] in ("committed", "released")
    charged_after_context = harness.counters(account.id)["committed"]
    if reservation["state"] == "committed":
        assert charged_after_context == before["committed"] + 1
    else:
        assert charged_after_context == before["committed"], "a released request must not be charged"

    schedule_key = fresh_key()
    schedule = account.device.post("/assist", assist_body("schedule", schedule={"op": "list"}), key=schedule_key)
    assert schedule.status_code == 200, schedule.text[:400]
    assert harness.one(
        "SELECT id FROM usage_reservations WHERE account_id = ? AND idempotency_key = ?",
        (account.id, schedule_key),
    ) is None, "the schedule mode is not metered"
    assert harness.counters(account.id)["committed"] == charged_after_context


def test_a_claim_is_checked_with_one_metered_web_search(ready_account, harness: Control, fresh_key):
    """W4 through the whole stack: gateway, engine, services proxy, the stand-in's `/v1/responses`.

    A public post with a checkable, time-sensitive claim that links nothing (and a news pool with
    nothing on it) gets ONE web search. Its cited pages reach the card as links, and the search
    is metered on the press: its tokens at the model's price plus the tool's per-call fee.
    """
    # Its own account: the drafting account's journeys above use up its per-minute rate.
    account = ready_account("web-search")
    key = fresh_key()
    response = account.device.post(
        "/assist",
        assist_body("context", context={
            "author": "AI Watcher",
            "author_handle": "aiwatcher",
            "text": "Example Labs released Model X today with a 400k context window.",
            "visibility": "public",
        }),
        key=key,
    )
    assert response.status_code == 200, response.text[:400]
    decision = response.json()["decision"]
    reservation = harness.one(
        "SELECT * FROM usage_reservations WHERE account_id = ? AND idempotency_key = ?", (account.id, key)
    )
    calls = harness.ai_calls(reservation_id=reservation["id"])
    searches = [row for row in calls if row["lane"] == "starling-search"]
    assert len(searches) == 1, [(row["lane"], row["status"]) for row in calls]
    assert decision.get("draft"), decision
    assert decision["web_checked"] is True
    sources = decision.get("sources") or []
    assert 1 <= len(sources) <= 3 and all(source["url"].startswith("https://") for source in sources), sources
    assert sources[0]["url"].startswith("https://news.example.com/"), sources
    assert_costs_match_the_provider(harness, account, reservation["id"])


def test_the_stream_route_refuses_the_assist_modes_and_the_other_way_round(ready_account, fresh_key):
    account = ready_account("drafting")
    wrong_stream = account.device.post("/assist/stream", stream_body("context"), key=fresh_key())
    assert wrong_stream.status_code == 400 and wrong_stream.json()["error"]["code"] == "bad_request"
    wrong_assist = account.device.post("/assist", assist_body("chat", message="hello"), key=fresh_key())
    assert wrong_assist.status_code == 400 and wrong_assist.json()["error"]["code"] == "bad_request"


def test_negative_control_the_draft_assertions_fail_when_the_provider_is_broken(
    ready_account, harness: Control, fresh_key
):
    """With the fake AI provider failing, the same assertions must not pass.

    Only a harness flag changes: `POST /_control/fail` on the fake provider. The product is
    untouched, and the helpers are the ones the happy path uses.
    """
    account = ready_account("drafting")
    before = harness.counters(account.id)
    key = fresh_key()
    harness.ai_fail("server_error", times=40)

    result = account.device.stream(stream_body("chat", "Write me a post about a broken provider."), key=key)

    with pytest.raises(AssertionError):
        assert_stream_contract(result, key)
    with pytest.raises(AssertionError):
        assert_charged_once(harness, account, key, before)
    assert harness.counters(account.id)["committed"] == before["committed"], "nothing may be charged"
    harness.ai_reset()
