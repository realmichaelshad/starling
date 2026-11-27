"""An in-memory fake of the Stripe API subset Starling uses (no network, no account, no keys).

`FakeStripe.transport` is an `httpx.MockTransport` for `StripeClient`. It checks the headers
the real API needs (Bearer key, pinned `Stripe-Version`, `Idempotency-Key` only on POST),
decodes Stripe's bracket form encoding, replays idempotent POSTs, validates the parameters
Starling relies on, and records every request. Simulation helpers (checkout completion,
renewals, payment failures, portal upgrades, refunds, disputes, customer deletion) change
the state the way Stripe does and queue the resulting events, which tests deliver as signed
webhooks in any order, with duplicates.
"""
from __future__ import annotations

import copy
import json
import random
import re
import threading
from collections import Counter
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl

import httpx

from starling_server.billing import signatures
from starling_server.billing.entitlements import add_months

API_VERSION = "2026-08-26.dahlia"
START = 1_790_000_000
TERMINAL = ("canceled", "incomplete_expired")


class FakeError(Exception):
    def __init__(self, status: int, error_type: str, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status, self.error_type, self.code, self.message = status, error_type, code, message


def bad(message: str, code: str | None = "parameter_invalid") -> FakeError:
    return FakeError(400, "invalid_request_error", code, message)


def missing(what: str, object_id: str) -> FakeError:
    return FakeError(404, "invalid_request_error", "resource_missing", f"No such {what}: '{object_id}'")


def decode_params(pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """`a[b][0][c]=v` pairs into nested dicts and lists."""
    root: dict[str, Any] = {}
    for key, value in pairs:
        parts = re.findall(r"[^\[\]]+", key)
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return _listify(root)


def _listify(node: Any) -> Any:
    if isinstance(node, dict):
        converted = {key: _listify(value) for key, value in node.items()}
        if converted and all(key.isdigit() for key in converted):
            return [converted[key] for key in sorted(converted, key=int)]
        return converted
    return node


def truthy(value: Any) -> bool:
    return value is True or value == "true"


def _coerce_bools(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _coerce_bools(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_coerce_bools(value) for value in node]
    if node in ("true", "false"):
        return node == "true"
    return node


def public(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in obj.items() if not key.startswith("_")}


class FakeStripe:
    def __init__(self, *, api_key: str, livemode: bool = False, account_id: str = "acct_fake0001") -> None:
        self.api_key = api_key
        self.livemode = livemode
        self.account_id = account_id
        self.now = START
        self.lock = threading.RLock()
        self._seq = 0
        self.customers: dict[str, dict[str, Any]] = {}
        self.products: dict[str, dict[str, Any]] = {}
        self.prices: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.schedules: dict[str, dict[str, Any]] = {}
        self.invoices: dict[str, dict[str, Any]] = {}
        self.charges: dict[str, dict[str, Any]] = {}
        self.disputes: dict[str, dict[str, Any]] = {}
        self.invoice_payments: dict[str, dict[str, Any]] = {}
        self.portal_configurations: dict[str, dict[str, Any]] = {}
        self.portal_sessions: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.creates: Counter[str] = Counter()
        self.denied_paths: set[str] = set()
        self.on_session_created: Callable[[dict[str, Any]], None] | None = None
        self._idempotency: dict[str, tuple[tuple[str, str], int, dict[str, Any]]] = {}
        self._failures: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ plumbing
    def new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:08d}"

    def advance(self, seconds: int) -> None:
        self.now += int(seconds)

    def fail_next(self, method: str, path_prefix: str, *, status: int = 500, times: int = 1,
                  error_type: str = "api_error", code: str | None = None, headers: dict[str, str] | None = None) -> None:
        self._failures.append({"method": method, "prefix": path_prefix, "status": status, "times": times,
                               "type": error_type, "code": code, "headers": headers or {}})

    def calls(self, method: str, path_prefix: str = "") -> int:
        return sum(1 for m, p in self.requests if m == method and p.startswith(path_prefix))

    def emit(self, event_type: str, obj: dict[str, Any]) -> dict[str, Any]:
        event = {
            "id": self.new_id("evt"), "object": "event", "api_version": API_VERSION, "created": self.now,
            "livemode": self.livemode, "type": event_type, "data": {"object": public(obj)},
            "pending_webhooks": 1, "request": {"id": None, "idempotency_key": None},
        }
        self.events.append(event)
        self.outbox.append(event)
        return event

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            method, path = request.method, request.url.path
            self.requests.append((method, path))
            if request.headers.get("authorization") != f"Bearer {self.api_key}":
                return self._respond(FakeError(401, "invalid_request_error", None, "Invalid API Key provided"))
            if request.headers.get("stripe-version") != API_VERSION:
                return self._respond(bad("Stripe-Version must be the pinned API version", "api_version_invalid"))
            key = request.headers.get("idempotency-key")
            if method == "POST":
                if not key:
                    return self._respond(bad("the fake requires an Idempotency-Key on every POST", "idempotency_key_missing"))
                params = decode_params(parse_qsl(request.content.decode("ascii"), keep_blank_values=True))
            else:
                if key:
                    return self._respond(bad("Idempotency-Key has no effect on GET or DELETE", "idempotency_key_unexpected"))
                params = decode_params(request.url.params.multi_items())
            for failure in self._failures:
                if failure["times"] > 0 and failure["method"] == method and path.startswith(failure["prefix"]):
                    failure["times"] -= 1
                    return self._respond(FakeError(failure["status"], failure["type"], failure["code"], "injected failure"),
                                         failure["headers"])
            fingerprint = (path, json.dumps(params, sort_keys=True))
            if method == "POST" and key in self._idempotency:
                stored_fingerprint, status, body = self._idempotency[key]
                if stored_fingerprint != fingerprint:
                    return self._respond(FakeError(400, "idempotency_error", None,
                                                   "Keys for idempotent requests can only be used with the same parameters."))
                return httpx.Response(status, json=body, headers={"idempotent-replayed": "true"})
            if path in self.denied_paths:
                return self._respond(FakeError(403, "invalid_request_error", "secret_key_required", "restricted key"))
            try:
                body = self._route(method, path, params)
            except FakeError as exc:
                return self._respond(exc)
            if method == "POST":
                self._idempotency[key] = (fingerprint, 200, body)
            return httpx.Response(200, json=body, headers={"request-id": self.new_id("req")})

    def _respond(self, exc: FakeError, headers: dict[str, str] | None = None) -> httpx.Response:
        body = {"error": {"type": exc.error_type, "code": exc.code, "message": exc.message}}
        return httpx.Response(exc.status, json=body, headers={"request-id": self.new_id("req"), **(headers or {})})

    def _route(self, method: str, path: str, p: dict[str, Any]) -> dict[str, Any]:
        routes: list[tuple[str, str, Callable[..., dict[str, Any]]]] = [
            ("POST", r"/v1/customers", self._create_customer),
            ("GET", r"/v1/customers/(?P<oid>\w+)", self._get_customer),
            ("POST", r"/v1/checkout/sessions", self._create_session),
            ("GET", r"/v1/checkout/sessions", self._list_sessions),
            ("GET", r"/v1/checkout/sessions/(?P<oid>\w+)", lambda p, oid: public(self._get(self.sessions, "checkout.session", oid))),
            ("POST", r"/v1/checkout/sessions/(?P<oid>\w+)/expire", self._expire_session),
            ("POST", r"/v1/billing_portal/sessions", self._create_portal_session),
            ("POST", r"/v1/billing_portal/configurations", self._create_portal_configuration),
            ("GET", r"/v1/billing_portal/configurations", self._list_portal_configurations),
            ("GET", r"/v1/subscriptions", self._list_subscriptions),
            ("GET", r"/v1/subscriptions/(?P<oid>\w+)", self._get_subscription),
            ("POST", r"/v1/subscriptions/(?P<oid>\w+)", self._update_subscription),
            ("DELETE", r"/v1/subscriptions/(?P<oid>\w+)", self._cancel_subscription),
            ("POST", r"/v1/subscription_schedules", self._create_schedule),
            ("GET", r"/v1/subscription_schedules/(?P<oid>\w+)", lambda p, oid: public(self._get(self.schedules, "subscription schedule", oid))),
            ("POST", r"/v1/subscription_schedules/(?P<oid>\w+)", self._update_schedule),
            ("POST", r"/v1/subscription_schedules/(?P<oid>\w+)/release", self._release_schedule),
            ("GET", r"/v1/invoices/(?P<oid>\w+)", lambda p, oid: public(self._get(self.invoices, "invoice", oid))),
            ("GET", r"/v1/invoices", self._list_invoices),
            ("GET", r"/v1/invoice_payments", self._list_invoice_payments),
            ("GET", r"/v1/charges/(?P<oid>\w+)", lambda p, oid: public(self._get(self.charges, "charge", oid))),
            ("GET", r"/v1/disputes", self._list_disputes),
            ("GET", r"/v1/disputes/(?P<oid>\w+)", lambda p, oid: public(self._get(self.disputes, "dispute", oid))),
            ("GET", r"/v1/prices", self._list_prices),
            ("GET", r"/v1/prices/(?P<oid>\w+)", lambda p, oid: public(self._get(self.prices, "price", oid))),
            ("POST", r"/v1/prices", self._create_price),
            ("GET", r"/v1/products", self._list_products),
            ("POST", r"/v1/products", self._create_product),
            ("GET", r"/v1/events", self._list_events),
            ("GET", r"/v1/account", lambda p: {"id": self.account_id, "object": "account", "livemode": self.livemode}),
        ]
        for route_method, pattern, handler in routes:
            if route_method == method:
                match = re.fullmatch(pattern, path)
                if match:
                    return handler(p, **match.groupdict())
        raise FakeError(404, "invalid_request_error", "resource_missing", f"Unrecognized request URL ({method}: {path})")

    def _get(self, table: dict[str, dict[str, Any]], what: str, oid: str) -> dict[str, Any]:
        if oid not in table:
            raise missing(what, oid)
        return table[oid]

    def _page(self, items: list[dict[str, Any]], p: dict[str, Any], url: str) -> dict[str, Any]:
        items = sorted(items, key=lambda item: (item.get("created", 0), item["id"]), reverse=True)
        if p.get("starting_after"):
            ids = [item["id"] for item in items]
            if p["starting_after"] in ids:
                items = items[ids.index(p["starting_after"]) + 1:]
        limit = int(p.get("limit", 10))
        if not 1 <= limit <= 100:
            raise bad("limit must be between 1 and 100")
        return {"object": "list", "url": url, "data": [public(item) for item in items[:limit]], "has_more": len(items) > limit}

    # ------------------------------------------------------------------ customers, products, prices
    def _create_customer(self, p: dict[str, Any]) -> dict[str, Any]:
        customer = {"id": self.new_id("cus"), "object": "customer", "email": p.get("email"),
                    "metadata": p.get("metadata") or {}, "livemode": self.livemode, "created": self.now}
        self.customers[customer["id"]] = customer
        self.creates["customer"] += 1
        return public(customer)

    def _get_customer(self, p: dict[str, Any], oid: str) -> dict[str, Any]:
        customer = self._get(self.customers, "customer", oid)
        if customer.get("_deleted"):
            return {"id": oid, "object": "customer", "deleted": True}
        return public(customer)

    def _create_product(self, p: dict[str, Any]) -> dict[str, Any]:
        if not p.get("name"):
            raise bad("Missing required param: name.", "parameter_missing")
        product = {"id": self.new_id("prod"), "object": "product", "name": p["name"], "active": True,
                   "metadata": p.get("metadata") or {}, "tax_code": p.get("tax_code"), "livemode": self.livemode,
                   "created": self.now}
        self.products[product["id"]] = product
        self.creates["product"] += 1
        return public(product)

    def _list_products(self, p: dict[str, Any]) -> dict[str, Any]:
        items = [x for x in self.products.values() if "active" not in p or x["active"] == truthy(p["active"])]
        return self._page(items, p, "/v1/products")

    def add_price(self, *, product: str | None = None, unit_amount: int, currency: str = "usd", interval: str = "month",
                  lookup_key: str | None = None, active: bool = True) -> dict[str, Any]:
        """Create a price directly (as if made in the Dashboard)."""
        with self.lock:
            if product is None:
                product = self._create_product({"name": "Dashboard product"})["id"]
            price = {"id": self.new_id("price"), "object": "price", "active": active, "currency": currency,
                     "unit_amount": unit_amount, "recurring": {"interval": interval, "interval_count": 1},
                     "lookup_key": lookup_key, "product": product, "livemode": self.livemode, "metadata": {},
                     "nickname": None, "type": "recurring", "tax_behavior": "unspecified", "created": self.now}
            if lookup_key:
                for other in self.prices.values():
                    if other.get("lookup_key") == lookup_key:
                        other["lookup_key"] = None
            self.prices[price["id"]] = price
            return public(price)

    def _create_price(self, p: dict[str, Any]) -> dict[str, Any]:
        for name in ("currency", "unit_amount", "product"):
            if name not in p:
                raise bad(f"Missing required param: {name}.", "parameter_missing")
        if p["product"] not in self.products:
            raise missing("product", p["product"])
        recurring = p.get("recurring") or {}
        if recurring.get("interval") not in ("day", "week", "month", "year"):
            raise bad("recurring[interval] is required for a recurring price")
        lookup_key = p.get("lookup_key")
        holder = next((x for x in self.prices.values() if lookup_key and x.get("lookup_key") == lookup_key), None)
        if holder is not None:
            if not truthy(p.get("transfer_lookup_key")):
                raise bad(f"A price ({holder['id']}) already uses that lookup key.", "lookup_key_in_use")
            holder["lookup_key"] = None
        price = {"id": self.new_id("price"), "object": "price", "active": True, "currency": p["currency"],
                 "unit_amount": int(p["unit_amount"]), "product": p["product"], "lookup_key": lookup_key,
                 "recurring": {"interval": recurring["interval"], "interval_count": int(recurring.get("interval_count", 1))},
                 "livemode": self.livemode, "metadata": p.get("metadata") or {}, "nickname": p.get("nickname"),
                 "type": "recurring", "tax_behavior": p.get("tax_behavior", "unspecified"), "created": self.now}
        self.prices[price["id"]] = price
        self.creates["price"] += 1
        return public(price)

    def _list_prices(self, p: dict[str, Any]) -> dict[str, Any]:
        keys = p.get("lookup_keys")
        if keys is not None and (not isinstance(keys, list) or len(keys) > 10):
            raise bad("lookup_keys accepts up to 10 values")
        items = [
            x for x in self.prices.values()
            if (keys is None or x.get("lookup_key") in keys) and ("active" not in p or x["active"] == truthy(p["active"]))
        ]
        return self._page(items, p, "/v1/prices")

    # ------------------------------------------------------------------ checkout
    def _create_session(self, p: dict[str, Any]) -> dict[str, Any]:
        if p.get("mode") != "subscription":
            raise bad("mode must be subscription for Starling")
        items = p.get("line_items") or []
        if len(items) != 1 or items[0].get("price") not in self.prices or items[0].get("quantity") != "1":
            raise bad("exactly one line item with a known price and quantity 1 is expected")
        if not self.prices[items[0]["price"]]["active"]:
            raise bad("The price specified is inactive.")
        customer = self.customers.get(p.get("customer") or "")
        if customer is None or customer.get("_deleted"):
            raise missing("customer", str(p.get("customer")))
        if "{CHECKOUT_SESSION_ID}" not in (p.get("success_url") or "") or not p.get("cancel_url"):
            raise bad("success_url must contain {CHECKOUT_SESSION_ID} and cancel_url is required")
        expires_at = int(p.get("expires_at", self.now + 86400))
        if not self.now + 1800 <= expires_at <= self.now + 86400:
            raise bad("expires_at must be between 30 minutes and 24 hours from now")
        restrictions = ((p.get("payment_method_options") or {}).get("card") or {}).get("restrictions") or {}
        blocked = restrictions.get("funding_types_blocked") or []
        if not set(blocked) <= {"credit", "debit", "prepaid"}:
            raise bad("invalid funding_types_blocked value")
        if truthy((p.get("automatic_tax") or {}).get("enabled")) and (p.get("customer_update") or {}).get("address") != "auto":
            raise bad("automatic tax with an existing customer needs customer_update[address]=auto here")
        session_id = self.new_id("cs_test")
        session = {"id": session_id, "object": "checkout.session", "status": "open", "customer": customer["id"],
                   "client_reference_id": p.get("client_reference_id"), "metadata": p.get("metadata") or {},
                   "mode": "subscription", "subscription": None, "url": f"https://checkout.stripe.com/c/pay/{session_id}",
                   "expires_at": expires_at, "livemode": self.livemode, "created": self.now, "payment_status": "unpaid",
                   "allow_promotion_codes": truthy(p.get("allow_promotion_codes")),
                   "_price": items[0]["price"], "_subscription_metadata": (p.get("subscription_data") or {}).get("metadata") or {},
                   "_params": p}
        self.sessions[session_id] = session
        self.creates["checkout.session"] += 1
        if self.on_session_created is not None:
            self.on_session_created(session)
        return public(session)

    def _list_sessions(self, p: dict[str, Any]) -> dict[str, Any]:
        items = [s for s in self.sessions.values()
                 if (not p.get("customer") or s["customer"] == p["customer"]) and (not p.get("status") or s["status"] == p["status"])]
        return self._page(items, p, "/v1/checkout/sessions")

    def _expire_session(self, p: dict[str, Any], oid: str) -> dict[str, Any]:
        session = self._get(self.sessions, "checkout.session", oid)
        if session["status"] != "open":
            raise bad(f"This Checkout Session is already {session['status']}.", "checkout_session_not_open")
        session["status"] = "expired"
        self.emit("checkout.session.expired", session)
        return public(session)

    # ------------------------------------------------------------------ customer portal
    def _create_portal_configuration(self, p: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(p.get("features"), dict):
            raise bad("Missing required param: features.", "parameter_missing")
        features = _coerce_bools(p["features"])
        update = features.get("subscription_update") or {}
        if update.get("enabled") is True:
            if not update.get("products"):
                raise bad("features[subscription_update][products] is required when updates are enabled")
            if update.get("proration_behavior") not in (None, "none", "create_prorations", "always_invoice"):
                raise bad("invalid proration_behavior")
            for product in update["products"]:
                for price_id in product.get("prices") or []:
                    if self.prices.get(price_id, {}).get("product") != product.get("product"):
                        raise bad(f"price {price_id} does not belong to product {product.get('product')}")
        if (features.get("subscription_cancel") or {}).get("mode") not in (None, "at_period_end", "immediately"):
            raise bad("invalid subscription_cancel mode")
        allowed = (features.get("customer_update") or {}).get("allowed_updates") or []
        if not set(allowed) <= {"address", "email", "name", "phone", "shipping", "tax_id"}:
            raise bad("invalid allowed_updates value")
        config = {"id": self.new_id("bpc"), "object": "billing_portal.configuration", "active": True, "is_default": False,
                  "features": features, "metadata": p.get("metadata") or {}, "business_profile": p.get("business_profile") or {},
                  "default_return_url": p.get("default_return_url"), "name": p.get("name"), "livemode": self.livemode,
                  "created": self.now}
        self.portal_configurations[config["id"]] = config
        self.creates["portal_configuration"] += 1
        return public(config)

    def _list_portal_configurations(self, p: dict[str, Any]) -> dict[str, Any]:
        items = [c for c in self.portal_configurations.values() if "active" not in p or c["active"] == truthy(p["active"])]
        return self._page(items, p, "/v1/billing_portal/configurations")

    def _create_portal_session(self, p: dict[str, Any]) -> dict[str, Any]:
        customer = self.customers.get(p.get("customer") or "")
        if customer is None or customer.get("_deleted"):
            raise missing("customer", str(p.get("customer")))
        config_id = p.get("configuration")
        config = self.portal_configurations.get(config_id or "")
        if config_id and (config is None or not config["active"]):
            raise missing("billing portal configuration", config_id)
        flow = p.get("flow_data")
        features = (config or {}).get("features") or {}
        if flow and flow.get("type") == "payment_method_update":
            if (features.get("payment_method_update") or {}).get("enabled") is not True:
                raise bad("payment method updates are disabled in this configuration")
        elif flow and flow.get("type") == "subscription_cancel":
            target = self.subscriptions.get((flow.get("subscription_cancel") or {}).get("subscription") or "")
            if target is None or target["customer"] != customer["id"]:
                raise missing("subscription", str((flow.get("subscription_cancel") or {}).get("subscription")))
            if (features.get("subscription_cancel") or {}).get("enabled") is not True:
                raise bad("cancellation is disabled in this configuration")
        elif flow:
            if flow.get("type") != "subscription_update_confirm":
                raise bad("unexpected flow type")
            confirm = flow.get("subscription_update_confirm") or {}
            subscription = self.subscriptions.get(confirm.get("subscription") or "")
            if subscription is None or subscription["customer"] != customer["id"]:
                raise missing("subscription", str(confirm.get("subscription")))
            items = confirm.get("items") or []
            if len(items) != 1 or items[0].get("id") != subscription["items"]["data"][0]["id"]:
                raise bad("items[0][id] must be the subscription's item")
            update = ((config or {}).get("features") or {}).get("subscription_update") or {}
            offered = {price for product in update.get("products") or [] for price in product.get("prices") or []}
            if update.get("enabled") is not True or items[0].get("price") not in offered:
                raise bad("The price must be included in the configuration's features.subscription_update.products.")
        session_id = self.new_id("bps")
        session = {"id": session_id, "object": "billing_portal.session", "customer": customer["id"],
                   "configuration": config_id or "bpc_dashboard_default", "flow": flow, "return_url": p.get("return_url"),
                   "url": f"https://billing.stripe.com/p/session/test_{session_id}", "livemode": self.livemode, "created": self.now}
        self.portal_sessions[session_id] = session
        return public(session)

    # ------------------------------------------------------------------ subscriptions
    def _get_subscription(self, p: dict[str, Any], oid: str) -> dict[str, Any]:
        subscription = self._get(self.subscriptions, "subscription", oid)
        view = public(subscription)
        if "latest_invoice" in (p.get("expand") or []) and subscription.get("latest_invoice") in self.invoices:
            view["latest_invoice"] = public(self.invoices[subscription["latest_invoice"]])
        return view

    def _list_subscriptions(self, p: dict[str, Any]) -> dict[str, Any]:
        status = p.get("status")
        items = [
            s for s in self.subscriptions.values()
            if (not p.get("customer") or s["customer"] == p["customer"])
            and (status == "all" or (status is None and s["status"] != "canceled") or s["status"] == status)
        ]
        page = self._page(items, p, "/v1/subscriptions")
        if "data.latest_invoice" in (p.get("expand") or []):
            for view in page["data"]:
                if view.get("latest_invoice") in self.invoices:
                    view["latest_invoice"] = public(self.invoices[view["latest_invoice"]])
        return page

    def _update_subscription(self, p: dict[str, Any], oid: str) -> dict[str, Any]:
        subscription = self._get(self.subscriptions, "subscription", oid)
        if subscription["status"] in TERMINAL:
            raise bad("A canceled subscription can only update its cancellation_details and metadata.")
        if set(p) - {"cancel_at_period_end", "metadata"}:
            raise bad("the fake supports only cancel_at_period_end and metadata updates")
        if "cancel_at_period_end" in p:
            subscription["cancel_at_period_end"] = truthy(p["cancel_at_period_end"])
        subscription["metadata"].update(p.get("metadata") or {})
        self.emit("customer.subscription.updated", subscription)
        return public(subscription)

    def _cancel_subscription(self, p: dict[str, Any], oid: str) -> dict[str, Any]:
        subscription = self._get(self.subscriptions, "subscription", oid)
        if subscription["status"] in TERMINAL:
            raise bad("This subscription is already canceled.")
        self._end_subscription(subscription, prorate=truthy(p.get("prorate")), invoice_now=truthy(p.get("invoice_now")))
        return public(subscription)

    def _end_subscription(self, subscription: dict[str, Any], **cancel: Any) -> None:
        subscription.update(status="canceled", canceled_at=self.now, ended_at=self.now, _cancel=cancel)
        schedule = self.schedules.get(subscription.get("schedule") or "")
        if schedule is not None and schedule["status"] in ("not_started", "active"):
            schedule["status"] = "canceled"
            self.emit("subscription_schedule.canceled", schedule)
        subscription["schedule"] = None
        self.emit("customer.subscription.deleted", subscription)

    # ------------------------------------------------------------------ subscription schedules
    def _create_schedule(self, p: dict[str, Any]) -> dict[str, Any]:
        if set(p) != {"from_subscription"}:
            raise bad("from_subscription cannot be combined with other parameters")
        subscription = self._get(self.subscriptions, "subscription", p["from_subscription"])
        if subscription.get("schedule"):
            raise bad("This subscription is already attached to a schedule.")
        item = subscription["items"]["data"][0]
        schedule_id = self.new_id("sub_sched")
        bounds = {"start_date": item["current_period_start"], "end_date": item["current_period_end"]}
        schedule = {"id": schedule_id, "object": "subscription_schedule", "customer": subscription["customer"],
                    "subscription": subscription["id"], "released_subscription": None, "status": "active",
                    "end_behavior": "release", "metadata": {}, "current_phase": dict(bounds),
                    "phases": [{**bounds, "items": [{"price": item["price"]["id"], "quantity": 1}],
                                "proration_behavior": "create_prorations", "discounts": []}],
                    "livemode": self.livemode, "created": self.now}
        self.schedules[schedule_id] = schedule
        subscription["schedule"] = schedule_id
        self.creates["subscription_schedule"] += 1
        self.emit("customer.subscription.updated", subscription)
        return public(schedule)

    def _update_schedule(self, p: dict[str, Any], oid: str) -> dict[str, Any]:
        schedule = self._get(self.schedules, "subscription schedule", oid)
        if schedule["status"] not in ("not_started", "active"):
            raise bad("You cannot update a subscription schedule that is released or canceled.")
        if "phases" in p:
            phases, start = [], None
            for index, raw in enumerate(p["phases"]):
                items = raw.get("items") or []
                if not items or any(item.get("price") not in self.prices for item in items):
                    raise bad("each phase needs items with known prices")
                if index == 0:
                    if "start_date" not in raw:
                        raise bad("phases[0][start_date] is required", "parameter_missing")
                    start = int(raw["start_date"])
                    if start != schedule["current_phase"]["start_date"]:
                        raise bad("You can not modify the start date of the current phase.")
                elif "start_date" in raw and int(raw["start_date"]) != start:
                    raise bad("a phase must start where the previous one ends")
                if ("end_date" in raw) == ("duration" in raw):
                    raise bad("Exactly one of end_date or duration is allowed per phase.")
                if "end_date" in raw:
                    end = int(raw["end_date"])
                else:
                    duration = raw["duration"]
                    if duration.get("interval") != "month":
                        raise bad("the fake supports monthly durations only")
                    end = add_months(start, int(duration.get("interval_count", 1)))
                if end <= start:
                    raise bad("a phase must end after it starts")
                phases.append({"start_date": start, "end_date": end,
                               "items": [{"price": item["price"], "quantity": int(item.get("quantity", 1))} for item in items],
                               "proration_behavior": raw.get("proration_behavior", "create_prorations"),
                               "discounts": raw.get("discounts") or []})
                start = end
            schedule["phases"] = phases
            schedule["current_phase"] = {"start_date": phases[0]["start_date"], "end_date": phases[0]["end_date"]}
        if "end_behavior" in p:
            if p["end_behavior"] not in ("release", "cancel"):
                raise bad("invalid end_behavior")
            schedule["end_behavior"] = p["end_behavior"]
        schedule["metadata"].update(p.get("metadata") or {})
        return public(schedule)

    def _release_schedule(self, p: dict[str, Any], oid: str) -> dict[str, Any]:
        schedule = self._get(self.schedules, "subscription schedule", oid)
        if schedule["status"] not in ("not_started", "active"):
            raise bad("A schedule can only be released if its status is not_started or active.")
        self._release(schedule)
        return public(schedule)

    def _release(self, schedule: dict[str, Any]) -> None:
        subscription = self.subscriptions.get(schedule.get("subscription") or "")
        schedule.update(status="released", released_subscription=schedule.get("subscription"), subscription=None,
                        released_at=self.now)
        self.emit("subscription_schedule.released", schedule)
        if subscription is not None:
            subscription["schedule"] = None
            self.emit("customer.subscription.updated", subscription)

    # ------------------------------------------------------------------ invoice payments and events
    def _list_invoices(self, p: dict[str, Any]) -> dict[str, Any]:
        gte = int((p.get("created") or {}).get("gte") or 0)
        items = [
            invoice for invoice in self.invoices.values()
            if (not p.get("subscription") or invoice["parent"]["subscription_details"]["subscription"] == p["subscription"])
            and (not p.get("status") or invoice["status"] == p["status"])
            and invoice["created"] >= gte
        ]
        return self._page(items, p, "/v1/invoices")

    def _list_invoice_payments(self, p: dict[str, Any]) -> dict[str, Any]:
        payment = p.get("payment") or {}
        if payment and payment.get("type") != "payment_intent":
            raise bad("payment[type] must be payment_intent")
        items = [
            x for x in self.invoice_payments.values()
            if (not p.get("invoice") or x["invoice"] == p["invoice"])
            and (not payment.get("payment_intent") or x["payment"]["payment_intent"] == payment["payment_intent"])
        ]
        return self._page(items, p, "/v1/invoice_payments")

    def _list_disputes(self, p: dict[str, Any]) -> dict[str, Any]:
        items = [d for d in self.disputes.values() if not p.get("charge") or d["charge"] == p["charge"]]
        return self._page(items, p, "/v1/disputes")

    def _list_events(self, p: dict[str, Any]) -> dict[str, Any]:
        types = p.get("types")
        if types is not None and len(types) > 20:
            raise bad("types accepts up to 20 values")
        gte = int((p.get("created") or {}).get("gte") or 0)
        items = [e for e in self.events if (types is None or e["type"] in types) and e["created"] >= gte]
        return self._page(items, p, "/v1/events")

    # ------------------------------------------------------------------ simulation: what Stripe does by itself
    def _new_subscription(self, customer_id: str, price_id: str, *, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        subscription_id = self.new_id("sub")
        item = {"id": self.new_id("si"), "object": "subscription_item", "price": public(self.prices[price_id]), "quantity": 1,
                "current_period_start": self.now, "current_period_end": add_months(self.now, 1), "subscription": subscription_id}
        subscription = {"id": subscription_id, "object": "subscription", "customer": customer_id, "status": status,
                        "items": {"object": "list", "data": [item], "has_more": False}, "cancel_at_period_end": False,
                        "cancel_at": None, "canceled_at": None, "ended_at": None, "schedule": None, "pending_update": None,
                        "latest_invoice": None, "livemode": self.livemode, "metadata": dict(metadata or {}),
                        "created": self.now, "start_date": self.now}
        self.subscriptions[subscription_id] = subscription
        return subscription

    def _line(self, subscription: dict[str, Any], price: dict[str, Any], start: int, end: int, amount: int, proration: bool = False) -> dict[str, Any]:
        return {"id": self.new_id("il"), "object": "line_item", "amount": int(amount), "currency": price["currency"],
                "period": {"start": int(start), "end": int(end)},
                "parent": {"type": "subscription_item_details", "subscription_item_details": {
                    "subscription": subscription["id"], "subscription_item": subscription["items"]["data"][0]["id"],
                    "proration": proration}},
                "pricing": {"type": "price_details", "price_details": {"price": price["id"], "product": price["product"]}},
                "quantity": 1}

    def invoice_page_url(self, invoice_id: str) -> str:
        """Stripe's hosted page for paying an open invoice (the e2e fake serves its own)."""
        return f"https://invoice.stripe.com/i/{invoice_id}"

    def _new_invoice(self, subscription: dict[str, Any], reason: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        invoice_id = self.new_id("in")
        invoice = {"id": invoice_id, "object": "invoice", "customer": subscription["customer"], "status": "open",
                   "hosted_invoice_url": self.invoice_page_url(invoice_id),
                   "billing_reason": reason,
                   "parent": {"type": "subscription_details", "subscription_details": {
                       "subscription": subscription["id"], "metadata": dict(subscription["metadata"])}},
                   "lines": {"object": "list", "data": lines, "has_more": False},
                   "amount_due": max(0, sum(line["amount"] for line in lines)), "amount_paid": 0,
                   "livemode": self.livemode, "created": self.now, "status_transitions": {"paid_at": None}}
        self.invoices[invoice["id"]] = invoice
        subscription["latest_invoice"] = invoice["id"]
        return invoice

    def _collect(self, invoice: dict[str, Any]) -> dict[str, Any]:
        payment_intent = self.new_id("pi")
        charge = {"id": self.new_id("ch"), "object": "charge", "customer": invoice["customer"], "amount": invoice["amount_due"],
                  "amount_refunded": 0, "refunded": False, "disputed": False, "payment_intent": payment_intent,
                  "livemode": self.livemode, "status": "succeeded", "created": self.now}
        self.charges[charge["id"]] = charge
        payment = {"id": self.new_id("inpay"), "object": "invoice_payment", "invoice": invoice["id"],
                   "payment": {"type": "payment_intent", "payment_intent": payment_intent}, "status": "paid",
                   "livemode": self.livemode, "created": self.now}
        self.invoice_payments[payment["id"]] = payment
        invoice.update(status="paid", amount_paid=invoice["amount_due"], _charge=charge["id"])
        invoice["status_transitions"]["paid_at"] = self.now
        self.emit("invoice.paid", invoice)
        return charge

    def _subscription_of(self, invoice: dict[str, Any]) -> dict[str, Any]:
        return self.subscriptions[invoice["parent"]["subscription_details"]["subscription"]]

    def complete_checkout(self, session_id: str, outcome: str = "paid") -> dict[str, Any]:
        """The customer pays on the Checkout page (`paid`, `requires_action` or `declined`)."""
        with self.lock:
            session = self.sessions[session_id]
            if session["status"] != "open" or self.now >= session["expires_at"]:
                raise RuntimeError(f"checkout session {session_id} cannot be completed: {session['status']}")
            subscription = self._new_subscription(session["customer"], session["_price"],
                                                  status="active" if outcome == "paid" else "incomplete",
                                                  metadata=session["_subscription_metadata"])
            price = self.prices[session["_price"]]
            item = subscription["items"]["data"][0]
            invoice = self._new_invoice(subscription, "subscription_create", [
                self._line(subscription, price, item["current_period_start"], item["current_period_end"], price["unit_amount"])])
            self.emit("customer.subscription.created", subscription)
            charge = self._collect(invoice) if outcome == "paid" else None
            if charge is None:
                self.emit("invoice.payment_action_required" if outcome == "requires_action" else "invoice.payment_failed", invoice)
            session.update(status="complete", subscription=subscription["id"], payment_status="paid" if charge else "unpaid")
            self.emit("checkout.session.completed", session)
            return {"subscription": subscription["id"], "invoice": invoice["id"], "charge": charge["id"] if charge else None,
                    "customer": session["customer"]}

    def start_subscription(self, customer_id: str, price_id: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """A paid subscription created outside Starling (for example in the Dashboard)."""
        with self.lock:
            subscription = self._new_subscription(customer_id, price_id, status="active", metadata=metadata)
            price, item = self.prices[price_id], subscription["items"]["data"][0]
            invoice = self._new_invoice(subscription, "subscription_create", [
                self._line(subscription, price, item["current_period_start"], item["current_period_end"], price["unit_amount"])])
            self.emit("customer.subscription.created", subscription)
            charge = self._collect(invoice)
            return {"subscription": subscription["id"], "invoice": invoice["id"], "charge": charge["id"]}

    def pay_invoice(self, invoice_id: str) -> str:
        """A later successful payment (3DS completed, card updated, retry succeeded)."""
        with self.lock:
            invoice = self.invoices[invoice_id]
            if invoice["status"] != "open":
                raise RuntimeError(f"invoice {invoice_id} is {invoice['status']}")
            charge = self._collect(invoice)
            subscription = self._subscription_of(invoice)
            if subscription.get("pending_update"):
                subscription["items"]["data"][0]["price"] = public(self.prices[subscription["_pending_price"]])
                subscription["pending_update"] = None
                self.emit("customer.subscription.pending_update_applied", subscription)
            if subscription["status"] in ("incomplete", "past_due", "unpaid"):
                subscription["status"] = "active"
                self.emit("customer.subscription.updated", subscription)
            return charge["id"]

    def expire_incomplete(self, subscription_id: str) -> None:
        with self.lock:
            subscription = self.subscriptions[subscription_id]
            subscription.update(status="incomplete_expired", ended_at=self.now)
            invoice = self.invoices.get(subscription.get("latest_invoice") or "")
            if invoice is not None and invoice["status"] == "open":
                invoice["status"] = "void"
            self.emit("customer.subscription.updated", subscription)

    def set_cancel_at_period_end(self, subscription_id: str, value: bool = True) -> None:
        with self.lock:
            subscription = self.subscriptions[subscription_id]
            subscription.update(cancel_at_period_end=value, canceled_at=self.now if value else None)
            self.emit("customer.subscription.updated", subscription)

    def cancel_now(self, subscription_id: str) -> None:
        with self.lock:
            self._end_subscription(self.subscriptions[subscription_id])

    def advance_period(self, subscription_id: str, outcome: str = "paid") -> dict[str, Any]:
        """Move time to the period end: cancel if scheduled, apply schedule phases, invoice the new period."""
        with self.lock:
            subscription = self.subscriptions[subscription_id]
            item = subscription["items"]["data"][0]
            end = item["current_period_end"]
            self.now = max(self.now, end)
            if subscription["cancel_at_period_end"]:
                subscription.update(status="canceled", ended_at=end)
                self.emit("customer.subscription.deleted", subscription)
                return {}
            schedule = self.schedules.get(subscription.get("schedule") or "")
            if schedule is not None and schedule["status"] == "active":
                upcoming = next((phase for phase in schedule["phases"] if phase["start_date"] == end), None)
                if upcoming is not None:
                    item["price"] = public(self.prices[upcoming["items"][0]["price"]])
                    schedule["current_phase"] = {"start_date": upcoming["start_date"], "end_date": upcoming["end_date"]}
                elif schedule["phases"] and schedule["phases"][-1]["end_date"] <= end:
                    self._release(schedule)
            item["current_period_start"], item["current_period_end"] = end, add_months(end, 1)
            price = self.prices[item["price"]["id"]]
            invoice = self._new_invoice(subscription, "subscription_cycle", [
                self._line(subscription, price, end, item["current_period_end"], price["unit_amount"])])
            if outcome == "paid":
                self.emit("customer.subscription.updated", subscription)
                return {"invoice": invoice["id"], "charge": self._collect(invoice)["id"]}
            subscription["status"] = "past_due"
            self.emit("customer.subscription.updated", subscription)
            self.emit("invoice.payment_action_required" if outcome == "requires_action" else "invoice.payment_failed", invoice)
            return {"invoice": invoice["id"], "charge": None}

    def confirm_portal_upgrade(self, portal_session_id: str, outcome: str = "paid", pending: bool = False) -> dict[str, Any]:
        """The customer confirms the portal flow; the proration is invoiced at once (always_invoice)."""
        with self.lock:
            confirm = self.portal_sessions[portal_session_id]["flow"]["subscription_update_confirm"]
            subscription = self.subscriptions[confirm["subscription"]]
            item = subscription["items"]["data"][0]
            old, new = self.prices[item["price"]["id"]], self.prices[confirm["items"][0]["price"]]
            start, end = item["current_period_start"], item["current_period_end"]
            remaining, length = max(0, end - self.now), max(1, end - start)
            lines = [self._line(subscription, old, self.now, end, -(old["unit_amount"] * remaining // length), proration=True),
                     self._line(subscription, new, self.now, end, new["unit_amount"] * remaining // length, proration=True)]
            if pending:
                subscription["pending_update"] = {"expires_at": self.now + 23 * 3600,
                                                  "subscription_items": [{"id": item["id"], "price": new["id"]}]}
                subscription["_pending_price"] = new["id"]
            else:
                item["price"] = public(new)
            invoice = self._new_invoice(subscription, "subscription_update", lines)
            self.emit("customer.subscription.updated", subscription)
            if outcome == "paid":
                charge = self._collect(invoice)
                if pending:
                    item["price"] = public(new)
                    subscription["pending_update"] = None
                    self.emit("customer.subscription.pending_update_applied", subscription)
                return {"invoice": invoice["id"], "charge": charge["id"]}
            self.emit("invoice.payment_failed", invoice)
            if not pending:
                subscription["status"] = "past_due"
                self.emit("customer.subscription.updated", subscription)
            return {"invoice": invoice["id"], "charge": None}

    def refund(self, charge_id: str, amount: int | None = None) -> None:
        with self.lock:
            charge = self.charges[charge_id]
            refunded = charge["amount"] if amount is None else int(amount)
            charge["amount_refunded"] = min(charge["amount"], charge["amount_refunded"] + refunded)
            charge["refunded"] = charge["amount_refunded"] >= charge["amount"]
            self.emit("charge.refunded", charge)

    def open_dispute(self, charge_id: str) -> str:
        with self.lock:
            charge = self.charges[charge_id]
            dispute = {"id": self.new_id("du"), "object": "dispute", "charge": charge_id, "payment_intent": charge["payment_intent"],
                       "amount": charge["amount"], "status": "needs_response", "livemode": self.livemode, "created": self.now}
            self.disputes[dispute["id"]] = dispute
            charge["disputed"] = True
            self.emit("charge.dispute.created", dispute)
            return dispute["id"]

    def close_dispute(self, dispute_id: str, status: str) -> None:
        with self.lock:
            dispute = self.disputes[dispute_id]
            dispute["status"] = status
            self.emit("charge.dispute.closed", dispute)

    def delete_customer(self, customer_id: str) -> None:
        with self.lock:
            customer = self.customers[customer_id]
            for subscription in list(self.subscriptions.values()):
                if subscription["customer"] == customer_id and subscription["status"] not in TERMINAL:
                    self._end_subscription(subscription)
            customer["_deleted"] = True
            self.emit("customer.deleted", {"id": customer_id, "object": "customer", "email": customer.get("email"),
                                           "livemode": self.livemode})

    # ------------------------------------------------------------------ webhook deliveries
    def take_events(self) -> list[dict[str, Any]]:
        with self.lock:
            events, self.outbox = self.outbox, []
            return events

    def delivery(self, event: dict[str, Any], secrets: str | list[str], timestamp: int | None = None) -> tuple[bytes, str]:
        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        return body, signatures.sign_header(body, secrets, self.now if timestamp is None else timestamp)

    def deliveries(self, secrets: str | list[str], *, shuffle_seed: int | None = None, duplicate: bool = False,
                   timestamp: int | None = None) -> list[tuple[bytes, str]]:
        events = self.take_events()
        if duplicate:
            events = events + [copy.deepcopy(event) for event in events]
        if shuffle_seed is not None:
            random.Random(shuffle_seed).shuffle(events)
        return [self.delivery(event, secrets, timestamp) for event in events]
