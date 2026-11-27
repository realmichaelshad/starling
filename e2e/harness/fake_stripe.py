"""A local stand-in for Stripe, served over real HTTP: the API, the hosted pages and the webhooks.

The API surface is the in-memory fake the billing workstream already maintains
(`backend/server/tests/billing/fake_stripe.py`); this module wraps it in an HTTP server so a
gateway in another process can use it through `STRIPE_API_BASE`, and adds what a browser
journey needs:

* `GET /c/pay/{session_id}` - the hosted Checkout page, with a button per outcome
  (pay, card declined, leave without paying);
* `GET /p/session/{session_id}` - the Customer Portal page, with the buttons the flow the
  gateway asked for allows (cancel at period end, keep the plan, confirm a plan switch,
  update the payment method);
* every button changes the same state Stripe would change and then delivers the resulting
  events to `POST /webhooks/stripe`, correctly signed, in the order Stripe produced them.

Control plane under `/_control`: `health`, `state`, `clock`, `delivery`, `deliver`, `pending`,
`advance`, `dispute`, `refund`, `delete_customer`, `reset`. Deliveries can be duplicated and
shuffled on demand, which is how the "duplicate and out-of-order changes nothing" journey is
driven.

The fake's clock follows the real one (`now = time.time() + offset`) because the gateway signs
and checks webhook timestamps against the real clock and resolves entitlement periods against
it. `POST /_control/clock` moves the offset when a journey needs a period boundary to fall a
few seconds from now.
"""
from __future__ import annotations

import html
import json
import random
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .paths import add_server_to_path

add_server_to_path()

from starling_server.billing import signatures  # noqa: E402
from tests.billing.fake_stripe import FakeStripe, public  # noqa: E402

WEBHOOK_PATH = "/webhooks/stripe"


class E2EStripe(FakeStripe):
    """The billing fake with a real-time clock and hosted pages that live on this server."""

    def __init__(self, *, api_key: str, base_url: str, pages_base_url: str | None = None,
                 livemode: bool = False) -> None:
        self._offset = 0
        super().__init__(api_key=api_key, livemode=livemode)
        self._offset = 0
        self.base_url = base_url.rstrip("/")
        # Checkout and Portal URLs must be https (the gateway refuses anything else), so the
        # pages live on this server's TLS listener while the API stays on plain HTTP.
        self.pages_base_url = (pages_base_url or base_url).rstrip("/")
        self.page_actions: list[dict[str, Any]] = []

    # ---- clock

    @property
    def now(self) -> int:
        return int(time.time()) + self._offset

    @now.setter
    def now(self, value: int) -> None:
        self._offset = int(value) - int(time.time())

    @property
    def offset(self) -> int:
        return self._offset

    # ---- hosted page URLs point at this server, not at stripe.com

    def _create_session(self, p: dict[str, Any]) -> dict[str, Any]:
        created = super()._create_session(p)
        session = self.sessions[created["id"]]
        session["url"] = f"{self.pages_base_url}/c/pay/{session['id']}"
        return public(session)

    def _create_portal_session(self, p: dict[str, Any]) -> dict[str, Any]:
        created = super()._create_portal_session(p)
        session = self.portal_sessions[created["id"]]
        session["url"] = f"{self.pages_base_url}/p/session/{session['id']}"
        return public(session)

    def invoice_page_url(self, invoice_id: str) -> str:
        return f"{self.pages_base_url}/i/{invoice_id}"


# --------------------------------------------------------------------------- pages


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px system-ui;margin:3rem auto;max-width:36rem}"
        "button{font:inherit;padding:.6rem 1rem;margin:.25rem .4rem .25rem 0}"
        "dl{display:grid;grid-template-columns:auto 1fr;gap:.25rem 1rem}</style></head>"
        f"<body>{body}</body></html>"
    )


def _button(action: str, label: str, test_id: str) -> str:
    return (
        f'<form method="post" action="{html.escape(action)}" style="display:inline">'
        f'<button type="submit" data-testid="{html.escape(test_id)}">{html.escape(label)}</button></form>'
    )


