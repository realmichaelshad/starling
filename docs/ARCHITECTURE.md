# Architecture overview

Last reviewed: 2026-09-18.

This is a plain overview of how Starling is built. The exact interfaces between components (routes,
message formats, database tables, settings) are defined in `docs/architecture/CONTRACTS.md`, which is
the single source of truth. When the two disagree, CONTRACTS.md wins.

## Components

| Component | What it does | Where it lives |
|---|---|---|
| Chrome extension | Runs on x.com in the person's browser. Shows Starling beside posts and X's text box, sends requests to the service, streams drafts back and places them in the text box for the person to review. It never presses Post. | `extension/` |
| Website and API gateway | One web application (FastAPI). Serves the website, the extension API (`/api/v1`), sign-in, checkout and Stripe webhooks, usage metering and the owner admin pages. It also serves the writing page for phones (`/write`), an installable web app that drafts through the same metered path as the extension. | `backend/server/starling_server/` |
| Supervisor | The container's first process. Starts the gateway and services, and starts and stops one engine per active customer, each under its own operating-system user. | `backend/server/starling_server/supervisor.py` |
| Engine (one per active customer) | Starling's drafting intelligence. Builds prompts, runs the multi-step drafting pipeline and keeps that customer's own data. | `backend/engine/` |
| Services | Two roles in one separate process. The **AI proxy** is the only place the AI provider's key is used; it enforces caps and budgets and records cost. The **world service** refreshes a shared news pool every 15 minutes, fetches public pages and images linked in posts, and serves cached topic summaries. | `backend/server/starling_server/services/` |
| Control database | SQLite in WAL mode at `/data/control/control.db`: accounts, sessions, devices, consents, the Stripe mirror, the usage ledger, AI metering and the admin audit log | `backend/server/starling_server/db/` |
| Configuration | environment variables, the plans file and the AI price file | `.env.example`, `backend/server/config/plans.toml`, `backend/server/config/ai_pricing.json` |
| Website templates and assets | website pages, legal pages, account and admin pages | `web/` |
| Stripe (external) | Checkout, Billing subscriptions, customer portal and webhooks. Needed only when a paid plan exists | [PAYMENTS.md](PAYMENTS.md) |
| AI provider (external) | the AI model: OpenAI by default, or Anthropic | [AI-PROVIDER.md](AI-PROVIDER.md) |
| Email provider (external) | account emails over SMTP | [COSTS-AND-ACCOUNTS.md](COSTS-AND-ACCOUNTS.md) |
| Storage | one persistent, encrypted volume mounted at `/data`: control database, customer folders and backups | host provider |

**What exists today.** Every component above is written and covered by tests that run against mocked
providers: the engine, the gateway (website, API, billing, usage, admin dashboard), the services
process, the supervisor with per-customer operating-system users, the extension, the website, the
deployment files and the operator command line. A GitHub Actions workflow
(`.github/workflows/ci.yml`) runs the three Python suites on the hash-checked lock, and the
extension's typecheck, tests and build, on every push and pull request. The container image has been
built and tested ([TESTING.md](TESTING.md) records those runs).

