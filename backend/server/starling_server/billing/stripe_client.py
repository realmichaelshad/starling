"""Minimal Stripe API client over httpx (no SDK), pinned to `settings.stripe_api_version`.

* Requests carry `Authorization: Bearer <key>` and `Stripe-Version`.
* Parameters use Stripe's form encoding: nested objects as `a[b][c]`, arrays as `a[0]`.
* Every POST carries an `Idempotency-Key` (the caller's, or a generated one) so the
  built-in retries (connection errors, 409 conflicts, 429, 5xx) never duplicate writes.
  GET and DELETE are idempotent by definition and never carry the header.
* Errors map to `StripeError` subclasses by HTTP status and `error.type`.
* The base URL comes from `STRIPE_API_BASE`, so tests point the client at a fake.
"""
from __future__ import annotations

import logging
import random
import re
import time
import uuid
from typing import Any, Callable, Iterator, Mapping, Sequence

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({409, 429, 500, 502, 503, 504})
_ID_SEGMENT = re.compile(r"^[A-Za-z0-9_]{1,255}$")
USER_AGENT = "Starling-Billing/1"


# --------------------------------------------------------------------------- errors


class StripeError(Exception):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        error_type: str | None = None,
        code: str | None = None,
        param: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.error_type = error_type
        self.code = code
        self.param = param
        self.request_id = request_id

    def __str__(self) -> str:
        bits = [self.message]
        if self.http_status:
            bits.append(f"status={self.http_status}")
        if self.error_type:
            bits.append(f"type={self.error_type}")
        if self.code:
            bits.append(f"code={self.code}")
        if self.request_id:
            bits.append(f"request_id={self.request_id}")
        return " ".join(bits)


class StripeNotConfigured(StripeError):
    pass


class StripeConnectionError(StripeError):
    pass


class StripeInvalidRequestError(StripeError):
    pass


class StripeNotFoundError(StripeInvalidRequestError):
    pass


class StripeAuthenticationError(StripeError):
    pass


class StripePermissionError(StripeError):
    pass


class StripeCardError(StripeError):
    pass


class StripeIdempotencyError(StripeError):
    pass


class StripeRateLimitError(StripeError):
    pass


class StripeAPIError(StripeError):
    pass


def error_from_response(response: httpx.Response) -> StripeError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    status = response.status_code
    kwargs = {
        "http_status": status,
        "error_type": error.get("type"),
        "code": error.get("code"),
        "param": error.get("param"),
        "request_id": response.headers.get("request-id"),
    }
    message = str(error.get("message") or f"Stripe API error (HTTP {status})")
    error_type = error.get("type")
    if error_type == "idempotency_error":
        return StripeIdempotencyError(message, **kwargs)
    if error_type == "card_error" or status == 402:
        return StripeCardError(message, **kwargs)
    if status == 401:
        return StripeAuthenticationError(message, **kwargs)
    if status == 403:
        return StripePermissionError(message, **kwargs)
    if status == 404:
        return StripeNotFoundError(message, **kwargs)
    if status == 429:
        return StripeRateLimitError(message, **kwargs)
    if status >= 500 or error_type == "api_error":
        return StripeAPIError(message, **kwargs)
    return StripeInvalidRequestError(message, **kwargs)


