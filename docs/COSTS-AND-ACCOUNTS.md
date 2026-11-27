# Costs and accounts

Last reviewed: 2026-09-18. Prices change. Figures marked "verify at purchase time" were not taken
from an official page, or they vary by region, plan or registrar.

## Accounts the operator creates

The operator is whoever runs a Starling instance, and opens each of these accounts in their own
name. None comes with the repository. The author ran Starling as a free public beta on Fly.io in
autumn 2026; that beta has ended and no hosted Starling service is running now.

| Account | What it is for | Needed | Notes and sources |
|---|---|---|---|
| Domain registrar | The website and API address (`STARLING_PUBLIC_URL`) and the email sending domain | Yes | Each instance needs a domain of its own. Check the name you choose for trademark conflicts first. Price: verify at purchase time. |
| Hosting (reference: Fly.io) | Runs Starling's single container (website and API gateway, services, per-customer engines) with a persistent encrypted volume at `/data` | Yes | Pricing https://fly.io/docs/about/pricing/ ; volumes https://fly.io/docs/volumes/overview/ ; always-on settings https://fly.io/docs/launch/autostop-autostart/ . Step by step: [DEPLOYMENT.md](DEPLOYMENT.md). |
| AI provider: OpenAI (the default) or Anthropic | AI for every draft | Yes, one of the two | [AI-PROVIDER.md](AI-PROVIDER.md) |
| Stripe | Subscriptions, checkout and the customer portal | Only when a paid plan exists; the free-only plans file (`backend/server/config/plans-free.toml`) needs no Stripe account | [PAYMENTS.md](PAYMENTS.md) |
| Email provider with SMTP | Verification, password reset and account emails, and owner alerts | Yes; production requires `EMAIL_MODE=smtp` | Resend https://resend.com/pricing ; Postmark https://postmarkapp.com/pricing |
| Chrome Web Store developer account | Publishing the extension | Yes | [EXTENSION-PUBLISHING.md](EXTENSION-PUBLISHING.md) |
| Off-site backup storage | A second copy of encrypted backups outside the hosting provider | Strongly recommended | Fly.io volumes are tied to one physical host, and Fly.io's documentation advises keeping your own backup plan besides its snapshots. Example provider: Cloudflare R2. Archives are copied off with `fly ssh sftp get /data/backups/<archive>.sbk` (or `docker compose cp`); see [`deploy/README.md`](../deploy/README.md#backups) and [OPERATIONS.md](OPERATIONS.md#9-backups-verification-and-restore). |
| Password manager | Keeping secrets, recovery codes and `BACKUP_PASSPHRASE` outside the server | Strongly recommended | Price: verify at purchase time. |
| Uptime monitor | An alert when the site's health check (`/healthz`) stops answering | Optional | Price: verify at purchase time. |

Good practice for every account:

