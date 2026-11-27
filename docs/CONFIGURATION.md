# Configuration

Last reviewed: 2026-09-18.

Every setting Starling reads comes from the environment. `.env.example` is the template: copy it to
`.env` for a local run, or load the same names into your host's secret store for production. This
page lists every setting with its default, whether production needs it, how it is validated, and
which process reads it.

Source of truth: `backend/server/starling_server/config.py` (parsing, defaults and validation) and
`docs/architecture/CONTRACTS.md` section 12.

## How configuration is loaded and checked

1. `load_settings()` reads the environment, applies defaults, and raises immediately on a value it
   cannot parse (a non-integer where an integer is required, an unknown choice, a key that is not
   base64url). The process does not start.
2. `validate(settings, role)` then returns every problem that makes the configuration unusable for
   one **role**: `gateway`, `services`, `supervisor` or `cli`.
3. In `STARLING_ENV=production` any problem is fatal at startup. In `development` and `e2e` the
   problems are logged and the process continues, so the stack runs without real credentials.

`/readyz` reports `config: false` in production while any problem remains
(`backend/server/starling_server/system/routes.py`).

### Which process reads what

| Role | Process | What it is |
|---|---|---|
| `gateway` | `uvicorn starling_server.app:create_app` | website, `/api/v1`, billing, admin dashboard, engine pool |
| `services` | `python -m starling_server.services.main` | AI proxy and world service |
| `supervisor` | `python -m starling_server.supervisor` | container entrypoint, per-customer OS users, nightly backups |
| `cli` | `python -m starling_server.cli ...` | operator commands |

Secrets are deliberately split: engines never receive AI, Stripe, SMTP or session secrets, and the
services process never receives the gateway's session secret
(`docs/architecture/CONTRACTS.md` section 2.1).

## Generating the secrets

Two settings are random keys you generate yourself. Both must decode to at least 32 bytes of
base64url:

```
python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('='))"
```

Run it twice: once for `STARLING_SECRET_KEY`, once for `STARLING_SERVICES_TOKEN_KEY`. Generate
`BACKUP_PASSPHRASE` as a long random passphrase (production requires at least 16 characters) and keep
a copy outside the server: without it no archive can be read, and there is no recovery.

---

## Core