The author ran Starling as a free public beta on Fly.io in autumn 2026, with the Anthropic provider
(Claude Haiku 5.5), web search off, the free-only plan file and Stripe not configured. That beta has
ended and no hosted Starling service is running now; a copy of the website with sign-up turned off is
at <https://starling-demo-ten.vercel.app>. Real Stripe payments were never taken: the billing code has only run against the
stand-in Stripe in this repository, never against Stripe itself, and the paid plans exist in code and
configuration but were never sold.
Known limitations are in the [README](../README.md#status-and-limitations), and the
mocked-versus-live split is in [TESTING.md](TESTING.md#mocked-versus-live).

## Diagram

```
 PERSON'S COMPUTER                          OPERATOR'S SERVER (one container, one machine)
 +-----------------------------+            +--------------------------------------------------+
 | Chrome on x.com             |  HTTPS     |  gateway: website, /api/v1, billing, admin       |
 |   Starling extension        |----------->|     |                    |                       |
 |                             |<-----------|     | control database   | spawner socket        |
 +-----------------------------+  streamed  |     v                    v                       |
                                   drafts   |  [SQLite on /data]    supervisor                 |
 +-----------------------------+            |                          | starts one engine     |
 | Any browser: website,       |  HTTPS     |                          | per active customer   |
 | sign-up, account, billing   |----------->|                          v                       |
 +-----------------------------+            |     gateway ---> engine A    engine B    ...     |
                                            |     (local sockets)  |           |               |
                                            |                      v           v               |
                                            |              services: AI proxy + world service  |
                                            +---------|----------------------|-----------------+
                                                      |                      |
                                                      v                      v
                                                 AI provider        news feeds and public
                                                                    pages and images

 Stripe: the website redirects customers to Checkout and the customer portal;
         Stripe sends webhooks to <public url>/webhooks/stripe on the gateway.
 Email:  the gateway sends account emails through the SMTP provider.
```

## Main flows

### One draft

1. The extension sends the request with its device token.
2. The gateway checks the account, plan and limits, then reserves one draft.
3. The gateway forwards the request to that customer's engine, starting the engine if needed.
4. The engine builds prompts and calls the AI proxy several times (usually 3–6) with a token that
   names the customer.
5. The proxy calls the AI provider and records cost. Text streams back through the engine and gateway
   to the extension.
6. The gateway charges the draft when the result is usable and an AI call for it succeeded, or when
   the customer stopped it after text had appeared. Everything else is released, even after a
   preview: a draft Starling withheld, and every failure on Starling's side.

Details: [PLANS-AND-USAGE.md](PLANS-AND-USAGE.md) and
[SECURITY-AND-PRIVACY.md](SECURITY-AND-PRIVACY.md#how-data-flows-for-one-draft).

### One draft from a phone

Chrome extensions do not run on phones, so the website's writing page (`/write`) does the extension's
drafting with a website session instead of a device token:

1. The person signs in, accepts the page's own disclosure once, and pastes the post they are answering,
   or its link (the services process reads a linked post through X's published oEmbed endpoint).
2. The page sends one JSON request; the gateway runs it through the same function as the extension's
   `POST /api/v1/assist`, into the engine's Create reply / quote / post pipeline.
3. The person copies the draft, or opens it in X's post box, and presses Post themselves.

The page installs on a home screen (a web app manifest, an Android share target and a service worker
that only shows an offline page). Details: [architecture/CONTRACTS.md](architecture/CONTRACTS.md)
section 10 and [SECURITY-AND-PRIVACY.md](SECURITY-AND-PRIVACY.md#the-same-draft-from-the-writing-page-phones).

### Extension sign-in

1. The extension opens the website's `/connect` page through Chrome's identity API.
2. The person signs in and approves.
3. The extension exchanges a one-time code, with PKCE, for a device token.

Details: [SECURITY-AND-PRIVACY.md](SECURITY-AND-PRIVACY.md#extension-sign-in).

### Subscription

1. The website creates a Stripe Checkout session, and the customer pays on Stripe's page.
2. Stripe sends webhooks.
3. The gateway stores each event, refetches the latest objects and grants the paid allowance when
   the invoice is paid.

Details: [PAYMENTS.md](PAYMENTS.md).

## Background work

| Job | When it runs |
|---|---|
| Stop idle engines | after `ENGINE_IDLE_SECONDS` (1800) without use; at most `ENGINE_MAX_LIVE` (6) engines run at once by default, plus one for the moment an evicted engine takes to exit |
| Expire stuck usage reservations | every 30 seconds |
| Process stored Stripe events | continuously, one at a time |
| Stripe reconciliation | hourly, listing the last 30 days of events; full customer resync daily |
| Refresh the shared news pool | every 15 minutes |
| Check usage counters against the ledger | nightly |
| Encrypted backup, verify and prune | nightly at 03:00 UTC, inside the supervisor, to `BACKUP_DIR` on the same volume; a catch-up run starts 5 minutes after start-up if the last archive is more than 26 hours old ([OPERATIONS.md](OPERATIONS.md#9-backups-verification-and-restore)) |
| Prune old failure counts (`request_errors`) | daily, rows older than 30 days |
| Delete stored copies of sent emails | 30 days after sending, in SMTP mode |
| Owner alert emails | checked continuously; each alert key is emailed at most once every 6 hours |

## Environments

| Setting | development | e2e (automated end-to-end tests) | production |
|---|---|---|---|
| `STARLING_ENV` | `development` | `e2e` | `production` |
| Customer isolation (`STARLING_ISOLATION`) | `none`: all processes run as the current user on local ports | `none` | `os`: one operating-system user per engine, communicating over local sockets |
| Email (`EMAIL_MODE`) | `outbox` (stored, not sent) | `outbox` | `smtp` |
| Stripe (`STRIPE_MODE`) | `test` (sandbox) | `test` or a fake Stripe (`STRIPE_API_BASE`) | `live` |
| Public URL | `http://127.0.0.1:8765` by default | test address | https required |

## Deployment shape

Starling runs as a single container on one machine with one persistent volume. The reference host is
Fly.io, with the machine kept always on.

**One machine, one gateway worker, and this is not tunable.** Per-account concurrency slots and the
engine pool live in the gateway process's memory, and the control database is SQLite with a single
writer on a volume that belongs to one machine. Scaling out breaks both.

Step by step from zero to running: [DEPLOYMENT.md](DEPLOYMENT.md). Running it afterwards, including
what can and cannot be scaled: [OPERATIONS.md](OPERATIONS.md#12-scaling-what-this-design-can-and-cannot-do).
Costs: [COSTS-AND-ACCOUNTS.md](COSTS-AND-ACCOUNTS.md).

## Where to read more

| Topic | Document |
|---|---|
| Deploying it | [DEPLOYMENT.md](DEPLOYMENT.md) |
| Every setting | [CONFIGURATION.md](CONFIGURATION.md) |
| Running it | [OPERATIONS.md](OPERATIONS.md) |
| Tests and what is mocked | [TESTING.md](TESTING.md) |
| Interfaces, tables, settings | [architecture/CONTRACTS.md](architecture/CONTRACTS.md) |
| News pool format | [architecture/news-pool-shape.md](architecture/news-pool-shape.md) |
| Security model and data | [SECURITY-AND-PRIVACY.md](SECURITY-AND-PRIVACY.md) |
| AI model and spending limits | [AI-PROVIDER.md](AI-PROVIDER.md) |
| Plans and usage rules | [PLANS-AND-USAGE.md](PLANS-AND-USAGE.md) |
| Stripe | [PAYMENTS.md](PAYMENTS.md) |
| Status and limitations | [README](../README.md#status-and-limitations) |