def _rows(pairs: list[tuple[str, str]]) -> str:
    return "<dl>" + "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>" for k, v in pairs) + "</dl>"


# --------------------------------------------------------------------------- the app


def create_app(*, api_key: str, webhook_secret: str, gateway_url: str, base_url: str,
               pages_base_url: str | None = None) -> FastAPI:
    fake = E2EStripe(api_key=api_key, base_url=base_url, pages_base_url=pages_base_url)
    app = FastAPI(title="fake Stripe", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.fake = fake
    app.state.auto_deliver = True
    app.state.duplicate = False
    app.state.shuffle_seed: int | None = None
    app.state.deliveries: list[dict[str, Any]] = []

    async def deliver(*, duplicate: bool | None = None, shuffle_seed: int | None = None) -> list[dict[str, Any]]:
        """Send every queued event to the gateway, signed with the real clock."""
        events = fake.take_events()
        if duplicate if duplicate is not None else app.state.duplicate:
            events = events + [json.loads(json.dumps(event)) for event in events]
        seed = shuffle_seed if shuffle_seed is not None else app.state.shuffle_seed
        if seed is not None:
            random.Random(seed).shuffle(events)
        sent: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            for event in events:
                body = json.dumps(event, separators=(",", ":")).encode("utf-8")
                header = signatures.sign_header(body, webhook_secret, int(time.time()))
                try:
                    response = await client.post(
                        gateway_url.rstrip("/") + WEBHOOK_PATH,
                        content=body,
                        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
                    )
                    status, answer = response.status_code, response.text[:200]
                except httpx.HTTPError as exc:
                    status, answer = 0, f"{type(exc).__name__}"
                record = {"id": event["id"], "type": event["type"], "status": status, "response": answer}
                sent.append(record)
                app.state.deliveries.append(record)
        return sent

    async def settle(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        options = payload or {}
        if not (options.get("deliver", app.state.auto_deliver)):
            return []
        return await deliver(duplicate=options.get("duplicate"), shuffle_seed=options.get("shuffle_seed"))

    # ---- control

    @app.get("/_control/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "now": fake.now, "offset": fake.offset}

    @app.post("/_control/reset")
    async def reset() -> dict[str, Any]:
        fake.take_events()
        app.state.deliveries.clear()
        fake.page_actions.clear()
        app.state.auto_deliver = True
        app.state.duplicate = False
        app.state.shuffle_seed = None
        return {"ok": True}

    @app.post("/_control/clock")
    async def clock(payload: dict[str, Any]) -> dict[str, Any]:
        if "offset_s" in payload:
            fake._offset = int(payload["offset_s"])
        elif "advance_s" in payload:
            fake.advance(int(payload["advance_s"]))
        elif "period_end_in_s" in payload:
            # Put the fake clock where a subscription created now would end `period_end_in_s`
            # from the real clock: the journeys that need a rollover use this.
            from starling_server.billing.entitlements import add_months

            target = int(time.time()) + int(payload["period_end_in_s"])
            fake._offset = add_months(target, -1) - int(time.time())
        return {"ok": True, "now": fake.now, "offset": fake.offset}

    @app.post("/_control/delivery")
    async def delivery(payload: dict[str, Any]) -> dict[str, Any]:
        if "auto" in payload:
            app.state.auto_deliver = bool(payload["auto"])
        if "duplicate" in payload:
            app.state.duplicate = bool(payload["duplicate"])
        if "shuffle_seed" in payload:
            app.state.shuffle_seed = payload["shuffle_seed"]
        return {"auto": app.state.auto_deliver, "duplicate": app.state.duplicate, "shuffle_seed": app.state.shuffle_seed}

    @app.get("/_control/pending")
    async def pending() -> dict[str, Any]:
        return {"events": [{"id": e["id"], "type": e["type"]} for e in fake.outbox]}

    @app.get("/_control/deliveries")
    async def deliveries() -> dict[str, Any]:
        return {"deliveries": app.state.deliveries}

    @app.post("/_control/deliver")
    async def deliver_now(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        options = payload or {}
        sent = await deliver(duplicate=options.get("duplicate"), shuffle_seed=options.get("shuffle_seed"))
        return {"delivered": sent}

    @app.get("/_control/state")
    async def state(customer: str | None = None) -> dict[str, Any]:
        subscriptions = [
            public(sub) for sub in fake.subscriptions.values() if customer is None or sub["customer"] == customer
        ]
        return {
            "now": fake.now,
            "customers": sorted(fake.customers),
            "subscriptions": subscriptions,
            "invoices": [public(inv) for inv in fake.invoices.values() if customer is None or inv["customer"] == customer],
            "charges": [public(ch) for ch in fake.charges.values() if customer is None or ch["customer"] == customer],
            "page_actions": fake.page_actions,
            "creates": dict(fake.creates),
        }

    @app.post("/_control/advance")
    async def advance(payload: dict[str, Any]) -> dict[str, Any]:
        """Renewal: `outcome` is `paid`, `failed` or `requires_action`."""
        result = fake.advance_period(str(payload["subscription"]), str(payload.get("outcome", "paid")))
        return {"result": result, "delivered": await settle(payload)}

    @app.post("/_control/start_subscription")
    async def start_subscription(payload: dict[str, Any]) -> dict[str, Any]:
        """A subscription created outside Starling (the Dashboard), on the fake's current clock.

        Checkout refuses a session whose expiry is far from Stripe's clock, so a journey that
        needs a period boundary a few seconds from now creates the subscription this way after
        moving the clock (`/_control/clock`).
        """
        result = fake.start_subscription(
            str(payload["customer"]), str(payload["price"]), metadata=payload.get("metadata")
        )
        return {"result": result, "delivered": await settle(payload)}

    @app.post("/_control/pay_invoice")
    async def pay_invoice(payload: dict[str, Any]) -> dict[str, Any]:
        charge = fake.pay_invoice(str(payload["invoice"]))
        return {"charge": charge, "delivered": await settle(payload)}

    @app.post("/_control/dispute")
    async def dispute(payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "open"))
        if action == "open":
            dispute_id = fake.open_dispute(str(payload["charge"]))
            return {"dispute": dispute_id, "delivered": await settle(payload)}
        fake.close_dispute(str(payload["dispute"]), str(payload.get("status", "lost")))
        return {"ok": True, "delivered": await settle(payload)}

    @app.post("/_control/refund")
    async def refund(payload: dict[str, Any]) -> dict[str, Any]:
        fake.refund(str(payload["charge"]), payload.get("amount"))
        return {"ok": True, "delivered": await settle(payload)}

    @app.post("/_control/delete_customer")
    async def delete_customer(payload: dict[str, Any]) -> dict[str, Any]:
        fake.delete_customer(str(payload["customer"]))
        return {"ok": True, "delivered": await settle(payload)}

    @app.post("/_control/cancel_now")
    async def cancel_now(payload: dict[str, Any]) -> dict[str, Any]:
        fake.cancel_now(str(payload["subscription"]))
        return {"ok": True, "delivered": await settle(payload)}

    @app.post("/_control/fail_next")
    async def fail_next(payload: dict[str, Any]) -> dict[str, Any]:
        fake.fail_next(
            str(payload.get("method", "POST")),
            str(payload.get("prefix", "/v1/")),
            status=int(payload.get("status", 500)),
            times=int(payload.get("times", 1)),
            error_type=str(payload.get("error_type", "api_error")),
            code=payload.get("code"),
        )
        return {"ok": True}

    # ---- hosted Checkout

    @app.get("/c/pay/{session_id}")
    async def checkout_page(session_id: str) -> Response:
        session = fake.sessions.get(session_id)
        if session is None:
            return _page("Not found", "<h1>No such Checkout Session</h1>")
        price = fake.prices.get(session["_price"], {})
        amount = f"{price.get('unit_amount', 0) / 100:.2f} {str(price.get('currency', '')).upper()}"
        body = (
            "<h1>Checkout (local fake)</h1>"
            + _rows(
                [
                    ("Session", session_id),
                    ("Status", session["status"]),
                    ("Customer", session["customer"]),
                    ("Amount", amount),
                    ("Interval", (price.get("recurring") or {}).get("interval", "")),
                ]
            )
            + "<p>Pick an outcome. Each one changes the subscription the way Stripe would and "
            "delivers the resulting webhooks.</p>"
            + _button(f"/c/pay/{session_id}/pay", "Pay", "checkout-pay")
            + _button(f"/c/pay/{session_id}/decline", "Card declined", "checkout-decline")
            + _button(f"/c/pay/{session_id}/leave", "Leave without paying", "checkout-leave")
        )
        return _page("Checkout", body)

    @app.post("/c/pay/{session_id}/{action}")
    async def checkout_action(session_id: str, action: str) -> Response:
        session = fake.sessions.get(session_id)
        if session is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        params = session["_params"]
        fake.page_actions.append({"page": "checkout", "action": action, "session": session_id, "at": fake.now})
        if action == "leave":
            await settle()
            return RedirectResponse(params["cancel_url"], status_code=303)
        outcome = "paid" if action == "pay" else "declined"
        result = fake.complete_checkout(session_id, outcome)
        await settle()
        target = str(params["success_url"]).replace("{CHECKOUT_SESSION_ID}", session_id)
        return RedirectResponse(target, status_code=303, headers={"X-Fake-Stripe-Result": json.dumps(result)})

    # ---- hosted invoice page (paying a first invoice whose card was declined)

    @app.get("/i/{invoice_id}")
    async def invoice_page(invoice_id: str) -> Response:
        invoice = fake.invoices.get(invoice_id)
        if invoice is None:
            return _page("Not found", "<h1>No such invoice</h1>")
        amount = f"{invoice['amount_due'] / 100:.2f}"
        body = (
            "<h1>Invoice (local fake)</h1>"
            + _rows(
                [
                    ("Invoice", invoice_id),
                    ("Status", invoice["status"]),
                    ("Customer", invoice["customer"]),
                    ("Amount due", amount),
                    ("Reason", invoice["billing_reason"]),
                ]
            )
            + (
                "<p>Pay this invoice with a working card, the way Stripe's hosted invoice page "
                "does. Paying a subscription's first invoice activates that subscription.</p>"
                + _button(f"/i/{invoice_id}/pay", "Pay this invoice", "invoice-pay")
                + _button(f"/i/{invoice_id}/decline", "Card declined again", "invoice-decline")
                if invoice["status"] == "open"
                else (
                    '<p data-testid="invoice-paid">This invoice is paid. Stripe\'s hosted invoice page has no '
                    "way back to the site that sent you here: return to Starling yourself.</p>"
                    if invoice["status"] == "paid"
                    else '<p data-testid="invoice-closed">This invoice needs no payment.</p>'
                )
            )
        )
        return _page("Invoice", body)

    @app.post("/i/{invoice_id}/{action}")
    async def invoice_action(invoice_id: str, action: str) -> Response:
        invoice = fake.invoices.get(invoice_id)
        if invoice is None:
            return JSONResponse({"error": "unknown invoice"}, status_code=404)
        fake.page_actions.append({"page": "invoice", "action": action, "invoice": invoice_id, "at": fake.now})
        if action == "pay" and invoice["status"] == "open":
            fake.pay_invoice(invoice_id)
        await settle()
        # Unlike Checkout and the Customer Portal, Stripe's hosted invoice page takes no return
        # URL. The customer stays on Stripe's page, which now shows the invoice as paid, and finds
        # their own way back. A stand-in that redirected to the site would hide exactly the moment
        # a journey has to prove: that the site is right when the customer comes back by themselves.
        return RedirectResponse(f"/i/{invoice_id}", status_code=303)

    # ---- Customer Portal

    @app.get("/p/session/{portal_id}")
    async def portal_page(portal_id: str) -> Response:
        session = fake.portal_sessions.get(portal_id)
        if session is None:
            return _page("Not found", "<h1>No such portal session</h1>")
        flow = session.get("flow") or {}
        kind = flow.get("type") or "manage"
        subscriptions = [s for s in fake.subscriptions.values() if s["customer"] == session["customer"]]
        live = next((s for s in subscriptions if s["status"] not in ("canceled", "incomplete_expired")), None)
        rows = [("Portal session", portal_id), ("Customer", session["customer"]), ("Flow", kind)]
        if live is not None:
            rows += [
                ("Subscription", live["id"]),
                ("Status", live["status"]),
                ("Price", live["items"]["data"][0]["price"]["id"]),
                ("Cancel at period end", live["cancel_at_period_end"]),
            ]
        buttons = ""
        if kind == "subscription_update_confirm":
            buttons += _button(f"/p/session/{portal_id}/confirm_update", "Confirm the plan switch", "portal-confirm-update")
            buttons += _button(f"/p/session/{portal_id}/confirm_update_declined", "Confirm (card declined)", "portal-confirm-declined")
        if kind in ("manage", "subscription_cancel") and live is not None:
            if live["cancel_at_period_end"]:
                buttons += _button(f"/p/session/{portal_id}/resume", "Keep my plan", "portal-resume")
            else:
                buttons += _button(f"/p/session/{portal_id}/cancel", "Cancel at period end", "portal-cancel")
        if kind in ("manage", "payment_method_update"):
            buttons += _button(f"/p/session/{portal_id}/payment_method", "Save a new payment method", "portal-payment-method")
        buttons += _button(f"/p/session/{portal_id}/back", "Back to Starling", "portal-back")
        return _page("Customer portal", "<h1>Customer portal (local fake)</h1>" + _rows(rows) + buttons)

    @app.post("/p/session/{portal_id}/{action}")
    async def portal_action(portal_id: str, action: str) -> Response:
        session = fake.portal_sessions.get(portal_id)
        if session is None:
            return JSONResponse({"error": "unknown portal session"}, status_code=404)
        flow = session.get("flow") or {}
        after = ((flow.get("after_completion") or {}).get("redirect") or {}).get("return_url")
        target = after or session.get("return_url") or gateway_url
        fake.page_actions.append({"page": "portal", "action": action, "session": portal_id, "at": fake.now})
        live = next(
            (s for s in fake.subscriptions.values()
             if s["customer"] == session["customer"] and s["status"] not in ("canceled", "incomplete_expired")),
            None,
        )
        if action == "cancel" and live is not None:
            fake.set_cancel_at_period_end(live["id"], True)
        elif action == "resume" and live is not None:
            fake.set_cancel_at_period_end(live["id"], False)
        elif action == "confirm_update":
            fake.confirm_portal_upgrade(portal_id, "paid")
        elif action == "confirm_update_declined":
            fake.confirm_portal_upgrade(portal_id, "failed")
        elif action == "payment_method":
            pass  # Stripe emits no subscription event for a saved card
        await settle()
        return RedirectResponse(target, status_code=303)

    # ---- the API itself

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE"])
    async def api(path: str, request: Request) -> Response:
        body = await request.body()
        proxied = httpx.Request(
            request.method,
            httpx.URL(f"http://fake-stripe/v1/{path}").copy_with(query=request.url.query.encode("ascii")),
            headers={key: value for key, value in request.headers.items() if key.lower() != "host"},
            content=body,
        )
        answer = fake.handle(proxied)
        return Response(
            content=answer.content,
            status_code=answer.status_code,
            headers={k: v for k, v in answer.headers.items() if k.lower() not in ("content-length", "content-encoding")},
            media_type="application/json",
        )

    return app
