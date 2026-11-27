"""Driving the fake Stripe pages the way a customer's browser drives the real ones.

`POST /billing/checkout` redirects to a hosted Checkout page; here that page lives on the fake
Stripe server. Pressing one of its buttons changes the subscription exactly as Stripe would
and delivers the resulting webhooks to `POST /webhooks/stripe`, signed. Nothing in these
helpers talks to the gateway's database: the plan is read back from `GET /api/v1/session` and
`/account/billing`, which is what the journeys are checking.
"""
from __future__ import annotations

from typing import Any

import httpx

from e2e.tests.client import Account, Control


def start_checkout(account: Account, plan_code: str) -> str:
    """Press "Choose <plan>" on `/account/billing` and follow the redirect to Stripe."""
    response = account.site.post_form("/billing/checkout", {"plan_code": plan_code})
    assert response.status_code == 303, f"checkout refused: {response.status_code} {response.text[:300]}"
    location = response.headers["location"]
    assert "/c/pay/" in location, location
    return location


def press(url: str, action: str, *, client_ip: str | None = None) -> httpx.Response:
    """Press a button on a hosted Stripe page; the redirect back to Starling is not followed.

    The page is served over TLS with a per-run self-signed certificate (Stripe URLs are
    https and the gateway refuses anything else), so the client does not verify it.
    """
    headers = {"X-Forwarded-For": client_ip} if client_ip else {}
    with httpx.Client(follow_redirects=False, timeout=60.0, trust_env=False, headers=headers,
                      verify=False) as client:
        page = client.get(url)
        assert page.status_code == 200, page.text[:200]
        assert f'data-testid="{action}"' in page.text, f"the page has no {action} button"
        base = url.rstrip("/")
        return client.post(f"{base}/{_action_path(action)}")


_ACTIONS = {
    "checkout-pay": "pay",
    "checkout-decline": "decline",
    "checkout-leave": "leave",
    "portal-cancel": "cancel",
    "portal-resume": "resume",
    "portal-confirm-update": "confirm_update",
    "portal-confirm-declined": "confirm_update_declined",
    "portal-payment-method": "payment_method",
    "portal-back": "back",
}


def _action_path(action: str) -> str:
    assert action in _ACTIONS, f"unknown button {action}"
    return _ACTIONS[action]


def wait_for_plan(account: Account, harness: Control, plan_code: str, *, timeout_s: float = 45.0) -> dict[str, Any]:
    """Wait for the webhooks to be processed and the account to show `plan_code` everywhere."""
    harness.wait_for_events_processed(timeout_s)
    state = harness.wait_for(
        lambda: _state_if(account, plan_code),
        timeout_s,
        f"the account to move to {plan_code}",
    )
    page = account.site.billing_page()
    assert page["plan_code"] == plan_code, page["plan_code"]
    return state


def _state_if(account: Account, plan_code: str) -> dict[str, Any] | None:
    state = account.device.session() if account.device is not None else account.site.billing_status()
    return state if state["plan"]["code"] == plan_code else None


def subscribe(account: Account, harness: Control, plan_code: str) -> dict[str, Any]:
    """The whole happy path: checkout, pay, webhooks, allowance granted."""
    url = start_checkout(account, plan_code)
    answer = press(url, "checkout-pay", client_ip=account.site.client_ip)
    assert answer.status_code == 303
    assert "/billing/return" in answer.headers["location"], answer.headers["location"]
    return wait_for_plan(account, harness, plan_code)


def portal(account: Account, flow: str = "") -> str:
    response = account.site.post_form("/billing/portal", {"flow": flow} if flow else {})
    assert response.status_code == 303, f"portal refused: {response.status_code} {response.text[:300]}"
    location = response.headers["location"]
    assert "/p/session/" in location, location
    return location


def upgrade(account: Account, plan_code: str) -> str:
    response = account.site.post_form("/billing/upgrade", {"plan_code": plan_code})
    assert response.status_code == 303, f"upgrade refused: {response.status_code} {response.text[:300]}"
    location = response.headers["location"]
    assert "/p/session/" in location, location
    return location


def subscription_of(harness: Control, account: Account) -> dict[str, Any]:
    row = harness.one(
        "SELECT * FROM subscriptions WHERE account_id = ? ORDER BY synced_at DESC LIMIT 1", (account.id,)
    )
    assert row is not None, "no subscription is mirrored for this account"
    return row


def customer_of(harness: Control, account: Account) -> str:
    row = harness.one("SELECT customer_id FROM stripe_customers WHERE account_id = ?", (account.id,))
    assert row is not None, "no Stripe customer is recorded for this account"
    return str(row["customer_id"])
