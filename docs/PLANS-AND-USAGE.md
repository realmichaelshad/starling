# Plans and usage

Last reviewed: 2026-09-18.

Where the rules come from:

- `backend/server/config/plans.toml`: the plans themselves.
- `backend/server/config/plans-free.toml`: the Free plan alone, for a free-only setup.
- `docs/architecture/CONTRACTS.md`, sections 6, 7 and 8: usage and billing behaviour.

Everything on this page is implemented and covered by tests that run against a fake Stripe and a
fake AI provider.

The three plans below are what the code and `plans.toml` define. They are not a record of what was
sold:

- A free-only file exists as well. Point `PLANS_FILE` at `plans-free.toml` and the service runs with
  the Free plan alone and needs no Stripe account.
- The author's public beta in autumn 2026 ran free-only, on that file and with Stripe not
  configured.
- The paid plans, Starter and Pro, exist in code and configuration but were never sold. No real
  Stripe payment has been taken.

## Plans

| | Free | Starter | Pro |
|---|---|---|---|
| Price | $0 | $9 per month | $19 per month |
| Drafts per period | 25 | 250 | 800 |
| Rate limit per minute | 3 | 6 | 10 |
| Rate limit per day | 10 | 60 | 150 |
| Fair-use AI cost ceiling per period | $0.30 | $4.50 | $9.00 |
| Stripe lookup key | – | `starling_starter_monthly_usd` | `starling_pro_monthly_usd` |
| Billing period | monthly, counted from the day the email address was verified | the Stripe subscription's billing period | the Stripe subscription's billing period |

The pricing page, checkout, webhooks, usage enforcement, the extension and the account pages all
read this one file.

**In code today:** the loader rejects a plans file in any of these cases:

- there is no `free` plan
- a paid plan is missing `amount_cents` or `currency`, or its interval is not `"month"`
- the free plan has a Stripe lookup key
- any allowance or ceiling is negative

A foundation test also checks that each paid plan's ceiling is below its price. Plans are displayed
in the order they appear in the file.

**What counts toward the rate limits.** Only requests that actually reserved a draft. The check
counts the account's `usage_reservations` rows created in the last minute and the last day, so a
request refused *before* a reservation exists (no drafts left, wrong consent, unverified email,
service paused, or a rate refusal itself) does not count. A reservation that is later released
(an error, a cancel before any text was shown) still counts toward the rate limit even though it
costs the customer no draft: the limit is about request pace, not billing.

The `Retry-After` time is exact rather than a whole window: it is the moment the oldest reservation
in the window falls out of it.

## What counts as a draft

A draft is one **metered press**. Three extension actions are metered:

| Customer action | API mode | Metered |
|---|---|---|
| Ask Starling about a post (the ✦ button: Starling decides whether and how to respond and drafts it) | `context` on `POST /api/v1/assist` | yes |
| Chat with Starling | `chat` on `POST /api/v1/assist/stream` | yes |
| Write or improve the text in X's composer (reply, quote or new post) | `enhance_composer` on `POST /api/v1/assist/stream` | yes |
| Scheduling helper | `schedule` on `POST /api/v1/assist` | no |
| Sign-in, session, plan list, consent, feedback, writing samples, passive snapshots | other `/api/v1` routes | no |

Each metered press uses `AI_CREDIT_MULTIPLIER` drafts. The default is 1.

In the extension these are the sparkle button on a post, the chat panel, and Create or Improve in
X's own text box.

## When a draft is charged

A draft is **charged** (committed) in either of these cases only:

1. The result is usable (the response is `ok` and contains draft or answer text) and at least one AI
   call for it succeeded.
2. **The customer stopped it after text had been shown.** They pressed Stop, or closed the page,
   after the first streamed text reached the extension. The stream's `final` then says
   `used_after_stop: true` and the extension tells them why a draft was used.

A draft is **never charged** (released) for:

- errors or timeouts, **even after text was shown**: an engine error, a provider outage, a timeout,
  an engine that went away, a gateway restart or a request past its five-minute wall
- cancelling before any text was shown
- results withheld by Starling's own quality checks, even after a preview had streamed, a "better to
  wait" verdict, an Enhance that keeps the customer's words (`unchanged`), and empty results
- answers that needed no AI call
- the same request sent again with the same `Idempotency-Key`. A completed request is replayed from
  its stored result (kept for 15 minutes) instead of being charged twice.
- requests refused before they start: no drafts left, busy, rate limited, service paused, email not
  verified, or consent missing
- background AI work, which never uses drafts

Starling still pays OpenAI for any AI calls made during a released request. That spend is recorded
(the `released_with_spend` counter) and counts toward the fair-use ceiling and the global budgets.

## Limits and resets

| Limit | Rule |
|---|---|
| Drafts available | Allowance for the period + grants − charged − reserved. A press needs at least `AI_CREDIT_MULTIPLIER` drafts available. |
| Reset, Free | a new 25-draft period every month, counted from email verification |
| Reset, paid plans | the new period's allowance is granted when that period's invoice is paid |
| Unused drafts | No general carry-over: each period stores its own allowance, and a new period starts from it. The only carry-over is the past-due provisional period, capped at 10% of the plan allowance ([PAYMENTS.md](PAYMENTS.md#failed-payments-upgrades-downgrades-and-cancellations)). |
| One press at a time | One active press per account. One more may wait up to 15 seconds; beyond that the customer gets `busy`. |
| Rate limits | per plan, per minute and per day (`rate_limited` with a `Retry-After` time) |
| Chat message length | 2,000 characters |
| Time | first text in about 90 seconds; whole stream within 240 seconds; the server stops anything still running at 300 seconds |
| Fair-use ceiling | AI cost per period (see below) |

## Plan changes

| Change | What happens |
|---|---|
| Upgrade (for example Starter to Pro) | Confirmed on Stripe's customer portal with an immediate proration invoice. The new plan starts only after that invoice is paid. The customer gains (new allowance − old allowance) × the fraction of the period remaining. Halfway through a period that is (800 − 250) × 0.5 = 275 extra drafts. |
| Downgrade | Scheduled for the end of the current period; the current plan continues until then. The customer can undo it before it takes effect. |
| Cancel | Takes effect at the end of the period. The customer keeps the paid plan until then, then moves to Free. |
| Failed payment, refund, dispute | See [PAYMENTS.md](PAYMENTS.md#failed-payments-upgrades-downgrades-and-cancellations). |

## Fair use and global budgets

- **Fair-use ceiling (per customer).** Each plan has a cap on estimated AI spend per period: $0.30
  Free, $4.50 Starter, $9.00 Pro. It sits well above typical use, about $0.0036 per draft on the
  default model, so honest customers do not reach it. It stops one unusual account from running the
  service at a loss. At the ceiling, the customer gets `fair_use_exceeded` until the next period.
- **Global budgets (whole service).** `AI_DAILY_BUDGET_USD` (default $10) and
  `AI_MONTHLY_BUDGET_USD` (default $90). When one is reached, new drafts pause for everyone with
  `service_paused` and a `Retry-After` time, until 00:00 UTC or the first of the next month.
  Free-plan accounts stop earlier: at `AI_FREE_TRIP_PCT` (default 80) percent of the daily budget,
  free accounts are refused while paid accounts keep drafting.
- **Order of limits.** The global budgets must sit at or below your OpenAI hard spend limit, which
  must sit at or below your usage tier limit. The defaults are chosen to stay under a new account's
  Tier 1 limit of $100 a month. See [AI-PROVIDER.md](AI-PROVIDER.md#spending-safeguards).

## What customers see

The website account pages and the extension both read one server function, `account_state()`, so
they always show the same numbers.

| Situation | Signal from the server | What the customer sees |
|---|---|---|
| 80% of drafts used | `usage.near_limit` is true when used + reserved ≥ 80% of the allowance and some drafts remain | In the dock: "N drafts left this month", then "They refill on ‹date›." with a **See plans** button linking to `/pricing?from=extension&reason=limit`. The footer adds "· Upgrade" at 20% or less. |
| No drafts left | HTTP 402 `limit_reached`, with the usage block and the upgrade path | "You've used your M drafts for this month", "They refill on ‹date›. Upgrade to keep drafting today. No draft was used." Buttons: **See plans** (filled) and **Not now**. Once the plan changes, the same card says how many drafts there are now and offers to draft the reply it was refused. |
| Fair-use ceiling reached | HTTP 402 `fair_use_exceeded`; `usage.fair_use_blocked` is true | "You've reached this month's fair-use limit", "Drafting resumes on ‹date›. No draft was used." |
| Daily request cap or a burst | HTTP 429 `rate_limited` with `Retry-After` (the same code for both; the extension tells them apart by the wait) | More than 5 minutes away: "You've reached today's limit on ‹plan›", "You can draft again at ‹time›, or upgrade for more requests a day. No draft was used." with **See plans**. Otherwise: "One moment", with a live countdown. |
| Payment problem | `plan.payment_problem` is true; HTTP 402 `payment_required` is returned only while the account is **suspended**, which today means an open Stripe dispute | "Your payment didn't go through" with an **Update payment** button to the billing page. A past-due account is not refused this way: it keeps paid access through the grace period, then falls back to the Free allowance. |
| Service paused (global budget reached or paused by the owner) | HTTP 503 `service_paused` with `Retry-After`; `service.status` is `paused` | "Drafting is paused", followed by the owner's own message if one was set with `ai-pause --message`, otherwise "Starling has paused drafting for a short while." |
| Extension too old | HTTP 426 `extension_outdated` (minimum set by `STARLING_EXTENSION_MIN_VERSION`) | "Update Starling": "This version of Starling is no longer supported. Chrome updates extensions on its own; you can also update now from chrome://extensions, then reload X. No draft was used." |

Every refusal the server makes before any draft text reaches the person says "No draft was used.";
where the connection itself failed, the message says "Nothing in X changed." instead. The
extension's own tests require that every error code has a title, a body and an action.

## Changing prices and allowances

### Changing a price

1. **Create a new Price in Stripe.**
   - In the sandbox first, create a new monthly recurring Price on the Starling product with the new
     amount.
   - Pass the same lookup key with `transfer_lookup_key=true`, so the lookup key moves to the new
     Price.
   - You cannot edit an existing Price's amount
     (https://docs.stripe.com/products-prices/manage-prices), and Starling's setup command never
     changes amounts on existing prices.
2. **Update `plans.toml` to match.** Set the same `amount_cents` (and `currency`, if it changed). The
   interval must stay `"month"`.
3. **Deploy.** In production, the gateway compares every Stripe price (amount, currency and
   interval) with `plans.toml` at startup and **refuses to start if they differ**.
4. **Handle existing subscribers.** Stripe keeps existing subscriptions on the old Price active,
   even after it is archived.
   - **Starling keeps mapping the old price id to its plan.** Rows in the price map are never
     deleted, and a price is matched by its id alone, so a grandfathered subscriber stays on their
     plan even after the price is archived and loses its lookup key. Only a price from the wrong
     Stripe mode, or one whose plan no longer exists in `plans.toml`, drops a customer to Free and
     raises an owner alert.
   - **Moving existing subscribers to the new price** is a Stripe-side decision Starling does not
     make for you. The gentle route is to leave them where they are and let new customers pay the
     new price. If you do want to move them, change each subscription's price in Stripe and let the
     resulting `customer.subscription.updated` event flow through; the reconciler will pick up
     anything you miss. Whatever you choose, tell customers first: a price change on an existing
     subscription is a consumer-law matter in many places.
5. **Run the verification command** in the sandbox, then repeat the change in live mode. See
   [PAYMENTS.md](PAYMENTS.md#setup-and-verification-commands).

### Changing allowances, ceilings or rate limits

Edit `plans.toml` and deploy.

**What a change affects, and when:**

| Field | Effect on a period already under way |
|---|---|
| `monthly_credits` | **None.** Each period stores its allowance when it is created. The new figure applies to the next period a customer starts: the next renewal, the next free month, or the next provisional past-due period. |
| `cost_ceiling_usd` | **None**, for the same reason. |
| `rate_per_minute`, `rate_per_day` | **Immediate.** They are read live from `plans.toml` on every request. |
| `name` | Immediate, as displayed. |
| `amount_cents`, `currency`, `interval` | These must match the Stripe price, or production refuses to start. Change the Stripe price first ([Changing a price](#changing-a-price)). |

If you want an existing customer to benefit from a raised allowance straight away, grant them the
difference in credits from their page on the owner dashboard. The grant is audited.

### Adding a plan

Add a `[plans.<code>]` section with a new lookup key, then run the Stripe setup command to create
its Price. Every paid plan's Price must sit under the same Stripe Product, because downgrades at
the end of a period depend on it ([PAYMENTS.md](PAYMENTS.md#customer-portal)).

**Plans are ranked by price first, then by allowance**, not by their position in the file.
A change to a higher rank is an upgrade (immediate proration invoice); a change to a lower rank is a
downgrade (a schedule at the end of the period). A change that is neither is refused. Position in the
file decides only the display order on the pricing page. So a new plan slots itself into the upgrade
and downgrade order by its `amount_cents`, wherever you put it in the file.

## Margins

The margin tables below use these assumptions:

- a US domestic card: 2.9% + $0.30 per payment
- Stripe Billing: 0.7% (https://stripe.com/pricing)
- no tax product
- AI on `gpt-5.6-luna`, with the estimates from [AI-PROVIDER.md](AI-PROVIDER.md)
- fixed hosting costs are left out; see [COSTS-AND-ACCOUNTS.md](COSTS-AND-ACCOUNTS.md)

"Contribution" is the price minus Stripe fees minus AI cost, per customer per month.

| Plan | Price | Stripe fees | AI, typical, every draft used | Contribution | AI, heavy, every draft used | Contribution | AI at fair-use ceiling (worst case) | Contribution |
|---|---|---|---|---|---|---|---|---|
| Free | $0 | $0 | $0.09 | −$0.09 | $0.28 | −$0.28 | $0.30 | −$0.30 |
| Starter | $9.00 | $0.62 | $0.90 | $7.48 | $2.75 | $5.63 | $4.50 | $3.88 |
| Pro | $19.00 | $0.98 | $2.88 | $15.14 | $8.80 | $9.22 | $9.00 | $9.02 |

Extra Stripe fees, per payment, when they apply:

| Fee | Starter | Pro | Source |
|---|---|---|---|
| International card, +1.5% | $0.14 | $0.29 | https://stripe.com/pricing |
| Currency conversion, +1% | $0.09 | $0.19 | https://stripe.com/pricing |
| Stripe Tax Basic, 0.5% where registered | $0.05 | $0.10 | https://stripe.com/pricing |
| Managed Payments (merchant of record), +3.5% | $0.32 | $0.67 | https://stripe.com/pricing |

Worst realistic case, an international card with currency conversion under Managed Payments at the
fair-use ceiling, leaves:

- Starter: about $3.34 contribution
- Pro: about $7.88 contribution

Whether Billing's 0.7% also applies under Managed Payments: verify at purchase time.

Other money to plan for:

- Refunds do not return the original processing fees (https://docs.stripe.com/refunds).
- Each US dispute costs a $15 fee that is not returned, plus another $15 if you contest it, which
  is returned if you win (https://stripe.com/pricing).
- Every Free account can cost up to $0.30 of AI a month.
- With about $30 a month of fixed costs, break-even is:
  - 5 Starter or 2 Pro customers at typical full use
  - 8 Starter or 4 Pro customers who all sit at the fair-use ceiling

## If you switch drafting to `gpt-5.6-terra`

A typical draft costs about $0.03 with terra drafting, against $0.0036 on luna. Starling cannot
charge more drafts per press except through `AI_CREDIT_MULTIPLIER`, a whole number. The table below
keeps today's prices and ceilings.

| Plan (price) | Multiplier | Presses per period | AI cost if all are used | Contribution after Stripe fees | Current ceiling stops the customer after |
|---|---|---|---|---|---|
| Free ($0) | 1 | 25 | $0.75 | −$0.75 | 10 presses |
| Free ($0) | 2 | 12 | $0.36 | −$0.36 | 10 presses |
| Free ($0) | 3 | 8 | $0.24 | −$0.24 | not reached |
| Starter ($9) | 1 | 250 | $7.50 | $0.88 | 150 presses |
| Starter ($9) | 2 | 125 | $3.75 | $4.63 | not reached |
| Starter ($9) | 3 | 83 | $2.49 | $5.89 | not reached |
| Pro ($19) | 1 | 800 | $24.00 | −$5.98 | 300 presses |
| Pro ($19) | 2 | 400 | $12.00 | $6.02 | 300 presses |
| Pro ($19) | 3 | 266 | $7.98 | $10.04 | not reached |

Why a multiplier or higher prices are needed:

- **Losses.** With a multiplier of 1, a Pro customer who uses every draft costs more in AI than they
  pay.
- **Surprise cut-offs.** Today's ceilings would stop Starter customers at 150 of their 250 drafts and
  Pro customers at 300 of 800. They would see `fair_use_exceeded` while their plan still shows
  drafts left. Raising the ceilings removes the cut-off but also removes the loss protection.
- **Price rises instead.** To keep a multiplier of 1 and today's contribution at typical full use,
  prices would need to be about $15.85 for Starter and $40.91 for Pro. The formula is (AI cost +
  today's contribution + $0.30) ÷ 0.964. Ceilings would also need to rise to at least $7.50 and
  $24.00, with headroom for heavy drafts.
- **Uncertain heavy cost.** A heavy terra press has not been measured. If every call used terra, a
  heavy press could reach about $0.11, so the ceilings remain the main protection.

If you change the multiplier, update the pricing page and plan copy so customers understand what one
press uses. Both the extension and the account pages show **credits**, using the unit label from
`plans.toml` (today "drafts"), for example "12 of 250 drafts left". Neither counts presses. With a
multiplier above 1 a customer therefore sees their draft count fall by more than one per press, so
the unit label and the pricing copy have to explain that, or change the unit name to something that
fits.

## What is unverified here

- **Stripe fees** were re-checked against https://stripe.com/pricing on 2026-09-18 and are unchanged.
  The margin tables are arithmetic on those fees and on the AI cost estimates in
  [AI-PROVIDER.md](AI-PROVIDER.md), which have not been measured against a real OpenAI account.
- **AI cost per draft** is an estimate from token counts, not a measurement. Every margin below
  moves with it. After the AI pass of 2026-09-22 a typical press measured, against the stand-in
  model, about 1,700 to 3,500 input and 100 to 250 output tokens, roughly a quarter of the 13,000
  input and 800 output the cost tables assume; the tables stay as an upper bound until a live
  account confirms it.
- **Web search** is paid inside the same ceiling: a search runs only while searches have used
  under 25% of the plan's cost ceiling for the period, which at about 1.2 cents a search is roughly
  5 searches a month on Free, 90 on Starter and 185 on Pro (and never more than
  `WEB_SEARCH_PER_ACCOUNT_DAY` a day). The real cost per search is one of the live checks.
- **No real payment has been taken**, so no fee has ever actually been charged to test these
  numbers.