| Setting | Default | Production | Validation | Read by |
|---|---|---|---|---|
| `STARLING_ENV` | `development` | must be `production` | one of `production`, `development`, `e2e` | all roles |
| `STARLING_PUBLIC_URL` | `http://127.0.0.1:8765` | required | trailing `/` stripped; must start with `https://` in production | gateway (links, cookies, emails, redirects, sitemap) |
| `STARLING_DATA_DIR` | `/data` in production, `<repo>/data` otherwise | required | path; must be a persistent, encrypted volume | supervisor, gateway, services, cli |
| `STARLING_WEB_DIR` | `<repo>/web` (the image sets `/app/web`) | set by the image | path; `/readyz` reports `templates: false` when a required template is missing | gateway |
| `STARLING_SECRET_KEY` | none (development generates a throwaway key at start-up) | **required** | base64url, at least 32 bytes decoded | gateway, cli |
| `STARLING_SECRET_KEY_PREVIOUS` | empty | only during a rotation | base64url, at least 32 bytes decoded | gateway |
| `STARLING_SERVICES_TOKEN_KEY` | none in production; derived from `STARLING_SECRET_KEY` in development and e2e | **required** | base64url, at least 32 bytes decoded | gateway, services, supervisor |
| `STARLING_ISOLATION` | `os` in production, `none` otherwise | must be `os` | one of `os`, `none` | supervisor, gateway, cli |
| `STARLING_TRUSTED_PROXIES` | empty | set it after the first-deploy check | comma-separated addresses or CIDR ranges; no format check | gateway, supervisor |
| `STARLING_EXTENSION_IDS` | empty | **required** (at least one) | comma-separated Chrome extension ids | gateway (`/connect`, device tokens) |
| `STARLING_OWNER_EMAILS` | empty | **required** (at least one) | comma-separated emails, lower-cased | gateway, cli (`admin-grant`) |
| `STARLING_SUPPORT_EMAIL` | `[[OPERATOR_CONTACT_EMAIL]]` | **required**, and must not still be a placeholder | any non-placeholder string | gateway (website, emails, `security.txt`) |
| `STARLING_EXTENSION_MIN_VERSION` | `1.0.0` | keep at the oldest version you still accept | free text; compared with the extension's `X-Starling-Extension` header | gateway |
| `STARLING_EXTENSION_INSTALL_URL` | empty; in production, worked out from the first id in `STARLING_EXTENSION_IDS` as `https://chromewebstore.google.com/detail/<id>` | set it if the listing lives elsewhere | if set, must start with `https://` | gateway ("Add to Chrome" buttons on the website) |
| `STARLING_CONSENT_VERSION` | `2026-09-22.1` | required to match the published extension | free text; `POST /api/v1/consent` accepts only this exact value | gateway |
| `STARLING_MAINTENANCE` | unset | only during a restore | `1`, `true`, `yes` or `on` starts nothing | supervisor |
| `STARLING_WORLD_OFFLINE` | `false` | **refused**: production will not start with it on | boolean | services |
| `STARLING_E2E_PAYMENT_PAGES_ORIGIN` | empty | **refused**: production will not start with it set | one loopback https origin (`https://127.0.0.1:PORT` or `https://localhost:PORT`); anything else is ignored. The end-to-end harness sets it so the site's `form-action` policy allows the redirect to its stand-in payment pages, exactly as production's policy allows Stripe's three hosted origins | gateway |
| `STARLING_E2E_OIDC_BASE` | empty | **refused**: production will not start with it set | a loopback address. The end-to-end harness points Google sign-in at its stand-in Google with it (`e2e/harness/fake_google.py`); production always talks to Google's own endpoints, whatever this says | gateway |
| `STARLING_SERVICES_URL` | `unix:/run/starling/services.sock` with OS isolation, else `http://127.0.0.1:8766` | set `http://127.0.0.1:8766` (see the note below) | address string | gateway, supervisor (passed to engines) |
| `STARLING_SERVICES_LISTEN` | `unix:/run/starling/services.sock` with OS isolation, else `tcp:127.0.0.1:8766` | set `tcp:127.0.0.1:8766` (see the note below) | `unix:`, `tcp:127.0.0.1:PORT` or `http://127.0.0.1:PORT`; a non-loopback TCP address is refused | services |

Notes:

- A missing or older `X-Starling-Extension` header is treated as outdated and answered with HTTP 426
  `extension_outdated`.
- `STARLING_CONSENT_VERSION` must match the version compiled into the published extension. Publish
  the extension that shows the new disclosure **first**, then raise `STARLING_CONSENT_VERSION` and
  `STARLING_EXTENSION_MIN_VERSION` together.
- With `https`, the session cookie is `__Host-starling_session` (Secure, HttpOnly, SameSite=Lax);
  without it, plain `starling_session`. The cookie name is derived from `STARLING_PUBLIC_URL`.
- **The services address under OS isolation.** The default Unix socket does not work yet: the
  supervisor creates `/run/starling` as root with mode 0755, so the services user cannot bind a
  socket in it and the process restarts in a loop. Set `STARLING_SERVICES_LISTEN` and
  `STARLING_SERVICES_URL` to the loopback addresses above, as `deploy/fly.toml` does. That is what
  the beta ran. Every call to the services process carries a signed per-account token, and the
  address is never public.

## Sign in with Google (optional)

