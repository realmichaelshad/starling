# Deployment

Last reviewed: 2026-09-18.

Zero to a running Starling service. The reference host is Fly.io; the container runs anywhere that can
give it one always-on machine and one persistent encrypted volume.

**How far this page has been exercised.** The container image has been built and tested
([TESTING.md](TESTING.md) records those runs). The author ran Starling on Fly.io as a free public beta
in autumn 2026: the Anthropic provider with Claude Haiku 5.5, web search off, the free-only plan file
(`backend/server/config/plans-free.toml`) and Stripe not configured. That beta has ended and no hosted
Starling service is running now. Real Stripe payments were never taken: the billing code has only run
against the stand-in Stripe in this repository, never against Stripe itself. Expect to iterate on any step your own setup uses
that the beta did not.

Runbooks for day-to-day operation (backups, restore, the trusted-proxy check, the first-run
checklist) live in [`deploy/README.md`](../deploy/README.md). This page does not repeat them; it
links to them at the right moment.

## The shape of the deployment

One container, one machine, one volume.

- The **supervisor** is the container's first process (as root, under `tini`). It creates the data
  folders, starts the gateway and the services process under their own operating-system users, starts
  one engine per active customer under a per-customer user, and runs the nightly backup thread.
- The **gateway** listens on `0.0.0.0:8080` behind the host's TLS proxy, as exactly **one** uvicorn
  worker.
- `/data` holds the control database, every customer's folder and the backup archives.

**Do not scale out.** Per-account concurrency slots and the engine pool live in the gateway
process's memory, and the control database is SQLite with a single writer on a volume that belongs to
one machine. More than one machine breaks both. `deploy/fly.toml` and `deploy/docker-compose.yml`
both say so, and the limit is a design property, not a configuration choice.

## Before you start