- Open each account for your own business, using an email address the business controls, such
  as one on your own domain. Google notes that a Chrome Web Store developer account's email cannot
  be changed after registration (https://developer.chrome.com/docs/webstore/register).
- Turn on two-factor authentication wherever it is offered.
- Keep credentials only in the password manager and the hosting provider's secret store, never in
  files or chat.

## Fixed monthly costs before any customers

| Item | Monthly cost | Source and notes |
|---|---|---|
| Fly.io machine, shared-cpu-2x with 4 GB memory, always on | $21.40 | https://fly.io/docs/about/pricing/ (checked 2026-09-18), Amsterdam. **Prices vary by region**; check the region you will use. Note that shared-cpu-1x is not offered above 2 GB, so 4 GB means shared-cpu-2x or larger. |
| Fly.io volume, 20 GB (the reference size in [DEPLOYMENT.md](DEPLOYMENT.md)) | $3.00 | $0.15 per GB-month of **provisioned** capacity, used or not (https://fly.io/docs/about/pricing/, checked 2026-09-18). A 10 GB volume would be $1.50. |
| Volume snapshots | $0 for the first 10 GB each month, then $0.08 per GB-month | Daily snapshots, kept 5 days by default and configurable from 1 to 60 with `fly volumes update <id> --snapshot-retention <days>` (https://fly.io/docs/volumes/snapshots/, checked 2026-09-18). Snapshots are not a substitute for Starling's own encrypted archives: anything written between the snapshot and a host failure is lost. |
| TLS certificate for your domain | $0 | The first 10 single-hostname certificates per organization are free, then $0.10 per month each ($1 for a wildcard) (https://fly.io/docs/about/pricing/, checked 2026-09-18). |
| IP addresses | $0 | Shared IPv4 and IPv6 are included, and shared is what Fly recommends. A dedicated IPv4 address costs $2 per month if you ever need one (https://fly.io/docs/about/pricing/, checked 2026-09-18). |
| Outbound data transfer | $0.02 per GB (North America and Europe) | $0.04 in Asia Pacific, Oceania and South America; $0.12 in Africa and India (https://fly.io/docs/about/pricing/, checked 2026-09-18). Starling's text traffic is small. |
| Email | $0 on Resend Free (3,000 emails per month, 100 per day) | Resend Pro is $20 per month for 50,000. Postmark Free is 100 per month, and Postmark Basic is $15 per month for 10,000. |
| Off-site backup storage, example Cloudflare R2 | $0 within 10 GB-month of free storage, then $0.015 per GB-month; no egress fees | https://developers.cloudflare.com/r2/pricing/ |
| OpenAI | $0 fixed | Usage-based; see per-customer costs below. |
| Stripe | $0 fixed | Stripe lists no setup or monthly fees (https://stripe.com/pricing). |
| Domain | verify at purchase time | Usually billed yearly. |
| Password manager | verify at purchase time | |
| Uptime monitor (optional) | verify at purchase time | |
| **Subtotal of sourced items** | **≈ $24.40** | Machine plus a 20 GB volume, Amsterdam prices. |
| **Planning figure** | **≈ $30 per month** | Adds a domain, a password manager and a little headroom. Verify at purchase time. |

Not included: Fly.io's optional support plans (Standard $29 per month, Premium $199 per month;
community support costs nothing). These are optional support tiers, **not** a minimum spend: Fly.io's
pricing page states no minimum organization plan cost, only that a card must be on file
(https://fly.io/plans and https://fly.io/docs/about/pricing/, both checked 2026-09-18).

Starling must run continuously. Its engines, webhook queue, usage sweeper and billing reconciler all
work in the background. New Fly.io apps stop idle machines by default, so set
`auto_stop_machines = "off"` and `min_machines_running = 1`
(https://fly.io/docs/launch/autostop-autostart/). `deploy/fly.toml` already ships both settings.

## One-time costs

| Item | Cost | Source |
|---|---|---|
| Chrome Web Store developer registration | A one-time fee; Google shows the amount during registration. | https://developer.chrome.com/docs/webstore/register |
| OpenAI usage Tier 1 | $5 paid to OpenAI to qualify | https://developers.openai.com/api/docs/guides/rate-limits |
| Domain registration | verify at purchase time | – |
| Trademark clearance and legal review | varies; verify at purchase time | – |

## Per-customer variable costs

| Cost | Amount | Notes and sources |
|---|---|---|
| AI on `gpt-5.6-luna` | About $0.0036 per typical draft and $0.011 per heavy draft. Never above the plan's fair-use ceiling per period: $0.30 Free, $4.50 Starter, $9.00 Pro. | [AI-PROVIDER.md](AI-PROVIDER.md) |
| Stripe card payment | 2.9% + $0.30 per successful charge (US domestic card) | Add 1.5% for international cards and 1% when currency conversion is needed (https://stripe.com/pricing). |
| Stripe Billing | 0.7% of billing volume (pay-as-you-go) | https://stripe.com/pricing |
| Stripe Tax, if used | 0.5% per transaction where you are registered | Tax Basic's price for Checkout and Billing, which is how Starling charges when `STRIPE_AUTOMATIC_TAX=true` (off by default). Stripe's separate Tax API costs 50¢ per transaction instead, and Tax Complete is a monthly subscription from $90 (https://stripe.com/tax/pricing, checked 2026-09-22). Verify at purchase time. |
| Stripe Managed Payments, if used | 3.5% on top of payment fees | https://docs.stripe.com/payments/managed-payments |
| Disputes | $15 per dispute (US), not returned; another $15 to contest, returned if you win | https://stripe.com/pricing |
| Email | a few account emails per customer, such as verification and password resets | Within free tiers at small scale. |
| Storage | Each customer has a private folder on the volume holding a SQLite database, a settings file, an image cache, two exported datasets and a timing log. | **No measured figure.** This page records no measurement from the public beta or from any other run. The 20 GB reference volume is a starting point; watch the dashboard's free-space figure and the `disk:low` alert. |
| Server memory | Each active customer runs their own engine process (512 MB soft and 768 MB hard memory limit). By default up to 6 engines run at once on a 4 GB machine (`ENGINE_MAX_LIVE`), and an idle engine stops after 30 minutes (`ENGINE_IDLE_SECONDS`). | **No measured figure for memory per engine under real load is recorded here**; the engine's own development figure of about 143 MB resident after a health check is indicative only. When many customers draft at the same moment, move to a larger machine: shared-cpu-4x with 8 GB is $42.79 per month in Amsterdam against $21.40 for shared-cpu-2x with 4 GB (https://fly.io/docs/about/pricing/, checked 2026-09-18). Sizing guidance: [OPERATIONS.md](OPERATIONS.md#12-scaling-what-this-design-can-and-cannot-do). |

Margins per plan, after these costs, are in [PLANS-AND-USAGE.md](PLANS-AND-USAGE.md#margins).

## Figures that are still estimates

Everything sourced above comes from an official pricing page on the date shown. Three figures do not,
and no amount of reading will settle them:

- **Storage per customer** and **memory per engine under load.** The public beta ran on Fly.io in
  autumn 2026, but this page records no measurement of either from it. Both drive machine and volume
  sizing, so measure them on your own instance.
- **The Chrome Web Store registration fee.** Google's own pages confirm the fee is one-time but do
  not state an amount; the developer agreement reserves it to Google's discretion. The figure is
  shown during registration (https://developer.chrome.com/docs/webstore/register, checked
  2026-09-18).

One more constraint worth knowing before you plan more than one extension: since August 2026 a Chrome
Web Store publisher account has a default limit of two extension slots, with increases available on
request from the developer dashboard (https://developer.chrome.com/blog/cws-review-updates-2026,
checked 2026-09-18).