# --------------------------------------------------------------------------- encoding


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def encode_params(params: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """Flatten parameters into Stripe's bracket notation. `None` values are omitted;
    an empty string is sent as-is (Stripe uses it to unset a field)."""
    pairs: list[tuple[str, str]] = []

    def walk(prefix: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                walk(f"{prefix}[{key}]" if prefix else str(key), item)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(f"{prefix}[{index}]", item)
        else:
            pairs.append((prefix, _scalar(value)))

    walk("", params or {})
    return pairs


def seg(value: str) -> str:
    """A Stripe object id used in a URL path; anything else is refused."""
    if not isinstance(value, str) or not _ID_SEGMENT.fullmatch(value):
        raise ValueError("invalid Stripe object id")
    return value


def object_id(value: Any) -> str | None:
    """The id of a field that may be a string id or an expanded object."""
    if isinstance(value, Mapping):
        found = value.get("id")
        return found if isinstance(found, str) else None
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------- client


class StripeClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.stripe_secret_key:
            raise StripeNotConfigured("STRIPE_SECRET_KEY is not set")
        self.settings = settings
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=settings.stripe_api_base,
            transport=transport,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {settings.stripe_secret_key}",
                "Stripe-Version": settings.stripe_api_version,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )

    # ---- lifecycle
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "StripeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- core
    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        encoded = encode_params(params)
        headers: dict[str, str] = {}
        if method == "POST":
            headers["Idempotency-Key"] = idempotency_key or f"starling-{uuid.uuid4()}"
        attempt = 0
        while True:
            try:
                if method == "POST":
                    response = self._http.request(
                        method,
                        path,
                        content=_form_body(encoded),
                        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                    )
                else:
                    response = self._http.request(method, path, params=encoded, headers=headers)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    self._backoff(attempt, None)
                    attempt += 1
                    continue
                raise StripeConnectionError(f"could not reach Stripe: {type(exc).__name__}") from exc
            if response.status_code < 400:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise StripeAPIError("Stripe returned a non-JSON response", http_status=response.status_code) from exc
                if not isinstance(body, dict):
                    raise StripeAPIError("Stripe returned an unexpected response", http_status=response.status_code)
                return body
            if self._should_retry(response) and attempt < self.max_retries:
                self._backoff(attempt, response)
                attempt += 1
                continue
            raise error_from_response(response)

    def _should_retry(self, response: httpx.Response) -> bool:
        hint = response.headers.get("stripe-should-retry")
        if hint == "false":
            return False
        if hint == "true":
            return True
        return response.status_code in RETRY_STATUSES

    def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        delay = min(5.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.25)
        if response is not None:
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                delay = max(delay, min(float(retry_after), 10.0))
        self._sleep(delay)

    def list_all(
        self, path: str, params: Mapping[str, Any] | None = None, *, limit: int = 100, max_pages: int = 1000
    ) -> Iterator[dict[str, Any]]:
        """Iterate every object of a list endpoint, following `has_more` / `starting_after`."""
        query = dict(params or {})
        query["limit"] = limit
        for _ in range(max_pages):
            page = self.request("GET", path, query)
            data = page.get("data") or []
            yield from data
            if not page.get("has_more") or not data:
                return
            query["starting_after"] = data[-1]["id"]
        raise StripeAPIError(f"pagination did not finish for {path}")

    # ---- customers
    def create_customer(self, params: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return self.request("POST", "/v1/customers", params, idempotency_key=idempotency_key)

    def retrieve_customer(self, customer_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/customers/{seg(customer_id)}")

    # ---- checkout sessions
    def create_checkout_session(self, params: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.request("POST", "/v1/checkout/sessions", params, idempotency_key=idempotency_key)

    def retrieve_checkout_session(self, session_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/checkout/sessions/{seg(session_id)}")

    def expire_checkout_session(self, session_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/checkout/sessions/{seg(session_id)}/expire")

    def list_checkout_sessions(self, *, customer: str, status: str | None = None) -> list[dict[str, Any]]:
        return list(self.list_all("/v1/checkout/sessions", {"customer": customer, "status": status}))

    # ---- customer portal
    def create_portal_session(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/billing_portal/sessions", params)

    def create_portal_configuration(self, params: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.request("POST", "/v1/billing_portal/configurations", params, idempotency_key=idempotency_key)

    def list_portal_configurations(self, *, active: bool | None = None) -> list[dict[str, Any]]:
        return list(self.list_all("/v1/billing_portal/configurations", {"active": active}))

    # ---- subscriptions
    def retrieve_subscription(self, subscription_id: str, *, expand: Sequence[str] = ()) -> dict[str, Any]:
        params = {"expand": list(expand)} if expand else None
        return self.request("GET", f"/v1/subscriptions/{seg(subscription_id)}", params)

    def list_subscriptions(
        self, *, customer: str, status: str = "all", expand: Sequence[str] = ("data.latest_invoice",)
    ) -> list[dict[str, Any]]:
        # `latest_invoice` is expanded so a listed subscription mirrors like a retrieved one.
        params = {"customer": customer, "status": status, "expand": list(expand) or None}
        return list(self.list_all("/v1/subscriptions", params))

    def update_subscription(
        self, subscription_id: str, params: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.request("POST", f"/v1/subscriptions/{seg(subscription_id)}", params, idempotency_key=idempotency_key)

    def cancel_subscription(self, subscription_id: str, *, prorate: bool = False, invoice_now: bool = False) -> dict[str, Any]:
        return self.request(
            "DELETE", f"/v1/subscriptions/{seg(subscription_id)}", {"prorate": prorate, "invoice_now": invoice_now}
        )

    # ---- subscription schedules
    def create_schedule_from_subscription(self, subscription_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.request(
            "POST", "/v1/subscription_schedules", {"from_subscription": seg(subscription_id)}, idempotency_key=idempotency_key
        )

    def retrieve_subscription_schedule(self, schedule_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/subscription_schedules/{seg(schedule_id)}")

    def update_subscription_schedule(
        self, schedule_id: str, params: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.request(
            "POST", f"/v1/subscription_schedules/{seg(schedule_id)}", params, idempotency_key=idempotency_key
        )

    def release_subscription_schedule(self, schedule_id: str, *, preserve_cancel_date: bool | None = None) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/subscription_schedules/{seg(schedule_id)}/release",
            {"preserve_cancel_date": preserve_cancel_date},
        )

    # ---- invoices, payments, charges, disputes
    def retrieve_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/invoices/{seg(invoice_id)}")

    def list_invoices(
        self, *, subscription: str, status: str | None = None, created_gte: int | None = None
    ) -> list[dict[str, Any]]:
        params = {
            "subscription": seg(subscription),
            "status": status,
            "created": {"gte": int(created_gte)} if created_gte is not None else None,
        }
        return list(self.list_all("/v1/invoices", params))

    def list_invoice_payments_for_payment_intent(self, payment_intent_id: str) -> list[dict[str, Any]]:
        # Charges carry no invoice field in this API version; invoice payments link them.
        params = {"payment": {"type": "payment_intent", "payment_intent": seg(payment_intent_id)}}
        return list(self.list_all("/v1/invoice_payments", params))

    def retrieve_charge(self, charge_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/charges/{seg(charge_id)}")

    def retrieve_dispute(self, dispute_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/disputes/{seg(dispute_id)}")

    def list_disputes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """The most recent disputes, newest first. One page, deliberately: this only answers
        "is a chargeback still open", and an account with 100 disputes needs a human anyway."""
        return list(self.request("GET", "/v1/disputes", {"limit": limit}).get("data") or [])

    # ---- products and prices
    def list_prices(self, *, lookup_keys: Sequence[str], active: bool | None = True) -> list[dict[str, Any]]:
        keys = list(dict.fromkeys(lookup_keys))
        found: list[dict[str, Any]] = []
        for start in range(0, len(keys), 10):  # Stripe accepts up to 10 lookup keys per call
            found.extend(self.list_all("/v1/prices", {"lookup_keys": keys[start:start + 10], "active": active}))
        return found

    def retrieve_price(self, price_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/prices/{seg(price_id)}")

    def create_price(self, params: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return self.request("POST", "/v1/prices", params, idempotency_key=idempotency_key)

    def list_products(self, *, active: bool | None = True) -> list[dict[str, Any]]:
        return list(self.list_all("/v1/products", {"active": active}))

    def create_product(self, params: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return self.request("POST", "/v1/products", params, idempotency_key=idempotency_key)

    # ---- events and account
    def list_events(self, *, types: Sequence[str], created_gte: int) -> Iterator[dict[str, Any]]:
        names = sorted(set(types))
        for start in range(0, len(names), 20):  # Stripe accepts up to 20 types per call
            yield from self.list_all(
                "/v1/events", {"types": names[start:start + 20], "created": {"gte": int(created_gte)}}
            )

    def retrieve_account(self) -> dict[str, Any]:
        return self.request("GET", "/v1/account")


_client_factory: Callable[[Settings], StripeClient] | None = None


def set_client_factory(factory: Callable[[Settings], StripeClient] | None) -> Callable[[Settings], StripeClient] | None:
    """Replace how `default_client` builds clients (tests install a fake transport)."""
    global _client_factory
    previous, _client_factory = _client_factory, factory
    return previous


def default_client(settings: Settings) -> StripeClient:
    """A client for code paths that are not handed one (CLI commands, account deletion)."""
    if _client_factory is not None:
        return _client_factory(settings)
    return StripeClient(settings)


def _form_body(pairs: Sequence[tuple[str, str]]) -> bytes:
    return str(httpx.QueryParams(list(pairs))).encode("ascii")