| You need | Where it comes from |
|---|---|
| A domain name | your registrar |
| A hosting account (reference: Fly.io) | [COSTS-AND-ACCOUNTS.md](COSTS-AND-ACCOUNTS.md) |
| An API key for the AI provider: OpenAI by default, or Anthropic | [AI-PROVIDER.md](AI-PROVIDER.md) |
| A Stripe account, sandbox first, if you offer a paid plan | [PAYMENTS.md](PAYMENTS.md) |
| An SMTP provider and a verified sending domain | [COSTS-AND-ACCOUNTS.md](COSTS-AND-ACCOUNTS.md) |
| Your business and legal details | [CONFIGURATION.md](CONFIGURATION.md#business-and-legal-details) |
| The three generated secrets | [CONFIGURATION.md](CONFIGURATION.md#generating-the-secrets) |

**Stripe is optional.** Production requires it only when the plans file has a paid plan. To run
free-only, set `PLANS_FILE` to the free-only plans file (`backend/server/config/plans-free.toml` in
the repository, `/app/backend/server/config/plans-free.toml` inside the image), leave the `STRIPE_*`
secrets unset and skip Step 7.

If you offer paid plans, deploy with the **Stripe sandbox** first and switch to live keys only after
the sandbox end-to-end test passes. The extension can be published later: the service runs without
it, and you add the published extension id to `STARLING_EXTENSION_IDS` afterwards.

## Step 1: build and check the image locally

From the repository root:

```
scripts\lock-requirements.ps1          # Windows; scripts/lock-requirements.sh on Linux and macOS
docker compose -f deploy/docker-compose.yml --profile test run --rm --build starling-test
docker compose -f deploy/docker-compose.yml up --build
```

- The first command regenerates `backend/requirements.lock`, the hashed Linux lock the image installs
  with `--require-hashes --only-binary=:all:`. It needs Docker. The lock **is** in the repository
  (last changed 2026-10-30: 47 packages, 1,269 hashes); regenerate it only when `requirements.in`
  changes, review the diff, and run the test target before committing it.
- The second runs both test suites inside the image as root, so the Linux-only privilege-separation
  tests run too.
- The third starts the service locally at <http://127.0.0.1:8765> with `.env` from the repository
  root. Keep `STARLING_ENV=development` locally.

Then run the release gate on the unpacked image filesystem, as `deploy/README.md` describes.

## Step 2: create the app, the volume and the machine

On Fly.io, from the repository root (the build context must be the repository root):

```
fly apps create <your-app-name>
fly volumes create starling_data --region <region> --size 20
fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile
```

Copy `deploy/fly.toml` and replace every `[[PLACEHOLDER]]`: `[[FLY_APP_NAME]]`, `[[FLY_REGION]]` and
`[[PUBLIC_HOSTNAME]]`.

What `deploy/fly.toml` already sets, and why:

| Setting | Value | Why |
|---|---|---|
| `[[vm]] size` / `memory` | `shared-cpu-2x`, `4gb` | fits `ENGINE_MAX_LIVE = 6` engines at 512 MB soft each, plus the gateway and services |
| `auto_stop_machines` | `"off"` | a stopped machine would kill engines mid-request, drop Stripe webhooks and halt the billing queue, the reservation sweeper, the news refresh and the nightly backup |
| `min_machines_running` | `1` | always on |
| `kill_timeout` | `"30s"` | engines get 10 s to finish, then services and the gateway stop |
| `force_https` | `true` | the gateway refuses to run in production on a non-https public URL anyway |
| `[http_service.concurrency]` | soft 150, hard 250 | streams stay open up to 240 s; past the hard limit the proxy queues rather than overloading the machine |
| `[[http_service.checks]]` | `GET /healthz`, 30 s, 60 s grace | `/healthz` always answers 200 when the gateway is up |

**Volume size.** 20 GB is the reference. It bills on *provisioned* capacity, not used
(<https://fly.io/docs/about/pricing/>, checked 2026-09-18), and per-customer storage has not been
measured, so start at 20 GB and watch the dashboard's free-space figure. The `disk:low` alert fires
below 15% free and becomes an error below 5%.

**A caveat to know about.** Fly's own volumes documentation warns: "Always provision at least two
volumes per app", because a volume is a slice of an NVMe drive on one physical server and is tied to
that hardware (<https://fly.io/docs/volumes/overview/>, checked 2026-09-18). Starling **cannot**
follow that advice: one SQLite writer, one machine. Your protection is the nightly encrypted backup
plus off-site copies, not redundancy. Treat copying archives off the machine as mandatory, not
optional.

## Step 3: DNS and TLS

Fly.io terminates TLS for you and issues the certificate; you only publish DNS records.

1. Allocate addresses and note them:

   ```
   fly ips allocate-v6
   fly ips allocate-v4 --shared      # or, without --shared, a dedicated IPv4 at $2 per month
   fly ips list
   ```

   Shared IPv4 is recommended unless you have a specific need for your own
   (<https://fly.io/docs/networking/services/>, checked 2026-09-18). The $2 per month dedicated IPv4
   price is from <https://fly.io/docs/about/pricing/> (checked 2026-09-18).

2. Add the certificate:

   ```
   fly certs add app.your-domain.example
   fly certs check app.your-domain.example
   ```

3. Publish the DNS records your certificate needs
   (<https://fly.io/docs/networking/custom-domain/>, checked 2026-09-18):

   | Record | When |
   |---|---|
   | `A` and `AAAA` to the app's IPs | recommended, and required for an apex domain |
   | `CNAME` to the app's `.fly.dev` hostname | an alternative for a subdomain such as `app.` or `www.` |
   | `CNAME` `_acme-challenge` | needed for a wildcard certificate, or to issue a certificate before traffic arrives |
   | `TXT` `_fly-ownership` | domain ownership verification, when Fly asks for it |

   Validation uses the TLS-ALPN challenge, falling back to HTTP-01 automatically, or DNS-01 through
   the `_acme-challenge` record. The first 10 single-hostname certificates per organization are free,
   then $0.10 per month each (<https://fly.io/docs/about/pricing/>, checked 2026-09-18).

4. `fly certs check` until it reports the certificate is issued, then open
   `https://app.your-domain.example/healthz`. It must answer `{"ok": true}`.

Set `STARLING_PUBLIC_URL` to exactly this origin, with no trailing slash. It is used for links,
emails, redirects, the sitemap, the Stripe return URL and the session cookie name.

## Step 4: set the secrets and environment

Secrets go in the platform's secret store, never in `fly.toml` and never in the image. Setting a
secret restarts the machine (<https://fly.io/docs/apps/secrets/>, checked 2026-09-18), so set them
in as few commands as you can.

```
fly secrets set \
  STARLING_SECRET_KEY=... \
  STARLING_SERVICES_TOKEN_KEY=... \
  BACKUP_PASSPHRASE=... \
  AI_API_KEY=... \
  STRIPE_SECRET_KEY=... STRIPE_WEBHOOK_SECRET=... STRIPE_ACCOUNT_ID=... \
  SMTP_HOST=... SMTP_PORT=587 SMTP_USERNAME=... SMTP_PASSWORD=... SMTP_FROM=... \
  ALERT_EMAIL=...
```

Leave out the three `STRIPE_*` values if you run free-only.

Non-secret settings can live in `fly.toml` under `[env]`, next to the ones already there
(`STARLING_ENV`, `STARLING_ISOLATION`, `STARLING_DATA_DIR`, `STARLING_PUBLIC_URL`, `BACKUP_DIR`,
`EMAIL_MODE`, `STRIPE_MODE`, `LOG_LEVEL`, and the two services addresses,
`STARLING_SERVICES_LISTEN` and `STARLING_SERVICES_URL`, which must stay on loopback TCP on any host
that runs with OS isolation: [CONFIGURATION.md](CONFIGURATION.md) has the reason):

```
STARLING_SUPPORT_EMAIL, STARLING_OWNER_EMAILS, STARLING_EXTENSION_IDS,
STARLING_EXTENSION_MIN_VERSION, STARLING_CONSENT_VERSION,
OPERATOR_COMPANY_NAME, OPERATOR_REGISTERED_ADDRESS, OPERATOR_CONTACT_EMAIL, OPERATOR_PRIVACY_CONTACT,
OPERATOR_JURISDICTION, OPERATOR_GOVERNING_LAW_VENUE, OPERATOR_EFFECTIVE_DATE,
HOSTING_PROVIDER, EMAIL_PROVIDER, AI_SUBPROCESSORS, RETENTION_PERIODS, REFUND_POLICY
```

Every one of these is described in [CONFIGURATION.md](CONFIGURATION.md). Production will not start
while any `[[PLACEHOLDER]]` remains, while the public URL is not https, while isolation is not `os`,
while either key is missing, while email is not SMTP, or while the extension id and owner email lists
are empty. That is deliberate: a misconfigured production service fails loudly at start-up rather
than quietly serving a broken page.

**Leave `STARLING_EXTENSION_IDS` for last if the extension is not published yet**, but remember the
gateway will refuse to start in production without it. If you want the website live before the store
listing, deploy with the id of an unpacked development build, then replace it with the published id.

Do not add `UVICORN_*` variables anywhere. The supervisor alone controls workers, proxy headers and
access logging, and access logs are off on purpose: query strings carry email-verification and
password-reset tokens.

## Step 5: client IP addresses

Until `STARLING_TRUSTED_PROXIES` is right, every request looks like it comes from the proxy, so all
customers share one rate limit and the session list shows a host address instead of the customer's
own network.

Follow the five-step first-deploy check in
[`deploy/README.md`](../deploy/README.md#client-ip-addresses-on-flyio-starling_trusted_proxies). It
uses `fly ips list` and `python -m starling_server.cli proxy-peers`, and it ends with you signing in
and confirming the session list shows your own network. Never list `*`, `0.0.0.0/0` or all of
`fdaa::/16`.

## Step 6: the database and the first owner admin

The gateway applies migrations on start-up, but run them once yourself so you see the result:

```
fly ssh console -C "python -m starling_server.cli migrate"
fly ssh console -C "python -m starling_server.cli db-status"
```

Then create the owner account. Admin access needs **all four** of a CLI grant, a verified email
listed in `STARLING_OWNER_EMAILS`, an enrolled authenticator, and an elevation in the last 15
minutes.

1. **Sign up on the website** with an address that is in `STARLING_OWNER_EMAILS`, and verify it from
   the email. `admin-grant` refuses an account that does not exist, is unverified, is not active, or
   is not on that list.
2. **Grant admin:**

   ```
   fly ssh console -C "python -m starling_server.cli admin-grant --email you@your-domain.example"
   ```

   It prints `admin-grant: done for account <uuid>`, or `admin-grant: refused: <reason>` and exits 1.
3. **Enrol the authenticator.** Sign in on the website and open `/admin`. You are sent to
   `/admin/totp`, which asks for your password plus a code from your authenticator app.

   There is **no QR code and there are no recovery codes.** Scan or type the secret into your
   authenticator carefully, and keep a copy of the secret in your password manager before you
   finish. If you lose the authenticator, the only route back is
   `admin-revoke --email ...` followed by `admin-grant --email ...` and enrolling again.
4. **Elevate.** Every admin POST needs an elevation from the last 15 minutes; `/admin/elevate` asks
   for a fresh code. Every admin action writes an audit row.

Add a second owner address and enrol it too, so one lost phone does not lock you out of your own
dashboard.

## Step 7: Stripe

Skip this step if your plans file has no paid plan: Stripe is required in production only when a paid
plan exists.

Full detail is in [PAYMENTS.md](PAYMENTS.md). The deployment-side sequence:

1. Create the sandbox, create a restricted key, and set `STRIPE_MODE=test`, `STRIPE_SECRET_KEY`,
   `STRIPE_ACCOUNT_ID` and **`STRIPE_ALLOW_TEST_MODE=true`**. Production refuses sandbox keys without
   that last switch. With it, every production check still applies, every page carries a "Payments are
   in test mode" banner, search engines are told not to index the site, and the owner dashboard keeps
   an alert up until you switch to live keys. **Never rehearse by setting `STARLING_ENV=development` on
   a public host**: development mode turns the production checks off, including the isolation between
   customers.
2. Create the products, prices and both customer portal configurations:

   ```
   fly ssh console -C "python -m starling_server.cli stripe-setup"
   ```

   It is idempotent: it finds existing objects by lookup key and setup metadata and never changes the
   amount of an existing price. Put the two portal configuration ids it reports into
   `STRIPE_PORTAL_CONFIGURATION_ID` and `STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID`.
3. **Create the webhook endpoint in the Stripe dashboard** at
   `https://app.your-domain.example/webhooks/stripe`, with API version `2026-08-26.dahlia` and exactly
   the 16 event types listed in [PAYMENTS.md](PAYMENTS.md#webhook-endpoint). Store its signing secret
   as `STRIPE_WEBHOOK_SECRET`. Without a webhook secret the route answers 503.
4. Confirm the wiring:

   ```
   fly ssh console -C "python -m starling_server.cli stripe-verify"
   ```

5. Redeploy so the new settings take effect, then run the sandbox billing test.

In production the gateway compares each Stripe price (amount, currency, interval) with `plans.toml`
at start-up and **refuses to start on a mismatch**. If a deploy fails to come up right after a price
change, that is the first thing to check.

## Step 7b: Sign in with Google (optional)

"Continue with Google" lets customers sign up and sign in without a password or a confirmation
email. It is off until both settings are set, and the site works the same without it.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project for the
   business (or use an existing one).
2. **APIs & Services → OAuth consent screen** (Google Auth Platform → Branding): app name
   "Starling", your support email, your logo, the home page, privacy policy and terms URLs on your
   domain, and your domain under authorized domains. Audience: External.
3. **Data access / scopes:** add only `openid` and `.../auth/userinfo.email`. Starling asks for
   nothing else, and these are not sensitive scopes, so no Google app verification is needed. To show
   your app name and logo on Google's sign-in screen, complete Google's lighter "brand verification"
   (Google Cloud help, "OAuth App Verification", checked 2026-09-22).
4. **Clients → Create client → Web application.** Authorized redirect URI, exactly:
   `https://app.your-domain.example/auth/google/callback`. No JavaScript origins are needed.
5. Copy the client id and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (`fly secrets set`)
   and redeploy.
6. **Publish** the app (Audience → Publish app). While it is in "Testing", only the test users you
   list can sign in.
7. Check it: sign up with a Gmail account you have not used on Starling, sign out, sign in again with
   Google, and connect Google from Security on an account made with a password.

How accounts are matched and joined, and why, is in
[SECURITY-AND-PRIVACY.md](SECURITY-AND-PRIVACY.md#sign-in-with-google).

## Step 8: smoke test

Run these against the deployed service. Everything except the last two rows works without the
extension.

| # | Check | Expected |
|---|---|---|
| 1 | `GET /healthz` | 200, `{"ok": true}` |
| 2 | `GET /readyz` | 200 with `database`, `schema`, `templates` and `config` all true. A 503 lists which check failed |
| 3 | `GET /` and `GET /pricing` | the site renders; plans and prices match `plans.toml` |
| 4 | `GET /privacy`, `/terms`, `/refunds`, `/subprocessors`, `/acceptable-use`, `/contact` | your business details appear, with **no** `[[PLACEHOLDER]]` markers and no draft-legal banner |
| 5 | `GET /robots.txt` | in production it allows the site and disallows `/account`, `/admin`, `/api/` and the private paths |
| 6 | `GET /.well-known/security.txt` | a `Contact:` line with your support address |
| 7 | Sign up with a real address | the verification email arrives; the link verifies the account |
| 8 | Sign in, open `/account` | plan Free, 25 drafts, the period end date |
| 9 | Response headers on any page | `X-Request-Id`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, a `Permissions-Policy`, `Cross-Origin-Opener-Policy: same-origin`, HSTS, and a CSP with no inline script or style |
| 10 | `python scripts/check_site.py --base-url https://app.your-domain.example --env production` | `SITE CHECK: PASS` |
| 11 | Subscribe with a Stripe test card | checkout completes; `/account/billing` shows the paid plan after the webhook is processed; `/admin/webhooks` shows the event as processed |
| 12 | `python -m starling_server.cli ai-check` | each configured model answers |
| 13 | `python -m starling_server.cli services-status` | budgets, masked key status, upstream health and news freshness |
| 14 | `python -m starling_server.cli backup-status` | after the first night, or run `backup` once by hand |
| 15 | Install the extension, sign in, make one draft | the draft streams into X's own text box; `/account` and the extension show the same numbers |
| 16 | `python -m starling_server.cli engines-status` | that customer's engine is listed as running |
| 17 | If Google sign-in is configured: "Sign up with Google" with a fresh Gmail address | back on `/account`, signed in, no confirmation email; Security lists Google as a sign-in method |

Rows 11 to 16 are the ones that need real provider accounts or a published extension. Row 11 applies
only if you offer a paid plan.

## Step 9: going live

| Done | Step |
|---|---|
| ☐ | If you offer paid plans: the sandbox billing test passed end to end ([PAYMENTS.md](PAYMENTS.md#go-live-checklist)) |
| ☐ | Legal pages complete, reviewed by counsel, no placeholders |
| ☐ | The extension is published and its store id is the only id in `STARLING_EXTENSION_IDS` |
| ☐ | `STARLING_CONSENT_VERSION` matches the published extension's disclosure version |
| ☐ | If you offer paid plans: live Stripe restricted key, `STRIPE_MODE=live`, **`STRIPE_ALLOW_TEST_MODE` removed**, live webhook endpoint and its own secret, `stripe-verify` passes, and the "test mode" banner is gone |
| ☐ | A hard monthly spend limit is set in the OpenAI dashboard, above `AI_MONTHLY_BUDGET_USD` and within your usage tier |
| ☐ | `ALERT_EMAIL` is a mailbox somebody reads |
| ☐ | `BACKUP_PASSPHRASE` is stored outside the server, and one restore has been rehearsed |
| ☐ | `STARLING_TRUSTED_PROXIES` verified by the session-list check |
| ☐ | An uptime monitor watches `/healthz` |
| ☐ | If Google sign-in is on: the OAuth app is published (not "Testing"), and its redirect URI is the production domain's `/auth/google/callback` |

## Redeploying and updating

```
fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile
```

Migrations run at start-up. A deploy restarts the machine, which stops every engine: in-flight
drafts fail and the extension shows an error the customer can retry. There is no zero-downtime
deploy for this design, so prefer quiet hours.

If a deploy will not come up, the usual causes are, in order: a configuration problem that production
treats as fatal (the log names it), a Stripe price that no longer matches `plans.toml`, and a missing
or stale `backend/requirements.lock`.

## Other hosts

Nothing in the code is Fly-specific. Any host works that gives you:

- one always-on machine that can run a container as root (the supervisor creates per-customer users);
- one persistent, encrypted volume mounted at `/data`;
- TLS termination in front of port 8080, with the proxy's address in `STARLING_TRUSTED_PROXIES`;
- a secret store;
- a shell into the container, for the operator commands.

`deploy/docker-compose.yml` is the plain-Docker equivalent and is what the local run and the
clean-checkout test use. `no-new-privileges:true` is set there and must not block the supervisor from
switching users; that is on the first-run checklist in `deploy/README.md`.
