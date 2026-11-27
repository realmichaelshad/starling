# Payments (Stripe)

Last reviewed: 2026-09-18.

This page describes the billing design from `docs/architecture/CONTRACTS.md` sections 7.1 and 8, as
it is implemented. **Every behaviour below was verified against a fake Stripe, never against Stripe
itself**: the test suites run the billing code in test mode (`STRIPE_MODE=test`) against that fake,
no sandbox test has been run, and no payment has been taken. The paid plans were never sold.
Stripe's own fees, procedures and page names were checked on the dates shown.

Stripe is optional. Production requires it only when the plans file holds a paid plan: point
`PLANS_FILE` at `backend/server/config/plans-free.toml` for a free-only setup with no Stripe
account. The author's public beta in autumn 2026 ran that way.

## How money flows

- **Customer subscriptions.** Customers pay the operator (the person or company that runs the
  instance) monthly, through the operator's own Stripe account, using Stripe Checkout. Stripe
  Billing renews the subscription. Customers manage cards, invoices and cancellation in Stripe's
  customer portal.
- **What Starling holds.** Card details stay with Stripe. Starling stores Stripe ids and a mirror of
  subscription and invoice status.

Technical notes:

- The Stripe client is `backend/server/starling_server/billing/stripe_client.py`, using plain HTTPS
  (no Stripe SDK).