"Continue with Google" appears on the sign-in and sign-up pages only when both settings are set. How
to create the OAuth client is in [DEPLOYMENT.md](DEPLOYMENT.md#step-7b-sign-in-with-google-optional).

| Setting | Default | Production | Validation | Read by |
|---|---|---|---|---|
| `GOOGLE_CLIENT_ID` | empty (Google sign-in off) | optional | must end in `.apps.googleusercontent.com`; must be set together with the secret | gateway |
| `GOOGLE_CLIENT_SECRET` | empty | a secret: store it like `STRIPE_SECRET_KEY` | must be set together with the id | gateway only (withheld from the services process and from every engine) |

With it off, `/auth/google/*` answers 404 and the site's `form-action` policy does not name Google.
With it on, the policy also allows `https://accounts.google.com`, because "Connect Google" on the
Security page is a form answered with a redirect to Google.

## Business and legal details

These fill the legal pages, the contact page, the footer and the account emails. Production refuses
to start while any of them still holds a `[[PLACEHOLDER]]` marker.

| Setting | Default | Production | What to enter | Read by |
|---|---|---|---|---|
| `OPERATOR_COMPANY_NAME` | `[[OPERATOR_COMPANY_NAME]]` | **required** | legal name of the business that operates Starling | gateway |
| `OPERATOR_REGISTERED_ADDRESS` | `[[OPERATOR_REGISTERED_ADDRESS]]` | **required** | registered business address | gateway |
| `OPERATOR_CONTACT_EMAIL` | `[[OPERATOR_CONTACT_EMAIL]]` | **required** | general contact email | gateway |
| `OPERATOR_PRIVACY_CONTACT` | `[[OPERATOR_PRIVACY_CONTACT]]` | **required** | where privacy requests go | gateway |
| `OPERATOR_JURISDICTION` | `[[OPERATOR_JURISDICTION]]` | **required** | where the business is established | gateway |
| `OPERATOR_GOVERNING_LAW_VENUE` | `[[OPERATOR_GOVERNING_LAW_VENUE]]` | **required** | governing law and venue for disputes | gateway |
| `OPERATOR_EFFECTIVE_DATE` | `[[OPERATOR_EFFECTIVE_DATE]]` | **required** | the date the legal pages take effect | gateway |
| `HOSTING_PROVIDER` | `[[HOSTING_PROVIDER]]` | **required** | name of the hosting provider | gateway (subprocessors page) |
| `EMAIL_PROVIDER` | `[[EMAIL_PROVIDER]]` | **required** | name of the email provider | gateway (subprocessors page) |
| `AI_SUBPROCESSORS` | `OpenAI` | already filled | the AI provider or providers | gateway (subprocessors page) |
| `RETENTION_PERIODS` | `[[RETENTION_PERIODS]]` | **required** | a plain-language retention schedule | gateway (privacy page) |
| `REFUND_POLICY` | `[[REFUND_POLICY]]` | **required** | a plain-language refund policy | gateway (refunds page) |

The seven `OPERATOR_*` settings were previously named `BUYER_*`, with the same suffixes. For
compatibility the code still reads an old name when the new one is not set.

Validation: any value starting with `[[` counts as unfilled. Outside production the pages render with
the placeholders highlighted and a "draft legal text" banner.

## Email

| Setting | Default | Production | Validation | Read by |
|---|---|---|---|---|
| `EMAIL_MODE` | `smtp` in production, `outbox` otherwise | must be `smtp` | one of `smtp`, `outbox` | gateway |
| `SMTP_HOST` | empty | **required** | non-empty in production | gateway |
| `SMTP_PORT` | `587` | – | integer, at least 1 | gateway |
| `SMTP_USERNAME` | empty | usually required by the provider | none | gateway |
| `SMTP_PASSWORD` | empty | usually required by the provider | none (not trimmed) | gateway |
| `SMTP_STARTTLS` | `true` | keep on | boolean | gateway |
| `SMTP_FROM` | empty | **required** | non-empty in production | gateway |

In `outbox` mode nothing is sent: messages are stored in the control database and, in development
and e2e only, readable at `GET /_dev/outbox?to=<address>`. In `smtp` mode a sent message's stored
copy is deleted after 30 days.

The emails Starling sends are: verify your address; "someone signed up with your address"; password
reset; password reset done; password changed; confirm your new address; a notice to the old address
with a 7-day revert link; "someone tried to move another account to this address"; and owner alerts
to `ALERT_EMAIL`. There is no marketing email.

## AI provider

| Setting | Default | Production | Validation | Read by |
|---|---|---|---|---|
| `AI_PROVIDER` | `openai` | – | `openai` or `anthropic` ([AI-PROVIDER.md](AI-PROVIDER.md#anthropic-ai_provideranthropic)). `anthropic` changes the defaults of `AI_BASE_URL` (`https://api.anthropic.com`), the three `AI_MODEL_*` settings (`claude-haiku-5-5`), `WEB_SEARCH` (`false`) and `AI_SUBPROCESSORS` (`Anthropic`), and requires Claude models, `WEB_SEARCH=false` and `AI_MAX_INPUT_TOKENS_PER_CALL` of 60000 or less | all roles |
| `AI_BASE_URL` | `https://api.openai.com/v1` | required | trailing `/` stripped; must start with `https://` | services |
| `AI_API_KEY` | empty | **required** | non-empty in production | services |
| `AI_API_KEY_SECONDARY` | empty | optional; used for zero-downtime rotation | none | services |
| `AI_MODEL_QUALITY` | `gpt-5.6-luna` | required | must have a price in the pricing file | services (`admin` shows it) |
| `AI_MODEL_FAST` | `gpt-5.6-luna` | required | must have a price in the pricing file | services |
| `AI_MODEL_VISION` | `gpt-5.6-luna` | required | must have a price in the pricing file | services |
| `AI_REASONING_EFFORT` | `none` | – | sent through as `reasoning_effort` | services |
| `AI_PRICING_FILE` | `backend/server/config/ai_pricing.json` | – | must be valid JSON that parses into price entries | all roles (loaded at start-up) |
| `AI_CREDIT_MULTIPLIER` | `1` | – | integer, at least 1 | gateway (usage) |
| `AI_MAX_CALLS_PER_REQUEST` | `12` | – | integer, at least 1 | services |
| `AI_MAX_INPUT_TOKENS_PER_CALL` | `8000` | – | integer, at least 256 | services |
| `AI_MAX_OUTPUT_TOKENS` | `600` | – | integer, at least 16 | services |
| `AI_TIMEOUT_S` | `25` | – | number. Each upstream call; the engine gives a draft call 20 seconds and a helper call 8, so a longer limit only keeps paying for answers nobody is waiting for | services |
| `AI_DAILY_BUDGET_USD` | `10` | – | number; stored as micro-dollars | services (`admin` shows it) |
| `AI_MONTHLY_BUDGET_USD` | `90` | – | number; stored as micro-dollars | services (`admin` shows it) |
| `AI_FREE_TRIP_PCT` | `80` | – | integer, at least 1 | services |
| `WEB_SEARCH` | `true` | – | boolean. Real web search with OpenAI's `web_search` tool on the Responses endpoint, using the same key | services |
| `WEB_SEARCH_MODEL` | the value of `AI_MODEL_FAST` | – | must have a price in the pricing file while `WEB_SEARCH` is on | services |
| `WEB_SEARCH_CONTEXT_SIZE` | `low` | – | `low`, `medium` or `high`: how much of the pages found the tool reads (more costs more tokens) | services |
| `WEB_SEARCH_PER_ACCOUNT_DAY` | `20` | – | integer, at least 0; `0` means no searches | services |
| `WEB_SEARCH_BACKGROUND_PER_ACCOUNT_DAY` | `8` | – | integer, at least 0; `0` means no background topic research. Paid plans only, and paced over the period | services |
| `WEB_SEARCH_TIMEOUT_S` | `8` | – | number above 0 | services |

Notes:

- A model with no entry in `ai_pricing.json` is refused at run time with 503 `ai_unavailable`,
  because its spend cannot be metered, and in production it stops the services process from starting
  (`no price configured for AI model ...`).
- A zero budget allows no spend at all. A budget stop lasts to the end of its window (UTC midnight,
  or the first of the next month); raising the budget and restarting the services process clears a
  stop whose window is now under the limit.
- Web search is priced by OpenAI at $10 per 1,000 searches plus the pages read, billed at the
  model's token rates (checked 2026-09-22): about a cent a search at `low`, many times the cost of
  an ordinary press. So the engine asks for two things only. It checks a claim when the public post
  it is answering makes a checkable, time-sensitive claim that the post's own links and the news pool
  do not cover. It researches topics for the account's beat (the query names only its subject
  labels): in the background on paid plans, at most `WEB_SEARCH_BACKGROUND_PER_ACCOUNT_DAY` a day,
  paced over the period and within 60% of the account's background share, or once at press time when
  nothing is stored; a topics answer is shared between accounts asking the same query for 60 minutes.
  A press searches at most once, and at most `WEB_SEARCH_PER_ACCOUNT_DAY` times a day per account; and only while the
  period's spend plus the search's worst-case cost stays under 95% of the plan's cost ceiling and
  searches have used under 25% of it, so a search never takes an account to `fair_use_exceeded`. A
  search still running after 7 seconds is dropped and the card says so. The spend counts toward
  the budgets above like any other AI call. Production refuses to start while web search is on and
  `ai_pricing.json` has no `web_search` row in `tools`. Details in [AI-PROVIDER.md](AI-PROVIDER.md).
- `AI_FREE_TRIP_PCT`: once the day's **global** AI spend reaches this percentage of
  `AI_DAILY_BUDGET_USD`, free-plan accounts are refused with 503 `service_paused` while paid accounts
  keep drafting.
- The defaults are chosen to stay under OpenAI usage tier 1's $100 monthly limit. Also set a hard
  spend limit in the OpenAI dashboard: keep **Starling monthly budget ≤ provider hard limit ≤ tier
  monthly limit**. See [AI-PROVIDER.md](AI-PROVIDER.md#spending-safeguards).

## Plans and sign-up

| Setting | Default | Production | Validation | Read by |
|---|---|---|---|---|
| `PLANS_FILE` | `backend/server/config/plans.toml` | – | must be valid TOML defining `[plans.free]`; every paid plan needs `amount_cents` (at least 1), a three-letter `currency` and `interval = "month"`; no negative allowance or ceiling; the free plan must have no Stripe lookup key; every rate at least 1; a plan with an allowance needs a ceiling above 0 (a ceiling of 0 blocks every request); a paid plan must allow more than the free plan; lookup keys and names unique; a dearer plan may not allow fewer than a cheaper one. Every problem is listed at once and the server refuses to start | all roles (loaded at start-up) |
| `PAST_DUE_GRACE_DAYS` | `7` | – | integer, at least 0 | gateway (billing) |
| `SIGNUP_GLOBAL_PER_HOUR` | `500` | – | integer, at least 0; `0` switches it off. Sign-ups accepted per hour for the whole site, on top of the fixed 10 an hour per network. It bounds what a flood spread over many networks can cost: password hashing, account rows and outbound confirmation emails from your sending domain. Raise it for a launch day | gateway (accounts) |
| `MAX_DEVICES_PER_ACCOUNT` | `20` | – | integer, at least 0; `0` switches it off. Connected browsers one account may hold at once | gateway (accounts) |
| `DISPOSABLE_DOMAINS_FILE` | empty | optional | path to a file of one domain per line, reloaded when the file changes | gateway (sign-up) |

There is **no built-in disposable-domain list**: leaving `DISPOSABLE_DOMAINS_FILE` unset blocks
nothing.

`backend/server/config/plans-free.toml` is a free-only plans file. Point `PLANS_FILE` at it to run
with no paid plan and no Stripe account (see [Stripe](#stripe)).

In production the gateway also compares every Stripe price (amount, currency, interval) with
`plans.toml` at start-up and refuses to start on a mismatch.

## Stripe

Stripe is optional. Production requires `STRIPE_SECRET_KEY` only when the plans file has a paid plan,
so with a free-only plans file Starling runs without a Stripe account. The key, mode and webhook
secret checks below apply once a Stripe key is set.

| Setting | Default | Production | Validation | Read by |
|---|---|---|---|---|
| `STRIPE_MODE` | `test` | must be `live`, unless `STRIPE_ALLOW_TEST_MODE=true` | one of `test`, `live` | gateway, cli |
| `STRIPE_ALLOW_TEST_MODE` | `false` | only for a pre-launch rehearsal | boolean. Lets production run on Stripe's sandbox: every other production check still applies, every page says "Payments are in test mode", nothing is indexed, and the owner dashboard shows an alert until it is removed | gateway |
| `STRIPE_SECRET_KEY` | empty | **required** when the plans file has a paid plan | must start with `sk_`/`rk_` and match the mode (`_test_` or `_live_`) | gateway, cli |
| `STRIPE_ACCOUNT_ID` | empty | recommended | compared with `GET /v1/account` at start-up; skipped with a warning when a restricted key may not read the account | gateway, cli |
| `STRIPE_WEBHOOK_SECRET` | empty | **required** whenever a Stripe key is set | must start with `whsec_` whenever a Stripe key is set | gateway |
| `STRIPE_WEBHOOK_SECRET_PREVIOUS` | empty | only during a rotation | none | gateway |
| `STRIPE_API_BASE` | `https://api.stripe.com` | must be exactly this | exact match in production | gateway, cli |
| `STRIPE_API_VERSION` | `2026-08-26.dahlia` | keep pinned | free text; sent on every request | gateway, cli |
| `STRIPE_PORTAL_CONFIGURATION_ID` | empty | required for the billing page | none; created by `stripe-setup` | gateway |
| `STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID` | empty | required for upgrades | none; created by `stripe-setup` | gateway |
| `STRIPE_AUTOMATIC_TAX` | `false` | business decision | boolean | gateway |
| `STRIPE_TAX_CODE` | empty | needed when Stripe Tax is on | none | gateway |
| `STRIPE_BLOCK_PREPAID` | `true` | – | boolean | gateway |
| `STRIPE_ALLOW_PROMO_CODES` | `false` | – | boolean | gateway |

`STRIPE_API_BASE` exists only so the automated tests can point at a fake Stripe. Production refuses
any other value.

## Engines

| Setting | Default | Production | Validation | Read by |
|---|---|---|---|---|
| `ENGINE_MAX_LIVE` | `6` | tune to the machine | integer, at least 1 | gateway (engine pool) |
| `ENGINE_IDLE_SECONDS` | `1800` | – | integer, at least 30. A customer's engine stops after this long without a request; an idle engine is still the first one stopped when every slot is taken. The extension's session calls count as use, so someone reading X between drafts does not pay a cold start (2.5 to 3 s) on the next press | gateway |
| `ENGINE_MEMORY_SOFT_MB` | `512` | – | integer, at least 128 | gateway, supervisor |
| `ENGINE_MEMORY_HARD_MB` | `768` | – | integer, at least 128, **and at least `ENGINE_MEMORY_SOFT_MB`** | supervisor |
| `ENGINE_UID_BASE` | `200000` | – | integer, at least 100000 | supervisor, cli |

At the soft limit the supervisor reports the engine as `over_soft_limit` and the gateway pool
restarts it once it is idle. At the hard limit the supervisor kills it.

## Security and operations

| Setting | Default | Production | Validation | Read by |
|---|---|---|---|---|
| `ARGON2_TIME_COST` | `3` | – | integer, at least 1 | gateway |
| `ARGON2_MEMORY_KIB` | `65536` (64 MiB) | – | integer, at least 8192 | gateway |
| `ARGON2_PARALLELISM` | `4` | – | integer, at least 1 | gateway |
| `ARGON2_MAX_CONCURRENT` | `4` | – | integer, at least 1 | gateway |
| `LOG_LEVEL` | `INFO` | – | upper-cased and passed to Python logging | gateway, services, supervisor |
| `BACKUP_DIR` | `<STARLING_DATA_DIR>/backups` | – | path | supervisor, cli |
| `BACKUP_PASSPHRASE` | empty | **required** for the supervisor: at least 16 characters | length check in production | supervisor, cli |
| `ALERT_EMAIL` | empty | strongly recommended | none; without it no alert email is sent | gateway (admin) |

Passwords are Argon2id. The rules the gateway enforces at sign-up and on change: 10 to 128
characters; rejected if the password is on the built-in common list, has 2 or fewer distinct
characters, or equals the email address or its local part.

## Settings by environment

| Setting | `development` | `e2e` | `production` |
|---|---|---|---|
| `STARLING_ENV` | `development` | `e2e` | `production` |
| `STARLING_PUBLIC_URL` | `http://127.0.0.1:8765` | the harness address | https, required |
| `STARLING_ISOLATION` | `none` | `none` | `os` (refused otherwise) |
| `EMAIL_MODE` | `outbox` | `outbox` | `smtp` (refused otherwise) |
| `STRIPE_MODE` | `test` | `test` | `live` (`test` only with `STRIPE_ALLOW_TEST_MODE=true`, for a rehearsal) |
| `STRIPE_API_BASE` | Stripe, or a fake | a fake Stripe | `https://api.stripe.com` only |
| `AI_BASE_URL` | anything | a fake provider | https only |
| `STARLING_WORLD_OFFLINE` | allowed | allowed | refused |
| `STARLING_WORLD_FIXTURES` | allowed (with `STARLING_WORLD_OFFLINE`) | set by the harness | refused |
| `STARLING_E2E_PAYMENT_PAGES_ORIGIN` | allowed | set by the harness | refused |
| `STARLING_E2E_OIDC_BASE` | allowed | set by the harness | refused |
| `GOOGLE_CLIENT_ID` / `_SECRET` | optional | the harness's stand-in client | optional |
| Missing credentials | logged, process starts | logged, process starts | fatal at start-up |

## Extension build variables

These are **not** server settings. They are read by the extension build
(`extension/wxt.config.ts`), not by any Starling process.

| Variable | Meaning |
|---|---|
| `WXT_STARLING_ORIGIN` | The only origin the built extension ever talks to. It is validated when the build configuration loads and compiled into the bundle. https is required, except `http://127.0.0.1` or `http://localhost` in `--mode e2e`. |
| `STARLING_STORE_BUILD` | `1` marks the artifact uploaded to the Chrome Web Store: local and private hosts are refused and no development `version_name` is written. |
| `STARLING_OUT_DIR` | Moves the build output folder so two builds never overwrite each other. Without it, output lands in `.output/chrome-mv3` (production) or `.output/chrome-mv3-e2e`. |

The extension's own version comes from `extension/package.json` (`version`), which the build
configuration requires to be `x.y.z`. The origin in the built manifest must match
`STARLING_PUBLIC_URL`, and the published extension id must be listed in `STARLING_EXTENSION_IDS`.
See [EXTENSION-PUBLISHING.md](EXTENSION-PUBLISHING.md).

## What has not been verified

Everything on this page was read from the code and checked against local runs with mocked providers
(the container image builds and its tests pass). The author's free public beta on Fly.io in autumn
2026 used the Anthropic provider (Claude Haiku 5.5), web search off, the free-only plan file and no
Stripe configuration, so it did not exercise the OpenAI, web search, paid-plan or Stripe settings.
Real Stripe payments were never taken: the billing code has only run against the stand-in Stripe in
this repository, never against Stripe itself. See [TESTING.md](TESTING.md).
