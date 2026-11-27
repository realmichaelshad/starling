"""Seeded random event storms against the processor, checking money safety after every step.

A run drives the fake Stripe through long sequences of plausible operations for several
accounts, delivering the resulting webhooks shuffled, duplicated and sometimes dropped, and
after every single step asserts the invariants that protect the money:

* **owner safety** (checked after every step, whatever was delivered)
  - no period row holds more allowance than its own plan allows;
  - a period nobody paid for holds at most the documented 10% carry-over;
  - every granted period names a real, paid invoice of its own subscription;
  - no invoice ever grants twice, and no subscription has more granted periods than it has
    paid period invoices.
* **customer liveness** (checked once everything has been delivered and reconciled)
  - a paid, live subscription entitles its plan;
  - an account with nothing entitling is on the free plan;
  - an open dispute suspends the account.

`STARLING_BILLING_SOAK=1` runs many more seeds and steps; the default set is small enough
for the ordinary test run.
"""
from __future__ import annotations

import os
import random

import pytest

from starling_server.billing import entitlements, mirror, processor, reconcile, service, webhooks

from .conftest import DAY, WEBHOOK_SECRET, make_account
from .fake_stripe import START

SOAK = os.environ.get("STARLING_BILLING_SOAK") == "1"
SEEDS = range(1, int(os.environ.get("STARLING_BILLING_SOAK_SEEDS", "25")) + 1) if SOAK else (1, 2, 3, 4, 5)
STEPS = int(os.environ.get("STARLING_BILLING_SOAK_STEPS", "120")) if SOAK else 40
ACCOUNTS = 3