- The API version is pinned with `STRIPE_API_VERSION=2026-08-26.dahlia`.
- Billing periods are read from the subscription item (`items.data[0].current_period_start` and
  `current_period_end`), where Stripe has kept them since the 2025-03-31.basil version
  (https://docs.stripe.com/changelog/basil/2025-03-31/deprecate-subscription-current-period-start-and-end).

## What you create in Stripe

| Item | How | Notes |
|---|---|---|
| Stripe account for the operator's business | Stripe sign-up and business verification | Must belong to the business that sells Starling subscriptions. |
| A sandbox for testing | Dashboard account picker → Sandboxes | Stripe recommends a general sandbox for new integrations, and separate sandboxes for local development and automated tests (https://docs.stripe.com/sandboxes). |
| Restricted API key | Dashboard → API keys | See [API key](#api-key). |
| One Product holding every paid plan's monthly Price (Starter, Pro) | Starling's setup command | Lookup keys come from `plans.toml`. All paid prices must sit under the same Product; downgrades at the end of a period depend on it. |
| Two customer portal configurations | Starling's setup command | The management configuration goes in `STRIPE_PORTAL_CONFIGURATION_ID`; the configuration used only for confirmed upgrades goes in `STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID`. The command prints both ids, ready to paste. It prints the upgrade id only once every paid plan has a good price: fix any price problem it reports and run it again. |
| Webhook event destination | Dashboard → Workbench → Webhooks | **You create this by hand.** The setup command does not register it; Starling never calls Stripe's webhook endpoint API. See [Webhook endpoint](#webhook-endpoint). |
| Failed-payment retry settings | Dashboard → Billing → Revenue recovery → Retries | See [Stripe retry settings](#stripe-retry-settings). |
| Tax settings (optional) | Dashboard → Tax, or Managed Payments | See [Taxes](#taxes). This is a business decision. |
| Customer emails (**turn these on before launch**) | Dashboard → Settings → Customer emails, and Billing → Revenue recovery | Successful payments and refunds (receipts), failed payments and expiring cards. Starling sends its own account emails but no receipts, so without these a paying customer never gets one. |
| Branding | Dashboard → Settings → Branding | Icon `web/static/img/starling-icon-128.png`, brand colour `#000000`, accent `#faf8f3`, so Checkout, the portal, invoices and receipts look like the site. Set the statement descriptor to `STARLING` so the charge is recognisable on a card statement. |

## Sandbox (test) versus live mode

- A sandbox and live mode each have their own API keys, and objects in one cannot be used in the
  other (https://docs.stripe.com/keys).
- Sandbox keys start with `sk_test_` or `rk_test_`. Live keys start with `sk_live_` or `rk_live_`.
- Starling's `STRIPE_MODE` must match the key: `test` for sandbox keys, `live` for live keys.

**In code today:** the configuration check reports a problem, and production refuses to start, when:

- `STRIPE_MODE` does not match the key prefix
- `STRIPE_WEBHOOK_SECRET` is not a `whsec_` value
- production runs with `STRIPE_MODE=test` and without `STRIPE_ALLOW_TEST_MODE=true`

**Rehearsing on the deployed host.** Set `STRIPE_ALLOW_TEST_MODE=true` beside the sandbox keys. The
service then runs in production mode on Stripe's sandbox, says so on every page ("Payments are in
test mode"), tells search engines not to index it, and shows the owner an alert until live keys
replace the sandbox ones. Remove the switch when you go live.

In production the gateway also runs the full `stripe-verify` check at start-up and **refuses to
start** if anything fails: the key belongs to `STRIPE_ACCOUNT_ID` (through `GET /v1/account`), every
paid plan has an active price whose amount, currency and interval match `plans.toml`, each price's
`livemode` matches the mode, and both portal configurations exist, are active and have the right
settings. If the restricted key is not allowed to read the account, the account check is skipped with
a warning rather than a crash. Webhook events whose `livemode` does not match the mode get 400, are
not stored, and raise an owner alert.

Local development without a public URL: the Stripe CLI can forward sandbox events to your machine
(https://docs.stripe.com/webhooks). For example:

```
stripe listen --forward-to http://127.0.0.1:8765/webhooks/stripe
```

The command prints a signing secret to use as `STRIPE_WEBHOOK_SECRET` while it runs. Use Stripe's
test cards for payments (https://docs.stripe.com/testing).

## API key

Use a **restricted key** (`rk_...`), not an unrestricted secret key. Stripe recommends restricted
keys for most integrations (https://docs.stripe.com/keys).

The list below is derived from every Stripe endpoint the code actually calls. Stripe's key editor
groups permissions by resource, and the names it shows may not match these headings exactly; grant
the resource that covers each endpoint.

| Stripe resource | Access | Endpoints Starling calls | Why |
|---|---|---|---|
| Customers | Write | `POST /v1/customers`, `GET /v1/customers/{id}` | create the customer before checkout (idempotency key `cust-<account_id>`); every checkout reads the customer back, so one deleted in the Dashboard is replaced instead of blocking the purchase |
| Checkout Sessions | Write | `POST /v1/checkout/sessions`, `GET`, `POST .../expire`, list | create sessions and expire other open ones for the same account |
| Billing Portal | Write | `POST /v1/billing_portal/sessions`, `POST` and list `/v1/billing_portal/configurations` | setup creates both configurations; account pages open portal sessions and upgrade confirmations |
| Subscriptions | Write | `GET /v1/subscriptions/{id}`, list, `DELETE /v1/subscriptions/{id}` | refetch subscriptions; cancel immediately on a full refund, a lost dispute or a duplicate, and end a first purchase that was never paid when the customer chooses a different plan |
| Subscription schedules | Write | `POST /v1/subscription_schedules`, `GET`, `POST {id}`, `POST {id}/release` | schedule downgrades and undo them |
| Invoices | Read | `GET /v1/invoices/{id}`, list, `GET /v1/invoice_payments` | grant each period's allowance when its invoice is paid, map a charge to its subscription, and read the `hosted_invoice_url` of a first invoice that was never paid |
| Charges | Read | `GET /v1/charges/{id}` | react to refunds |
| Disputes | Read | `GET /v1/disputes/{id}`, list | react to disputes; when one closes, list the customer's other disputes so a suspension is lifted only when none is still open or lost |
| Events | Read | `GET /v1/events` | the hourly reconciler, which lists the last 30 days of events |
| Prices | Write during setup; Read afterwards | `POST /v1/prices`, `GET /v1/prices/{id}`, list | setup creates them; the start-up check compares them with `plans.toml` |
| Products | Write during setup; Read afterwards | `POST /v1/products`, list | setup creates the single Starling product |
| Account | Read | `GET /v1/account` | the start-up check that the key belongs to `STRIPE_ACCOUNT_ID` |

Starling never calls a refund, payment intent, tax or webhook endpoint API. Refunds and disputes are
read-only: you issue refunds in the dashboard, and Starling reacts to the event.

Other points about the key:

- A restricted key you create yourself in live mode is shown only once. Store it straight away in
  the host's secret store.
- Stripe lets you attach an **access policy** to a key, for example an allowlist of IP addresses or
  ASNs, and recommends one on live keys (https://docs.stripe.com/keys#access-policies). Check
  whether your host gives you stable outgoing IP addresses before you use an IP allowlist.

## Setup and verification commands

Operator commands run from `backend/server` as `python -m starling_server.cli <command>`. All three
take no options.

| Command | What it does |
|---|---|
| `stripe-setup` | Creates one Starling Product and, under it, a monthly Price with its lookup key for each paid plan in `plans.toml`, plus the two customer portal configurations (management and upgrade-only). Safe to run more than once: it finds existing objects by lookup key and setup metadata, and it **never changes the amount of an existing Price**. |
| `stripe-verify` | Checks the Stripe account against the configuration. This is the same check the gateway runs at start-up in production. |
| `stripe-resync` | Recovers missed events from the last 30 days, resyncs every customer, then processes every pending event. Use it after a webhook outage. |

What `stripe-verify` checks, in order:

1. the key prefix matches `STRIPE_MODE`;
2. `GET /v1/account` matches `STRIPE_ACCOUNT_ID` (skipped with a warning when a restricted key may
   not read the account);
3. every paid plan in `plans.toml` has an active price with that plan's lookup key;
4. each price's amount, currency, interval and interval count match `plans.toml`;
5. each price's `livemode` matches `STRIPE_MODE`;
6. both portal configuration ids are set (required in production), exist and are active;
7. the management configuration has plan switching off, and payment-method update, invoice history,
   cancel-at-period-end, and address and tax id editing on, with email editing off;
8. the upgrade configuration has plan switching on with `always_invoice` proration and lists every
   current plan price.

It does **not** check the webhook endpoint, its event list, or your tax settings. Check those by
hand.

Exit status: 0 and an `OK` line listing the account, mode and each plan's price id; 1 with each
problem printed as `MISMATCH`; 2 if Stripe itself returned an error. Warnings print but do not fail.

Run all of this in the sandbox first, then again with live keys. Do not copy products from the
sandbox to live mode by hand: run the setup command in live mode so the lookup keys and metadata are
right.

## Webhook endpoint

**Endpoint URL:** `<STARLING_PUBLIC_URL>/webhooks/stripe`, for example
`https://your-domain.example/webhooks/stripe`. Stripe requires a public HTTPS URL.

Create it in the Dashboard (https://docs.stripe.com/webhooks):

1. Open **Workbench → Webhooks → Create an event destination**, and choose **Your account**.
2. For the API version, choose `2026-08-26.dahlia`, the same as `STRIPE_API_VERSION`.
3. Select exactly these 16 events:

   - `checkout.session.completed`
   - `checkout.session.expired`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `customer.subscription.pending_update_applied`
   - `customer.subscription.pending_update_expired`
   - `invoice.paid`
   - `invoice.payment_failed`
   - `invoice.payment_action_required`
   - `charge.refunded`
   - `charge.dispute.created`
   - `charge.dispute.closed`
   - `customer.deleted`
   - `subscription_schedule.released`
   - `subscription_schedule.canceled`

4. Choose **Webhook endpoint** and enter the URL.
5. Reveal the signing secret (`whsec_...`) and store it as `STRIPE_WEBHOOK_SECRET`.

Each sandbox and live mode has its own endpoint and its own secret.

How Starling handles events:

- It verifies the `Stripe-Signature` header against the current secret or
  `STRIPE_WEBHOOK_SECRET_PREVIOUS`, allowing 300 seconds of clock difference.
- It stores the event and answers 200 at once. A single background worker then processes events one
  at a time: it refetches the latest objects from Stripe, updates its mirror and recalculates the
  customer's plan.
- Events left unprocessed are retried at startup. An hourly reconciler lists the last 30 days of
  events, and a daily job resyncs all customers.

Why the design works this way: Stripe does not guarantee event order and may deliver the same event
more than once. In live mode Stripe retries failed deliveries for up to three days; in a sandbox it
retries three times over a few hours (https://docs.stripe.com/webhooks).

## Customer portal

The setup command creates two portal configurations.

**1. Management configuration** (`STRIPE_PORTAL_CONFIGURATION_ID`). Customers reach it from the
billing page to manage their subscription. Settings:

| Feature | Setting | Why |
|---|---|---|
| Switch plans | off | Customers cannot change plans freely here. Upgrades go through the upgrade configuration below, and downgrades use a subscription schedule that Starling creates. |
| Cancel | on, at the end of the period | Customers keep what they paid for. |
| Update payment method | on | |
| Invoice history | on | |
| Billing address and tax id | on | |
| Email address editing | off | The email is the customer's Starling sign-in; customers change it on Starling's account security page. |

**2. Upgrade configuration** (`STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID`). It is used only for
confirmed upgrades:

- Starling opens a portal confirmation page for the higher price (`subscription_update_confirm`,
  with `proration_behavior=always_invoice`, so the difference is invoiced at once).
- The new plan starts only after that invoice is paid.
- Because every paid price sits under the same Stripe Product, the same product also lets
  downgrades switch price at the end of a period.
- Keep this configuration separate. Never use it as the general portal link.

Its exact settings, as the setup command creates them:

| Feature | Setting |
|---|---|
| Switch plans | **on**, allowed update `price` only, proration `always_invoice`, listing every current plan price under the single Starling product, with a schedule at period end when the amount decreases |
| Cancel | on, at the end of the period, proration `none` |
| Update payment method | on |
| Invoice history | on |
| Billing address and tax id | on |
| Email address editing | off |

Both configurations also carry a return URL of `<STARLING_PUBLIC_URL>/account/billing`, and links to
your `/privacy` and `/terms` pages when the public URL is https.

Portal features are described at https://docs.stripe.com/customer-management.

## Taxes

Starling does not decide whether, where or how you must collect tax. Decide with your accountant
before going live. The three options:

| Option | Extra fee | What it involves in Starling | Source |
|---|---|---|---|
| No tax collection | none | Keep `STRIPE_AUTOMATIC_TAX=false`. | – |
| Stripe Tax (you remain the seller) | Tax Basic: 0.5% per transaction where you are registered (Stripe's rate for no-code integrations such as Checkout; direct Tax API use is priced per transaction instead; verify which applies at purchase time) | Add your tax registrations in Stripe, set `STRIPE_AUTOMATIC_TAX=true`, and set `STRIPE_TAX_CODE`. For SaaS, Stripe lists `txcd_10103000` for personal use and `txcd_10103001` for business use; the difference matters only for US sales. | https://stripe.com/pricing , https://docs.stripe.com/tax/tax-codes |
| Stripe Managed Payments (Stripe is the merchant of record) | +3.5% on top of payment fees | See the notes below. | https://docs.stripe.com/payments/managed-payments , https://docs.stripe.com/payments/managed-payments/eligibility |

About Managed Payments:

- Stripe handles sales tax, VAT and GST in more than 80 countries, plus fraud, disputes and
  transaction-level customer support.
- It works only with Checkout and Payment Links, and it requires an eligibility review.
- Supported seller locations include the US, Canada, much of the EU, the UK, Australia, Hong Kong,
  Japan and Singapore.
- Stripe lists some features as unsupported, including creating subscriptions outside Checkout or
  Payment Links, and one-off invoices outside the billing period.
- **Unverified, and it matters.** Whether Starling's upgrade flow (an immediate proration invoice
  through a portal confirmation) and its downgrade flow (a server-created subscription schedule) work
  under Managed Payments has not been confirmed with Stripe, and the unsupported-feature list above
  is the reason to doubt it. If you are considering Managed Payments, test both flows in a sandbox
  with it enabled before you commit; if they do not work, plan changes would have to move into
  Checkout, which is a code change.

Another merchant of record, Paddle, charges 5% + 50¢ per transaction (https://www.paddle.com/pricing).
Using it would mean replacing Starling's Stripe integration.

## Failed payments, upgrades, downgrades and cancellations

Starling decides each customer's plan from the Stripe data it mirrors (CONTRACTS section 7.1):

| Situation | What the customer gets |
|---|---|
| No subscription | Free: 25 drafts per month, counted from email verification. |
| New paid subscription | The paid allowance for a period is granted when that period's invoice is paid (`invoice.paid`). The checkout success page grants nothing; it waits for the webhook by polling `/billing/status`. |
| Upgrade | The new plan starts only after the proration invoice is paid, with extra drafts for the rest of the period: (new − old allowance) × fraction of the period remaining. |
| Downgrade | A subscription schedule switches to the lower price, under the same Stripe Product, at the end of the current period. Undo releases the schedule, so the subscription simply continues on its current price. |
| Cancel | Cancels at the end of the period: paid until then, then Free. |
| Payment failed (`past_due`) | Paid access continues for `PAST_DUE_GRACE_DAYS` (default 7) after the first failure. During the grace period Starling creates a provisional period carrying over the customer's **unused** allowance from the last paid period, capped at 10% of the plan allowance, with the cost ceiling prorated to match. When the invoice is finally paid, that same provisional period becomes the paid period, so anything used in the gap counts against the new allowance. **After the grace period ends** the subscription stops entitling anything and the customer drops to the Free plan: 25 drafts a month on the free monthly cycle anchored to their email verification, with free-plan rate limits. The account is not suspended, and the billing page keeps showing a payment problem. |
| `incomplete`, `incomplete_expired`, `unpaid`, `canceled`, `paused` | Free. |
| First payment declined, or waiting for the bank's confirmation (`incomplete`) | Free, never charged. Stripe does not retry a first payment and the customer portal cannot pay an invoice that is already open, so Starling sends the customer to **that invoice's own hosted page**: the checkout return page and the Billing page both say "Your payment is not finished" with a "Finish paying for <plan>" button. If they choose a **different** plan instead, Starling cancels the unfinished subscription and opens a new checkout, so nobody pays for a plan they no longer want. Stripe expires an unpaid first invoice after about 23 hours; after that a normal checkout starts again. |
| Open dispute | Suspended (Free allowance only) until the dispute closes in your favour **and no other dispute of that customer is still open or was lost**. A lost dispute keeps the account suspended for good. |
| A paid invoice for a subscription that had already ended | Free, and an owner alert (`paid_invoice_without_access`): money arrived that gives no access, so refund it or reinstate the customer by hand. |
| Full refund, customer deleted, or subscription not found | Free. |
| Unknown price id | Free, and an owner alert is raised. |
| More than one paid subscription | The highest plan, plus an owner alert. The reconciler cancels the newer subscription. |

Checkout safeguards:

- Checkout sessions are created only by the server and expire 30 minutes later, plus a 60-second
  margin so a gateway clock slightly behind Stripe's does not create a session Stripe rejects.
- Other open sessions for the same account are expired when a new one is created.
- The customer's existing subscriptions are rechecked live before a session is created.
- `STRIPE_BLOCK_PREPAID=true` (the default) blocks prepaid cards.
- `STRIPE_ALLOW_PROMO_CODES=false` (the default) hides promotion codes.
- The website's Content-Security-Policy lists the three Stripe origins its billing forms redirect to
  (`checkout.stripe.com`, `billing.stripe.com`, `invoice.stripe.com`). A browser applies `form-action`
  to the redirect, so an origin missing from that list makes the button do nothing at all. If Stripe
  ever serves one of these pages from another host, add it in `security/middleware.py`.

**Check this once in your sandbox before launch.** The "Finish paying" path was built and tested
against a local stand-in for Stripe. Decline a first payment with a test card
(https://docs.stripe.com/testing), press "Finish paying", and confirm that Stripe's hosted invoice
page takes a working card and that the plan appears on the Billing page afterwards. Stripe's page has
no button back to your site; the customer returns by themselves.

### Stripe retry settings

Stripe retries failed subscription payments using the settings under **Billing → Revenue recovery →
Retries**. Stripe's recommended default for Smart Retries is 8 tries within 2 weeks. You also choose
what happens after the last try: cancel the subscription, mark it unpaid, or leave it past due
(https://docs.stripe.com/billing/revenue-recovery/smart-retries).

- **Cancel** and **mark unpaid** both move the customer to Free in Starling.
- **Leave past due** keeps Stripe trying to charge future invoices while Starling gives no new paid
  allowance. That is confusing for customers, so pick one of the other two.

Which to choose is a business decision.

## Refunds and disputes

**Refunds.** Issue them in the Stripe Dashboard, either full or partial.

- Stripe lists no fee for issuing a refund, but the original processing fees are not returned
  (https://stripe.com/pricing , https://docs.stripe.com/refunds).
- Customers usually see the credit 5–10 business days later.
- **A full refund cancels the subscription.** On `charge.refunded` with the charge fully refunded,
  Starling finds the subscriptions behind that charge, cancels each one immediately in Stripe (no
  proration, no final invoice), ends the current entitlement period and drops the customer to Free.
  You do not need to cancel it by hand as well.
- **A partial refund changes nothing automatically.** Starling raises an owner alert
  (`partial_refund`) with the amounts and leaves the plan alone, so you can decide. The ledger's
  `refund_adjust` entry type is reserved and currently unused; adjust by granting or debiting credits
  from the customer's dashboard page if you want to.

**Disputes (chargebacks).**

- When a dispute opens (`charge.dispute.created`), Starling suspends the account to the Free
  allowance until the dispute closes in your favour (`charge.dispute.closed`).
- Stripe's US dispute fee is $15 and is not returned. Contesting costs another $15, which is
  returned if you win (https://stripe.com/pricing).
- You usually have 7–21 days to respond, and the whole process can take 2–3 months. You cannot
  refund a payment while its dispute is open (https://docs.stripe.com/disputes/how-disputes-work).

## Go-live checklist

| Done | Step |
|---|---|
| ☐ | Stripe business verification is complete for the live account. |
| ☐ | The tax decision is made and configured (none, Stripe Tax, or Managed Payments). |
| ☐ | The sandbox end-to-end test passed: checkout, `invoice.paid` grant, upgrade, downgrade and undo, cancel, failed payment with grace, refund, dispute, customer portal. See [TESTING.md](TESTING.md#the-live-checks-that-remain). |
| ☐ | A live restricted key was created with the permissions above and stored as `STRIPE_SECRET_KEY`; `STRIPE_MODE=live`; `STRIPE_ACCOUNT_ID` set. |
| ☐ | `stripe-setup` was run in live mode, and both `STRIPE_PORTAL_CONFIGURATION_ID` and `STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID` are set. |
| ☐ | The live webhook destination has the exact 16 events and API version `2026-08-26.dahlia`, and `STRIPE_WEBHOOK_SECRET` is set. |
| ☐ | `stripe-verify` passes in live mode. |
| ☐ | Prices in Stripe match `plans.toml`, and the gateway starts in production (it refuses to start on a mismatch). |
| ☐ | Retry settings and the final action after failed retries are chosen. |
| ☐ | Customer emails (receipts, refunds, failed payments, expiring cards) and branding are set, and the statement descriptor reads `STARLING`. |
| ☐ | Legal pages (terms, refunds, privacy) are published with no placeholders ([CONFIGURATION.md](CONFIGURATION.md#business-and-legal-details)). |
| ☐ | One small real payment was made with a real card, then refunded, and the plan changes were checked. |
| ☐ | Any sandbox keys used during development by anyone outside your own team are expired. |

## Rotating secrets

**Stripe API key**

1. In **Dashboard → API keys**, open the key's menu, choose **Rotate key**, and pick an expiration
   for the old key. When you rotate in the Dashboard, both keys keep working for up to 7 days
   (https://docs.stripe.com/keys).
2. Put the new value in `STRIPE_SECRET_KEY` in the host's secret store and redeploy.
3. Confirm checkout and the account pages work, and that the old key's request logs show no more
   traffic.
4. Let the old key expire, or expire it straight away.

If a key is exposed, choose the **Now** expiration so the old key stops immediately, then update
and redeploy.

**Webhook signing secret**

1. In **Workbench → Webhooks**, open the endpoint and choose **Roll secret**. Stripe can keep the
   old secret valid for up to 24 hours, and signs with both secrets during that time.
2. Set `STRIPE_WEBHOOK_SECRET_PREVIOUS` to the old secret and `STRIPE_WEBHOOK_SECRET` to the new
   one, then redeploy. Starling accepts either secret.
3. After the old secret expires, clear `STRIPE_WEBHOOK_SECRET_PREVIOUS` and redeploy.

## Settings reference

| Setting | Default | Meaning |
|---|---|---|
| `STRIPE_MODE` | `test` | `test` (sandbox keys) or `live`; production requires `live` |
| `STRIPE_SECRET_KEY` | empty | restricted key (`rk_...`) recommended |
| `STRIPE_ACCOUNT_ID` | empty | the account the key must belong to |
| `STRIPE_WEBHOOK_SECRET` | empty | the endpoint's `whsec_...` secret |
| `STRIPE_WEBHOOK_SECRET_PREVIOUS` | empty | set only during a secret rotation |
| `STRIPE_API_VERSION` | `2026-08-26.dahlia` | pinned API version |
| `STRIPE_PORTAL_CONFIGURATION_ID` | empty | management portal configuration (plan switching off), from the setup command |
| `STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID` | empty | second portal configuration used only for confirmed upgrades, from the setup command |
| `STRIPE_AUTOMATIC_TAX` | `false` | turn on Stripe Tax in Checkout |
| `STRIPE_TAX_CODE` | empty | product tax code when Stripe Tax is on |
| `STRIPE_BLOCK_PREPAID` | `true` | block prepaid cards in Checkout |
| `STRIPE_ALLOW_PROMO_CODES` | `false` | show the promotion code field in Checkout |
| `STRIPE_API_BASE` | `https://api.stripe.com` | automated tests only |
| `PAST_DUE_GRACE_DAYS` | `7` | paid access after the first failed payment |

## Live checks still pending

These have not been run. Each needs a real Stripe account, which the operator opens:

- The sandbox checkout, portal and webhook delivery flow.
- The same in live mode with a real card.
- Stripe Tax or Managed Payments behaviour, if chosen.

## Fees re-checked on 2026-09-18

Against https://stripe.com/pricing, and unchanged: US online domestic cards 2.9% + 30¢;
international cards +1.5%; currency conversion +1%; disputes $15 each, plus $15 to contest which is
returned if you win; Billing pay-as-you-go 0.7% of billing volume.

Stripe Tax pricing sits on the same page, not on https://stripe.com/tax: **0.5% per transaction** for
a no-code integration such as Checkout, where you are registered to collect; **$0.50 per transaction**
for the Tax API. Starling uses Checkout, so the 0.5% rate is the one that applies.

## What is still unverified

- **Everything about the real Stripe.** Sandbox checkout, the portal, webhook delivery, retries and
  live mode with a real card have not been run. All billing behaviour above was tested against a fake
  Stripe that answers the same REST shapes.
- **Stripe Managed Payments compatibility.** Whether Starling's upgrade flow (an immediate proration
  invoice) and downgrade flow (a subscription schedule) work under Managed Payments has not been
  confirmed, and Stripe lists limits on creating subscriptions outside Checkout. Do not choose
  Managed Payments without testing those two flows first.
- **Whether Billing's 0.7% also applies under Managed Payments.**
