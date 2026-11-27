# Operations

Last reviewed: 2026-09-18.

Running Starling day to day: what to watch, what to do when something breaks, how to rotate secrets,
and where the limits of this design are.

**How far this page has been exercised.** Every behaviour is from the code and from local runs with
mocked providers. The author also ran Starling on Fly.io as a free public beta in autumn 2026, with
the Anthropic provider (Claude Haiku 5.5), web search off, the free-only plan file and Stripe not
configured. That beta has ended and no hosted Starling service is running now. Real Stripe payments
were never taken: the billing code has only run against the stand-in Stripe in this repository,
never against Stripe itself. The first real incident will teach you something this page does not say.

The mechanical runbooks (the exact backup, verify and restore commands, maintenance mode, the
trusted-proxy check) are in [`deploy/README.md`](../deploy/README.md). This page says when to reach
for them and what else to do.

Every command below runs inside the container:

```
fly ssh console -C "python -m starling_server.cli <command>"
docker compose -f deploy/docker-compose.yml exec starling python -m starling_server.cli <command>
```

## 1. Monitoring

### Health endpoints

| Endpoint | Answers | Use it for |
|---|---|---|
| `GET /healthz` | always 200 `{"ok": true}` while the gateway is up | the host's health check and your uptime monitor |
| `GET /readyz` | 200 when every check passes, 503 with the failing ones | after a deploy, and when diagnosing |

`/readyz` checks four things, and reports each as a plain boolean:

| Check | What it means when false |
|---|---|
| `database` | the control database is not writable: disk full, wrong ownership, or a stuck writer |
| `schema` | migrations have not been applied to the latest version |
| `templates` | a required website template is missing from `STARLING_WEB_DIR` |
| `config` | in production, a configuration problem remains; the start-up log names it |

`/readyz` does **not** check free disk space, backups or the AI provider. The owner dashboard does.

### The owner dashboard

`/admin`, behind a CLI grant, an owner email, an authenticator and a 15-minute elevation.

| Page | What it shows |
|---|---|
| `/admin` | monthly recurring revenue, subscribers per plan, credits used today and over 7 and 30 days, AI spend against the daily and monthly budgets, the failure rate over the last 24 hours, the webhook queue, current alerts, the last backup, and free space on the data volume |
| `/admin/customers` | search; each customer's plan, status and usage |
| `/admin/customers/{id}` | one customer, with grant credits, suspend, unsuspend, revoke devices and resync billing |
| `/admin/failures` | refusals and errors by route and code, from `request_errors` |
| `/admin/webhooks` | stored Stripe events, filtered by received, processed or failed |
| `/admin/audit` | every admin action: who, what, which account, parameters, IP prefix, time |
| `/admin/ai` | a connection test, and pause/resume for drafting |

Every figure is an estimate for the owner. Stripe's and the AI provider's own invoices are the
record. Dashboard queries read metadata, counts, costs and error codes only: never a customer's
drafts, prompts, writing samples or webhook payloads.

### Alerts

Alerts appear in three places: the dashboard's overview, the `alerts` command, and email to
`ALERT_EMAIL`. Each alert key is emailed at most once every 6 hours, and nothing is emailed at all
while `ALERT_EMAIL` is empty.

```
python -m starling_server.cli alerts            # add --days N, or --json
```