class Storm:
    """One deterministic run: a few accounts, a random sequence of Stripe-plausible events."""

    def __init__(self, billing, seed: int) -> None:
        self.billing = billing
        self.random = random.Random(seed)
        self.accounts = [make_account(billing.db) for _ in range(ACCOUNTS)]
        self.gone: set[str] = set()  # accounts whose Stripe customer was deleted
        self.undelivered: list[dict] = []

    # ---- the world
    @property
    def fake(self):
        return self.billing.fake

    def customer_of(self, account):
        row = self.billing.one("SELECT customer_id, deleted_at FROM stripe_customers WHERE account_id = ?", account)
        return row["customer_id"] if row is not None and row["deleted_at"] is None else None

    def subscriptions_of(self, account):
        customer = self.customer_of(account)
        if customer is None:
            return []
        return [s for s in self.fake.subscriptions.values() if s["customer"] == customer]

    def live_subscriptions(self, account):
        return [s for s in self.subscriptions_of(account) if s["status"] not in ("canceled", "incomplete_expired")]

    def open_invoices(self, account):
        customer = self.customer_of(account)
        if customer is None:
            return []
        return [i for i in self.fake.invoices.values() if i["customer"] == customer and i["status"] == "open"]

    def charges_of(self, account):
        customer = self.customer_of(account)
        if customer is None:
            return []
        return [c for c in self.fake.charges.values() if c["customer"] == customer]

    # ---- operations
    def step(self) -> None:
        account = self.random.choice([a for a in self.accounts if a not in self.gone] or self.accounts)
        choice = self.random.random()
        live = self.live_subscriptions(account)
        entitling = [s for s in live if s["status"] in mirror.ENTITLING_STATUSES]
        if choice < 0.16 and not live:
            self._buy(account)
        elif choice < 0.22 and not live:
            self._dashboard_subscription(account)
        elif choice < 0.34 and entitling:
            self._renew(entitling[0])
        elif choice < 0.42 and self.open_invoices(account):
            self.fake.pay_invoice(self.random.choice(self.open_invoices(account))["id"])
        elif choice < 0.50 and entitling:
            self._upgrade(account)
        elif choice < 0.56 and entitling:
            self.fake.set_cancel_at_period_end(entitling[0]["id"], self.random.random() < 0.7)
        elif choice < 0.60 and live:
            self.fake.cancel_now(self.random.choice(live)["id"])
        elif choice < 0.68 and self.charges_of(account):
            self._refund(account)
        elif choice < 0.76 and self.charges_of(account):
            self._dispute(account)
        elif choice < 0.79 and self.customer_of(account):
            self.fake.delete_customer(self.customer_of(account))
            self.gone.add(account)
        else:
            self.fake.advance(self.random.choice([3600, DAY, 5 * DAY, 20 * DAY]))
        self._flush()

    def _buy(self, account):
        try:
            result = self.billing.checkout(account)
        except service.BillingError:
            return
        if result.kind != "checkout":
            return
        outcome = self.random.choice(["paid", "paid", "paid", "declined", "requires_action"])
        self.fake.complete_checkout(result.session_id, outcome=outcome)

    def _dashboard_subscription(self, account):
        """A second subscription made outside Starling: the duplicate the contract has to survive."""
        customer = self.customer_of(account)
        # Only a customer Stripe still has: nobody can subscribe a deleted one in the Dashboard,
        # and the mirror may still be naming one that was deleted while a webhook was lost.
        if customer is None or self.fake.customers.get(customer, {}).get("_deleted"):
            return
        self.fake.start_subscription(customer, self.billing.price(self.random.choice(["starter", "pro"])))

    def _renew(self, subscription):
        self.fake.advance_period(subscription["id"], outcome=self.random.choice(["paid", "paid", "failed"]))

    def _upgrade(self, account):
        try:
            url = service.start_upgrade(self.billing.db, self.billing.settings, self.billing.client, account, "pro",
                                        now=self.fake.now)
        except (service.BillingError, Exception) as exc:
            if isinstance(exc, service.BillingError):
                return
            raise
        self.fake.confirm_portal_upgrade(url.rsplit("test_", 1)[1],
                                         outcome=self.random.choice(["paid", "paid", "failed"]))

    def _refund(self, account):
        charge = self.random.choice(self.charges_of(account))
        if charge["amount"] <= 0 or charge["refunded"]:
            return
        amount = None if self.random.random() < 0.5 else max(1, charge["amount"] // 3)
        self.fake.refund(charge["id"], amount=amount)

    def _dispute(self, account):
        open_disputes = [d for d in self.fake.disputes.values() if d["status"] in mirror.OPEN_DISPUTE_STATUSES]
        if open_disputes and self.random.random() < 0.6:
            self.fake.close_dispute(self.random.choice(open_disputes)["id"],
                                    self.random.choice(["won", "lost", "warning_closed", "prevented"]))
            return
        charge = self.random.choice(self.charges_of(account))
        if not charge["disputed"]:
            self.fake.open_dispute(charge["id"])

    # ---- delivery
    def _flush(self) -> None:
        """Deliver what Stripe produced, sometimes shuffled, duplicated or partly dropped."""
        events = self.fake.take_events() + self.undelivered
        self.undelivered = []
        if self.random.random() < 0.25:
            dropped = self.random.sample(events, k=self.random.randint(0, len(events))) if events else []
            self.undelivered = list(dropped)
            events = [event for event in events if event not in dropped]
        if self.random.random() < 0.4:
            self.random.shuffle(events)
        if self.random.random() < 0.3:
            events = events + list(events)
        for event in events:
            body, header = self.fake.delivery(event, WEBHOOK_SECRET)
            assert webhooks.intake(self.billing.db, self.billing.settings, body, header, self.fake.now).status_code == 200
        processor.process_pending(self.billing.db, self.billing.settings, self.billing.client, self.fake.now)

    def settle(self) -> None:
        """Everything Stripe knows reaches the gateway: retried deliveries, then recovery.

        The backlog is delivered rather than discarded, because Stripe retries a failed
        delivery for days; the reconciler only reaches back 30 days, so an event older than
        that would otherwise be lost for good and no invariant could speak about it.
        """
        backlog, self.undelivered = self.undelivered, []
        for event in backlog + self.fake.take_events():
            body, header = self.fake.delivery(event, WEBHOOK_SECRET)
            assert webhooks.intake(self.billing.db, self.billing.settings, body, header, self.fake.now).status_code == 200
        processor.process_pending(self.billing.db, self.billing.settings, self.billing.client, self.fake.now)
        reconcile.reconcile_events(self.billing.db, self.billing.settings, self.billing.client, now=self.fake.now)
        processor.process_pending(self.billing.db, self.billing.settings, self.billing.client, self.fake.now)
        reconcile.full_resync(self.billing.db, self.billing.settings, self.billing.client, now=self.fake.now)
        self._flush()


# --------------------------------------------------------------------------- invariants


def check_owner_safety(storm: Storm) -> None:
    billing, fake = storm.billing, storm.fake
    plans = billing.settings.plans
    rows = billing.all("SELECT * FROM entitlement_periods ORDER BY rowid")
    seen_invoices: set[str] = set()
    for row in rows:
        plan = plans.get(row["plan_code"])
        assert plan is not None, f"a period names a plan that does not exist: {row['plan_code']}"
        assert row["allowance"] >= 0 and row["cost_ceiling_micros"] >= 0
        # A full refund at the exact second a period began leaves it zero-length. That row can
        # never be resolved (resolution needs period_start <= now < period_end), which is the
        # point of the refund, so it is allowed -- but it may never run backwards.
        assert row["period_end"] >= row["period_start"], "a period may not end before it starts"
        if row["source"] == "free":
            assert row["allowance"] in (0, plans.free.monthly_credits), row["allowance"]
            continue
        invoice_id = row["granted_by_invoice_id"]
        if invoice_id is None:
            cap = plan.monthly_credits // entitlements.CARRY_OVER_DIVISOR
            assert row["allowance"] <= cap, f"a period nobody paid for holds {row['allowance']} > carry-over {cap}"
            continue
        assert invoice_id not in seen_invoices, f"invoice {invoice_id} granted more than one period"
        seen_invoices.add(invoice_id)
        invoice = fake.invoices.get(invoice_id)
        assert invoice is not None, f"a period names an invoice Stripe never had: {invoice_id}"
        assert invoice["status"] == "paid", f"a period was granted by an unpaid invoice: {invoice_id}"
        assert invoice["parent"]["subscription_details"]["subscription"] == row["subscription_id"]
        assert row["allowance"] <= plan.monthly_credits, (
            f"period holds {row['allowance']} against a plan allowance of {plan.monthly_credits}")
    # No subscription may hold more granted periods than it has paid period invoices.
    for subscription_id in {row["subscription_id"] for row in rows if row["subscription_id"]}:
        granted = sum(1 for row in rows if row["subscription_id"] == subscription_id and row["granted_by_invoice_id"])
        payable = sum(
            1 for invoice in fake.invoices.values()
            if invoice["parent"]["subscription_details"]["subscription"] == subscription_id
            and invoice["status"] == "paid"
            and any(not (line["parent"]["subscription_item_details"]).get("proration") for line in invoice["lines"]["data"])
        )
        assert granted <= payable, f"{subscription_id}: {granted} granted periods for {payable} paid invoices"


def _paid_for_now_at_stripe(storm: Storm, subscription) -> bool:
    """Stripe holds a paid invoice whose own period line covers this instant."""
    now = storm.fake.now
    for invoice in storm.fake.invoices.values():
        if invoice["parent"]["subscription_details"]["subscription"] != subscription["id"]:
            continue
        if invoice["status"] != "paid":
            continue
        for line in invoice["lines"]["data"]:
            details = line["parent"]["subscription_item_details"]
            if not details.get("proration") and line["period"]["start"] <= now < line["period"]["end"]:
                return True
    return False


def check_customer_liveness(storm: Storm) -> None:
    """Measured against Stripe, not against the mirror: what the customer actually bought."""
    billing, fake = storm.billing, storm.fake
    for account in storm.accounts:
        entitlement = billing.entitlement(account)
        subscriptions = billing.all("SELECT * FROM subscriptions WHERE account_id = ?", account)
        if any(row["disputed_at"] is not None for row in subscriptions):
            assert entitlement.status == "suspended", f"{account}: an open dispute must suspend"
            assert entitlement.plan_code == "free"
            continue
        if not [row for row in subscriptions
                if row["plan_code"] and entitlements._is_entitling(billing.settings, row, fake.now)]:
            assert entitlement.plan_code == "free", f"{account}: nothing entitles, so it must be free"
            assert entitlement.status in ("free", "suspended")
            continue
        alive = [
            subscription for subscription in storm.subscriptions_of(account)
            if subscription["status"] in ("active", "trialing")
            and not (subscription["cancel_at_period_end"]
                     and fake.now >= subscription["items"]["data"][0]["current_period_end"])
            and _paid_for_now_at_stripe(storm, subscription)
        ]
        if alive:
            assert entitlement.plan_code != "free", (
                f"{account}: Stripe has a live, paid subscription but the account is on the free plan\n"
                + _diagnosis(storm, account, alive))
            assert entitlement.status in ("active", "past_due")


def _diagnosis(storm: Storm, account: str, alive: list) -> str:
    """What the two sides believe, so a failure names the disagreement."""
    lines = [f"now={storm.fake.now}"]
    for subscription in alive:
        item = subscription["items"]["data"][0]
        lines.append(f"  stripe {subscription['id']} status={subscription['status']} price={item['price']['id']} "
                     f"period={item['current_period_start']}..{item['current_period_end']} "
                     f"cancel_at_period_end={subscription['cancel_at_period_end']}")
    for row in storm.billing.all("SELECT * FROM subscriptions WHERE account_id = ?", account):
        lines.append(f"  mirror {row['subscription_id']} status={row['status']} plan={row['plan_code']} "
                     f"price={row['price_id']} period={row['current_period_start']}..{row['current_period_end']} "
                     f"cancel_at_period_end={row['cancel_at_period_end']} cancel_at={row['cancel_at']} "
                     f"ended_at={row['ended_at']} disputed={row['disputed_at']}")
    for row in storm.billing.all("SELECT * FROM entitlement_periods WHERE account_id = ?", account):
        lines.append(f"  period {row['source']} sub={row['subscription_id']} plan={row['plan_code']} "
                     f"{row['period_start']}..{row['period_end']} allowance={row['allowance']} "
                     f"invoice={row['granted_by_invoice_id']}")
    return "\n".join(lines)


def check_dispute_suspension(storm: Storm) -> None:
    """Whatever the mirror thinks, an open dispute at Stripe must end in suspension."""
    billing, fake = storm.billing, storm.fake
    for account in storm.accounts:
        customer = storm.customer_of(account)
        if customer is None:
            continue
        charges = {c["id"] for c in fake.charges.values() if c["customer"] == customer}
        open_now = [d for d in fake.disputes.values()
                    if d["charge"] in charges and d["status"] in mirror.OPEN_DISPUTE_STATUSES]
        if open_now:
            assert billing.entitlement(account).status == "suspended", (
                f"{account}: open dispute, not suspended\n"
                + f"  disputes={[(d['id'], d['charge'], d['status']) for d in fake.disputes.values() if d['charge'] in charges]}\n"
                + f"  customer={customer}\n"
                + _diagnosis(storm, account, []))


# --------------------------------------------------------------------------- the runs


@pytest.mark.parametrize("seed", SEEDS)
def test_random_event_storms_keep_the_money_invariants(billing, seed):
    storm = Storm(billing, seed)
    for index in range(STEPS):
        storm.step()
        try:
            check_owner_safety(storm)
        except AssertionError as exc:
            raise AssertionError(f"seed {seed}, step {index}: {exc}") from exc
    storm.settle()
    check_owner_safety(storm)
    check_customer_liveness(storm)
    check_dispute_suspension(storm)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_storm_that_loses_every_webhook_is_repaired_by_recovery_alone(billing, seed):
    """Nothing is delivered while the storm runs; the reconciler has to rebuild the truth."""
    storm = Storm(billing, seed)
    storm._flush = lambda: storm.fake.take_events() and None  # drop every event
    for _ in range(STEPS // 2):
        storm.step()
    storm.settle()
    check_owner_safety(storm)
    check_customer_liveness(storm)


def test_the_generator_actually_exercises_the_interesting_paths(billing):
    """A guard on the test itself: a storm that never pays anything would prove nothing."""
    storm = Storm(billing, 4)
    for _ in range(STEPS):
        storm.step()
    storm.settle()
    granted = billing.all("SELECT * FROM entitlement_periods WHERE granted_by_invoice_id IS NOT NULL")
    events = billing.all("SELECT DISTINCT type FROM billing_events")
    types = {row["type"] for row in events}
    assert granted, "no period was ever granted"
    assert {"invoice.paid", "customer.subscription.updated"} <= types, types
    assert len(types) >= 5, types
