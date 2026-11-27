"""The httpx Stripe client: encoding, headers, idempotency, retries, error mapping, pagination."""
from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qsl

import httpx
import pytest

from starling_server.billing.stripe_client import (
    StripeAPIError,
    StripeAuthenticationError,
    StripeCardError,
    StripeClient,
    StripeConnectionError,
    StripeIdempotencyError,
    StripeInvalidRequestError,
    StripeNotConfigured,
    StripeNotFoundError,
    StripeRateLimitError,
    encode_params,
)
from starling_server.config import load_settings

from .conftest import billing_env, make_client


def scripted(settings, responses, seen, **options):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return StripeClient(settings, transport=httpx.MockTransport(handler), sleep=lambda _s: None, **options)


def test_encode_params_uses_bracket_notation():
    pairs = encode_params({
        "mode": "subscription",
        "line_items": [{"price": "price_1", "quantity": 1}],
        "payment_method_options": {"card": {"restrictions": {"funding_types_blocked": ["prepaid"]}}},
        "flag": True,
        "off": False,
        "skip": None,
        "blank": "",
        "expand": ["latest_invoice"],
    })
    assert pairs == [
        ("mode", "subscription"),
        ("line_items[0][price]", "price_1"),
        ("line_items[0][quantity]", "1"),
        ("payment_method_options[card][restrictions][funding_types_blocked][0]", "prepaid"),
        ("flag", "true"),
        ("off", "false"),
        ("blank", ""),
        ("expand[0]", "latest_invoice"),
    ]


def test_headers_and_idempotency_key_only_on_post(bsettings):
    seen: list[httpx.Request] = []
    ok = httpx.Response(200, json={"id": "cus_1", "object": "customer"})
    client = scripted(bsettings, [ok, ok, ok], seen)
    client.create_customer({"email": "member@example.com", "metadata": {"account_id": "a1"}}, idempotency_key="cust-a1")
    client.retrieve_customer("cus_1")
    client.cancel_subscription("sub_1")
    post, get, delete = seen
    assert post.headers["authorization"] == f"Bearer {bsettings.stripe_secret_key}"
    assert post.headers["stripe-version"] == "2026-08-26.dahlia"
    assert post.headers["idempotency-key"] == "cust-a1"
    assert post.headers["content-type"] == "application/x-www-form-urlencoded"
    assert dict(parse_qsl(post.content.decode())) == {"email": "member@example.com", "metadata[account_id]": "a1"}
    assert str(post.url).startswith(bsettings.stripe_api_base + "/v1/customers")
    assert "idempotency-key" not in get.headers and "idempotency-key" not in delete.headers
    assert dict(delete.url.params) == {"prorate": "false", "invoice_now": "false"}


def test_retries_keep_the_same_generated_idempotency_key(bsettings):
    seen: list[httpx.Request] = []
    responses = [httpx.Response(500, json={"error": {"type": "api_error"}}),
                 httpx.Response(429, json={"error": {"type": "invalid_request_error", "code": "lock_timeout"}}),
                 httpx.Response(200, json={"id": "cs_1"})]
    client = scripted(bsettings, responses, seen)
    assert client.create_checkout_session({"mode": "subscription"})["id"] == "cs_1"
    keys = {request.headers["idempotency-key"] for request in seen}
    assert len(seen) == 3 and len(keys) == 1 and keys.pop().startswith("starling-")


def test_retries_stop_after_the_limit_and_on_client_errors(bsettings):
    seen: list[httpx.Request] = []
    failing = [httpx.Response(503, json={"error": {"type": "api_error"}})] * 3
    with pytest.raises(StripeAPIError):
        scripted(bsettings, failing, seen).retrieve_invoice("in_1")
    assert len(seen) == 3
    seen.clear()
    with pytest.raises(StripeInvalidRequestError):
        scripted(bsettings, [httpx.Response(400, json={"error": {"type": "invalid_request_error"}})], seen).retrieve_invoice("in_1")
    assert len(seen) == 1


def test_should_retry_false_is_respected(bsettings):
    seen: list[httpx.Request] = []
    conflict = httpx.Response(409, json={"error": {"type": "invalid_request_error"}}, headers={"stripe-should-retry": "false"})
    with pytest.raises(StripeInvalidRequestError):
        scripted(bsettings, [conflict], seen).retrieve_charge("ch_1")
    assert len(seen) == 1


def test_connection_errors_are_retried_then_raised(bsettings):
    seen: list[httpx.Request] = []
    errors = [httpx.ConnectError("down")] * 3
    with pytest.raises(StripeConnectionError):
        scripted(bsettings, errors, seen).retrieve_account()
    assert len(seen) == 3


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (402, {"type": "card_error", "code": "card_declined"}, StripeCardError),
        (400, {"type": "idempotency_error"}, StripeIdempotencyError),
        (404, {"type": "invalid_request_error", "code": "resource_missing"}, StripeNotFoundError),
        (429, {"type": "invalid_request_error", "code": "rate_limit"}, StripeRateLimitError),
        (401, {"type": "invalid_request_error"}, StripeAuthenticationError),
        (500, {"type": "api_error"}, StripeAPIError),
        (400, {"type": "invalid_request_error", "code": "parameter_invalid"}, StripeInvalidRequestError),
    ],
)
def test_errors_map_to_types(bsettings, status, error, expected):
    seen: list[httpx.Request] = []
    response = httpx.Response(status, json={"error": {**error, "message": "no"}}, headers={"request-id": "req_9"})
    with pytest.raises(expected) as caught:
        scripted(bsettings, [response], seen, max_retries=0).retrieve_price("price_1")
    assert caught.value.http_status == status and caught.value.request_id == "req_9"


def test_pagination_follows_has_more(bsettings, fake, stripe):
    for index in range(205):
        fake._create_product({"name": f"Product {index}"})
    assert len(stripe.list_products(active=True)) == 205
    assert fake.calls("GET", "/v1/products") == 3


def test_list_events_batches_types(bsettings, fake, stripe):
    types = [f"type.{index}" for index in range(21)]
    assert list(stripe.list_events(types=types, created_gte=0)) == []
    assert fake.calls("GET", "/v1/events") == 2


def test_object_ids_in_paths_are_validated(stripe, fake):
    for bad_id in ("../account", "cs_1/expire", "in 1", ""):
        with pytest.raises(ValueError):
            stripe.retrieve_checkout_session(bad_id)
    assert fake.requests == []


def test_idempotent_replay_and_parameter_mismatch(stripe, fake):
    first = stripe.create_customer({"email": "a@example.com"}, idempotency_key="cust-replay")
    again = stripe.create_customer({"email": "a@example.com"}, idempotency_key="cust-replay")
    assert first["id"] == again["id"] and fake.creates["customer"] == 1
    with pytest.raises(StripeIdempotencyError):
        stripe.create_customer({"email": "b@example.com"}, idempotency_key="cust-replay")


def test_pinned_version_is_required_by_the_fake(bsettings, fake):
    with make_client(replace(bsettings, stripe_api_version="2020-08-27"), fake) as client:
        with pytest.raises(StripeInvalidRequestError):
            client.retrieve_account()


def test_client_refuses_to_start_without_a_key(tmp_path):
    env = billing_env(tmp_path / "data")
    env.pop("STRIPE_SECRET_KEY")
    with pytest.raises(StripeNotConfigured):
        StripeClient(load_settings(env))