| Alert | Raised when | Section below |
|---|---|---|
| `alert:billing:<kind>:<ref>` | a Stripe event or object Starling cannot resolve: unknown price, unknown customer, duplicate subscriptions, partial refund, lost dispute, an event that exhausted its retries, a webhook from the wrong Stripe mode | [6](#6-stripe-webhooks-and-billing) |
| `alert.engine_circuit.<n>` | one customer's engine crashed three times within ten minutes | [7](#7-engine-circuit-breaker) |
| `alert:ai_key_rejected` | the AI provider rejected a key (401/403) and the proxy fell back | [3](#3-ai-key-rejected) |
| `backup:failed` | the last scheduled backup run failed (production only) | [9](#9-backups-verification-and-restore) |
| `backup:missing` | no scheduled run recorded for more than 26 hours (production only) | [9](#9-backups-verification-and-restore) |
| `disk:low` | less than 15% free on the data volume; an error below 5% | [8](#8-disk-space) |

Alert texts carry metadata only (kinds, references, tenant numbers, HTTP statuses), never customer
content.

### Logs

- In production the gateway writes **JSON log lines**. An exception logs its class name and source
  line, never the traceback text and never the exception message.
- **HTTP access logs are off**, on purpose: query strings carry email-verification and
  password-reset tokens. Do not add `UVICORN_*` variables anywhere to turn them back on.
- Logs never contain prompts, drafts, tokens, passwords or email bodies. Neither does `ai_calls`,
  which records tokens, cost, status and timing only.
- `LOG_LEVEL` defaults to `INFO`. Read them with `fly logs` or `docker compose logs`.

Because logs are thin by design, the dashboard's failures page and the `request_errors` table are
your main diagnostic surface: one row per minute, per account, per route template, per code, with a
count, never a URL, body or token.

### A weekly routine

| When | Do |
|---|---|
| Daily, briefly | open `/admin`: AI spend against budget, failure rate, webhook queue, alerts |
| Weekly | `backup-status`; copy an archive off the machine; check free disk space; `usage-verify` (the gateway runs the same check daily and raises an alert, this prints the full list) |
| Monthly | `stripe-verify`; check AI spend against the provider's own invoice; check the extension's store listing is still live |
| Quarterly | rehearse a restore into a scratch target; review who holds which secret |

## 2. Service pause and resume

The one big lever. It stops all drafting without stopping the service.

```
python -m starling_server.cli ai-pause --reason "provider outage" \
  --message "Drafting is paused while we fix a problem." --retry-after-s 600
python -m starling_server.cli ai-resume --reason "provider recovered"
```

- `--reason` is required and goes in the audit log. `--message` is what customers see; without it
  they get a standard short notice. `--retry-after-s` is 1 to 86,400 seconds, default 300.
- Paused requests are refused with 503 `service_paused` and a `Retry-After` header; `service.status`
  in the session becomes `paused`, so the extension and the account page both say so.
- The same pause and resume buttons are on `/admin/ai`.
- A malformed pause value is treated as paused, retrying after 300 seconds. That is fail-safe, not a
  bug.

Pause when: the provider is down and every draft is failing; spend is running away and you need
minutes to think; you are restoring data.

## 3. AI key rejected

**Symptom.** `alert:ai_key_rejected`, and drafts failing with `ai_auth` (HTTP 502) if both keys are
bad.

**What the proxy already did.** On a 401 or 403 with the primary key it retried once with
`AI_API_KEY_SECONDARY` if one is configured, raised the alert, and now prefers the secondary key for
10 minutes.

**Do this.**

1. `python -m starling_server.cli services-status`: it shows masked key status and upstream health.
2. Check the key in the provider's dashboard. The usual causes are a revoked key, a key deleted with
   a project, or a key pasted with whitespace.
3. Create a new key. Set `AI_API_KEY` to it and `AI_API_KEY_SECONDARY` to the old one if the old one
   still works; restart the services process (on Fly.io, setting a secret restarts the machine).
4. `python -m starling_server.cli ai-check` to confirm every configured model answers.
5. Once no fallback alerts have appeared for a day, revoke the old key and clear
   `AI_API_KEY_SECONDARY`.

**If the key leaked:** revoke it in the provider's dashboard first, before anything else. Drafts fail
until a valid key is in place, unless a good `AI_API_KEY_SECONDARY` is already set.

## 4. Provider quota or spend limit reached

**Symptom.** Drafts fail with `ai_quota` (HTTP 502). Starling **never retries** these: a retry cannot
help and only burns time.

**Do this.**

1. Open the provider's usage page. Distinguish two cases:
   - **Spend limit**: you set a hard monthly limit and reached it. Raise it, or wait for the month
     to roll over.
   - **Usage tier limit**: the account's tier caps monthly usage. It rises with payment history;
     until then, your ceiling is the tier's.
2. While you decide, `ai-pause` so customers get a clear "paused" message instead of repeated
   failures.
3. Fix the order of limits so this becomes a Starling pause rather than a provider refusal:

   **`AI_MONTHLY_BUDGET_USD` ≤ provider hard spend limit ≤ tier monthly limit.**

   The defaults ($10 a day, $90 a month) sit under usage tier 1's $100 monthly limit
   (<https://developers.openai.com/api/docs/guides/rate-limits>, checked 2026-09-18). Raise both
   together as the tier rises, never one alone.

## 5. Global budget exhausted

**Symptom.** Everyone gets 503 `service_paused` with a `Retry-After`; the dashboard shows spend at or
above the daily or monthly budget.

Free-plan accounts stop earlier than paid ones: once the day's global spend reaches
`AI_FREE_TRIP_PCT` (default 80) percent of the daily budget, free accounts are refused while paid
accounts keep drafting. That is the intended behaviour, not a failure.

**Do this.**

1. Decide whether the spend is legitimate growth or one account misbehaving. Check
   `/admin/customers` sorted by usage, and the fair-use ceilings: a single account cannot exceed its
   plan's ceiling ($0.30 Free, $4.50 Starter, $9.00 Pro per period), so a budget blown by one account
   means the ceilings are too high for your traffic, not that a customer escaped them.
2. A stop lasts until the end of its window: UTC midnight for the daily budget, the first of the
   next month for the monthly one. To clear it early, raise the budget **and restart the services
   process**: at start-up it clears stops on current windows whose spend is below the new limit.
3. A budget of zero allows no spend at all. If drafting stops the instant you deploy, check for an
   accidental `AI_DAILY_BUDGET_USD=0`.
4. Raising a Starling budget without also raising the provider's hard limit just moves the failure
   from a clean pause to an `ai_quota` error. Raise both.

## 6. Stripe webhooks and billing

**Symptom.** `/admin/webhooks` shows events in `failed`, or a billing alert arrives.

**How the queue works.** Starling verifies the signature, stores the event and answers 200 at once.
A single serial worker then refetches the latest objects from Stripe, updates its mirror and
recomputes entitlement. A failed event retries with backoff from 30 seconds doubling to 1 hour, at
most 20 attempts, then raises `event_retries_exhausted`. On start-up, `received`, `processing` and
`failed` rows are re-queued. An hourly reconciler lists the last 30 days of events, and a daily job
resyncs every customer.

**Do this.**

1. Open `/admin/webhooks` and read the stored error on the failed event.
2. Recover everything in one pass:

   ```
   python -m starling_server.cli stripe-resync
   ```

   It recovers missed events from the last 30 days, resyncs every customer, then processes pending
   events. It prints how many events it recovered, the resync summary and what it processed, and
   exits 1 if anything is still failed.
3. For one customer, use **Resync billing** on their dashboard page instead: same code path, one
   account.
4. Check the endpoint itself in the Stripe dashboard. Common causes:
   - the endpoint is on the wrong API version: it must be `2026-08-26.dahlia`;
   - the event list is not the exact 16 types in [PAYMENTS.md](PAYMENTS.md#webhook-endpoint);
   - the signing secret was rolled and `STRIPE_WEBHOOK_SECRET` was not updated;
   - the endpoint points at a sandbox while the service runs live, or the reverse. A webhook from the
     wrong mode gets 400, is not stored, and raises `livemode_mismatch`.
5. Without a webhook secret the route answers 503, and every delivery fails. Check that first if
   *every* event is failing.

**Alerts that need a decision, not a retry:**

| Alert kind | What it means | What to do |
|---|---|---|
| `unknown_price` | a subscription is on a price Starling cannot map | the customer has dropped to Free. Run `stripe-setup`, or map the price by putting the plan back in `plans.toml` |
| `duplicate_subscriptions` / `duplicate_subscriptions_canceled` | one account has more than one entitling subscription | the highest plan wins; the resync cancels the others and credits unused time |
| `partial_refund` | a partial refund happened; **Starling changed nothing** | decide manually whether to adjust the plan or grant credits |
| `dispute_lost` | a dispute closed against you | the subscription is cancelled and the account stays suspended |
| `paid_invoice_without_access` | money arrived for a subscription that had already ended (a customer paid an old invoice after cancelling, or after choosing a different plan) | the account got nothing for it. Refund the invoice in the Stripe Dashboard, or reinstate the customer by hand |
| `checkout_customer_mismatch` / `subscription_account_mismatch` | a Checkout Session or a subscription names an account other than the one that owns the Stripe customer | nobody was entitled by the named account; the customer's own account decides. Look at who created it in Stripe |
| `event_retries_exhausted` | 20 attempts failed | read the stored error; fix the cause; `stripe-resync` |

## 7. Engine circuit breaker

**Symptom.** `alert.engine_circuit.<n>`; one customer gets 503 `ai_unavailable` with a `Retry-After`.

**What happened.** That customer's engine crashed or failed to start three times within ten minutes.
Restarts back off 1 s, 5 s, 30 s; after the third crash the breaker opens for that customer only.
Nobody else is affected.

**Do this.**

1. `python -m starling_server.cli engines-status`: the customer's engine state, memory and restarts.
2. Read the log lines around the crash. The supervisor kills an engine that passes
   `ENGINE_MEMORY_HARD_MB` (default 768 MB); an engine over `ENGINE_MEMORY_SOFT_MB` (512 MB) is
   recycled once it is idle, which is normal and not a crash.
3. Stop the engine cleanly and let it start fresh on the next request:

   ```
   python -m starling_server.cli engines-stop --account <account uuid>
   ```

4. If it is a memory problem, raise the soft and hard limits together (hard must be at least soft)
   and lower `ENGINE_MAX_LIVE` to match the machine, or move to a bigger machine.
5. If one customer's data is corrupt, their folder is `<data>/tenants/<n>/`. Their export
   (`POST /account/export`) is the safe way to get their content out before you touch anything.

**Every engine busy** is a different thing and does not open the breaker. The pool evicts the least
recently used *idle* engine to make room (it exits while the new one starts, so for that moment the
machine holds one engine more than `ENGINE_MAX_LIVE`); only when all `ENGINE_MAX_LIVE` engines are actively
serving does a new customer get 503 `ai_unavailable` with `Retry-After: 10`. The customer sees "could
not finish this draft, please try again". If that happens often, raise `ENGINE_MAX_LIVE` on a bigger
machine or lower `ENGINE_IDLE_SECONDS` so engines free up sooner.

## 8. Disk space

**Symptom.** `disk:low` (below 15% free) or the error version (below 5%). Nothing else watches free
space, and `/readyz` does not.

**Do this.**

1. What grows: backup archives in `BACKUP_DIR`, per-customer folders in `<data>/tenants/`, and the
   control database.
2. Prune archives to the retention rules (7 daily, 4 weekly, 3 monthly):

   ```
   python -m starling_server.cli backup-prune --dry-run
   python -m starling_server.cli backup-prune
   ```

3. Copy archives off the machine and keep fewer locally, if the archives are what is filling the
   volume.
4. Grow the volume. On Fly.io a volume can be extended; you pay for provisioned capacity, not used.
5. A full volume is the one condition that breaks everything at once: SQLite cannot write, so
   `/readyz` reports `database: false`, sign-in fails and backups fail. Act on the warning at 15%,
   not the error at 5%.

## 9. Backups, verification and restore

The mechanics are in [`deploy/README.md`](../deploy/README.md#backups). What matters operationally:

- A backup runs **every night at 03:00 UTC** inside the supervisor, verifies itself, and prunes to 7
  daily, 4 weekly and 3 monthly archives. If the machine was down at 03:00, a catch-up run starts 5
  minutes after start-up.
- The archive is a compressed tar with a manifest of sizes and SHA-256 hashes, encrypted with
  AES-256-GCM under a key derived from `BACKUP_PASSPHRASE` with scrypt. Tampering, truncation and a
  wrong passphrase are all detected.
- Root never opens a database in a folder another user can write to: each SQLite file is read by a
  short helper process running as the file's owner. A customer file not owned by that customer's
  engine user is left out and listed as skipped.
- **Without the passphrase no archive can be read. There is no recovery.** Keep it in a password
  manager, outside the server.

```
python -m starling_server.cli backup-status                       # exits 1 if the last run failed
python -m starling_server.cli backup
python -m starling_server.cli backup-verify --archive /data/backups/<name>.sbk
```

**Copy archives off the machine at least weekly.** An archive on the same volume does not survive
losing the volume, and the reference host's own documentation warns that a volume lives on one
physical server. Verify each off-site copy with `backup-verify` on a machine that has the passphrase.

**Restoring** stops the service. Use maintenance mode (`STARLING_MAINTENANCE=1`), which starts
nothing at all (no gateway, services, engines or nightly backup), so the volume can be restored in
place. The step-by-step is in
[`deploy/README.md`](../deploy/README.md#restore). Rehearse it once before you need it: a restore you
have never run is not a backup strategy.

## 10. Outdated extensions

**Symptom.** Customers report the extension refusing to work; the failures page shows 426
`extension_outdated`.

A missing, unreadable or older `X-Starling-Extension` header gets 426. The minimum is
`STARLING_EXTENSION_MIN_VERSION`.

**The safe order when you change what the extension must do:**

1. Publish the new extension to the Chrome Web Store and wait for it to be live and rolled out.
2. Only then raise `STARLING_EXTENSION_MIN_VERSION` on the server.

Reversing that order locks out every customer until the store review finishes, and reviews take a
few days and can take a few weeks (<https://developer.chrome.com/docs/webstore/review-process>,
checked 2026-09-18).

The same rule applies to the disclosure: publish the extension that shows the new disclosure first,
then raise `STARLING_CONSENT_VERSION` and `STARLING_EXTENSION_MIN_VERSION` together. The server
accepts only an exact match of `STARLING_CONSENT_VERSION` on `POST /api/v1/consent`.

If you must roll back, lowering `STARLING_EXTENSION_MIN_VERSION` takes effect on the next deploy;
the store version cannot be un-published as quickly.

## 11. Rotating keys and secrets

| Secret | Downtime | Procedure |
|---|---|---|
| `AI_API_KEY` | none | [section 3](#3-ai-key-rejected) and [AI-PROVIDER.md](AI-PROVIDER.md#api-keys-and-rotation) |
| `STRIPE_SECRET_KEY` | none | [PAYMENTS.md](PAYMENTS.md#rotating-secrets) |
| `STRIPE_WEBHOOK_SECRET` | none | [PAYMENTS.md](PAYMENTS.md#rotating-secrets), using `STRIPE_WEBHOOK_SECRET_PREVIOUS` |
| `SMTP_PASSWORD` | none | new credential in the provider, set, redeploy |
| `STARLING_SECRET_KEY` | see below | overlap window with `STARLING_SECRET_KEY_PREVIOUS` |
| `STARLING_SERVICES_TOKEN_KEY` | brief | restart order below |
| `BACKUP_PASSPHRASE` | none, but see below | new passphrase, then keep the old one |

### `STARLING_SECRET_KEY`

This key signs web sessions and CSRF tokens and encrypts stored authenticator secrets. It also keys
the deletion tombstones that stop a deleted address getting a fresh free allowance.

1. Generate a new key. Set `STARLING_SECRET_KEY` to it and `STARLING_SECRET_KEY_PREVIOUS` to the old
   one, then redeploy. Existing sessions, CSRF tokens and encrypted authenticator secrets keep
   working; each value is re-encrypted with the current key the next time it is used.
2. Leave the overlap in place for about 30 days, then clear `STARLING_SECRET_KEY_PREVIOUS` and
   redeploy. Sessions older than the overlap end, and people sign in again.
3. **Know what you lose.** Tombstones are keyed by an HMAC of the old secret, so after the rotation
   older rows can no longer be matched: an address whose account was deleted before the rotation can
   sign up again and get a full free allowance. That is an acceptable trade for a compromised key and
   a bad reason to rotate on a schedule.

### `STARLING_SERVICES_TOKEN_KEY`

The gateway, the services process and the supervisor share it; engines never hold it, only tokens
signed with it.

Set the new value and restart the whole container. Because all three processes restart together,
there is no window where one has the new key and another the old. Engines get a freshly signed token
when they next start; tokens signed with the old key stop working, so in-flight drafts fail and
retry. There is no previous-key overlap for this setting.

### `BACKUP_PASSPHRASE`

Changing it only affects **new** archives. Every existing archive still needs the old passphrase, so
keep both in the password manager until the last archive encrypted with the old one has been pruned,
at least three months under the default retention. Write down which passphrase covers which dates.

## 12. Scaling: what this design can and cannot do

**One machine. One gateway worker. This is not tunable.**

Per-account concurrency slots and the engine pool live in the gateway process's memory, and the
control database is SQLite with a single writer thread on a volume that belongs to one machine. A
second worker or a second machine breaks both silently: double-charged drafts, orphaned engines,
database contention. `deploy/fly.toml` and `deploy/docker-compose.yml` both warn against it, and the
supervisor starts uvicorn with exactly one worker.

What you *can* scale:

| Lever | Default | Effect |
|---|---|---|
| Machine size | `shared-cpu-2x`, 4 GB | more memory means more concurrent engines |
| `ENGINE_MAX_LIVE` | 6 | engines running at once. Budget about 512 MB each (soft limit), plus the gateway and services |
| `ENGINE_IDLE_SECONDS` | 1800 | lower frees slots sooner at the cost of more cold starts |
| `ENGINE_MEMORY_SOFT_MB` / `_HARD_MB` | 512 / 768 | hard must be at least soft |
| Fly proxy concurrency | soft 150, hard 250 | past the hard limit the proxy queues instead of overloading the machine |
| Plan rate limits | per minute and per day, in `plans.toml` | read live on every request, so a change takes effect at once |

Sizing, honestly: **memory per engine has not been measured under production load**, and neither has
storage per customer. The engine's own development measurement (about 143 MB resident after a health
check on a Windows machine) is indicative only; production engines run on Linux and hold customer
data. Treat 6 engines on 4 GB as a starting point and watch the dashboard.

**When to move to a bigger machine.** When customers regularly see `ai_unavailable` with
`Retry-After: 10` (every engine busy), or when engines are being recycled for memory during normal
use. Moving up is a `[[vm]]` change in `fly.toml` and a redeploy, plus a matching `ENGINE_MAX_LIVE`.
For example, `shared-cpu-4x` with 8 GB is $42.79 a month in Amsterdam against $21.40 for
`shared-cpu-2x` with 4 GB (<https://fly.io/docs/about/pricing/>, checked 2026-09-18; prices vary by
region).

**What genuinely does not scale.** Beyond roughly one machine's worth of concurrent drafting, the
design needs changing, not configuring: the control database would have to move off SQLite, the
engine pool out of process memory, and the per-account slot logic into shared state. That is a
rewrite of the gateway's core, not a deployment change. Plan for it as a project if Starling grows
past a single busy machine.

## 13. What has not been tested

The container image has been built and tested ([TESTING.md](TESTING.md) records those runs), and the
author ran Starling on Fly.io as a free public beta in autumn 2026: the Anthropic provider with Claude
Haiku 5.5, web search off, the free-only plan file and Stripe not configured. Known gaps:

- Real Stripe payments. They were never taken: the billing code has only run against the stand-in
  Stripe in this repository, never against Stripe itself, in a sandbox or live.
- The OpenAI provider and web search, which the beta did not use. All local verification used mocked
  providers; [TESTING.md](TESTING.md#mocked-versus-live) lists what each area still needs as a live
  check.
- Any restore on a real volume; the restore path is covered by automated tests only.
- Behaviour under real concurrent load, and the memory and storage figures that follow from it.
