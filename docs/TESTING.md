# Testing

Last reviewed: 2026-09-20.

Every automated check in the repository: the command that runs it, what it proves and, just as
importantly, what it does not. Read the [mocked versus live](#mocked-versus-live) table before you
trust any result here as evidence about a production service.

A GitHub Actions workflow (`.github/workflows/ci.yml`) runs the server, engine and website suites on
Linux with Python 3.11, on the hash-checked `backend/requirements.lock`. It also runs the extension's
type check and unit tests, then an e2e-mode build that the build verifier checks. It runs on every
push to `main` and on every pull request. It does not run the end-to-end journeys or build the
container image, so those stay local steps.

Prerequisites: Python 3.11 with the virtual environment at `backend\.venv`, and Node.js with pnpm for
the extension. Commands are written for Windows PowerShell from the repository root; on Linux and
macOS replace `backend\.venv\Scripts\python.exe` with `backend/.venv/bin/python` and the backslashes
with forward slashes.

## The suites

| Suite | Run from | Command | What it proves |
|---|---|---|---|
| Server | `backend\server` | `..\.venv\Scripts\python.exe -m pytest tests` | The gateway, services process, usage accounting, billing, engine pool, admin dashboard and backup code, against fake providers |
| Engine | `backend\engine` | `..\.venv\Scripts\python.exe -m pytest tests` | The drafting pipeline: decisions, voice, gates, scrubbers, prompt assembly, storage |
| Website | repository root | `backend\.venv\Scripts\python.exe -m pytest web\tests` | Every template renders with the sample contexts under `StrictUndefined`, and content, accessibility and contrast rules hold |
| Extension types | `extension` | `pnpm typecheck` | TypeScript compiles with no errors |
| Extension unit | `extension` | `pnpm test` | Consent, transport, sign-in, stream handling, X action guards, composer safety, token isolation |

Run one package of the server suite while you work on it:

```
cd backend\server
..\.venv\Scripts\python.exe -m pytest tests\billing      # or core, usage, engines, services, admin, ops
```

### What the server suite covers, package by package

| Package | The questions it answers |
|---|---|
| `tests/core` | Sign-up (the 6-digit code, and the sign-up tab carrying on once the link is opened on another device), sign-in, Sign in with Google against a stand-in Google (RS256 tokens, PKCE, state and nonce, the authoritative-address rule, linking and unlinking), password reset, sessions, CSRF, email tokens and their HTML versions, device tokens, the connect flow, security headers, problem pages, static-file compression, rate limits |
| `tests/usage` | Reserve, activate, expose, commit, release, replay, expiry; concurrency slots; the one `account_state()` the website and the extension both read |
| `tests/billing` | Checkout, the webhook queue, entitlement, upgrades, scheduled downgrades, cancellation, past-due grace, disputes, refunds, duplicate and out-of-order events, per-customer resync, all against a fake Stripe |
| `tests/services` | The AI proxy: caps, budgets, metering, the secondary-key fallback, rate-limit and quota errors, the world service and its SSRF defences |
| `tests/engines` | Spawning, the engine pool, eviction, the circuit breaker, the capture spool, stream passthrough, and the Linux-only privilege-separation checks |
| `tests/admin` | Every dashboard page and action, each with a negative control, including a test that a planted draft never appears on a customer page |
| `tests/ops` | Encrypted archives, verification, restore, retention and the backup scheduler |
| `tests/test_foundation.py` | Configuration parsing and validation, migrations, the plans file, AI price rounding, the contract error bodies |

### Security invariants

`docs/architecture/CONTRACTS.md` section 13 lists 14 security invariants, and each has a test in the
server suite. Among them: development-only routes return 404 in production; `/api/v1` accepts only
device tokens; device tokens cannot reach account or billing pages; engines never receive AI, Stripe,
SMTP or session secrets; logs never contain prompts, drafts, tokens, passwords or email bodies; a
two-customer canary proves no data crosses between customers; webhook signature checks reject wrong
secrets, modified bodies and stale timestamps; production refuses the test switches.

Seven of them are Linux-and-root checks (one engine cannot read another's files or socket, file
ownership and modes). They **skip** on Windows and on a non-root Linux user, and run only inside the
container image's `test` target.

## The gates

These are the checks that decide whether something may ship.

### Release gate

```
backend\.venv\Scripts\python.exe scripts\check_release.py
backend\.venv\Scripts\python.exe scripts\check_release.py --git-tracked-only
backend\.venv\Scripts\python.exe scripts\check_release.py <unpacked image root>
backend\.venv\Scripts\python.exe scripts\check_release.py <unzipped extension build>
```

It scans a directory tree for provider and Stripe keys, private key blocks, device tokens,
high-entropy secret assignments, home-directory and synced-folder paths, and forbidden files
(databases, key and token files, `.env` files, logs, dumps, archives, anything over 5 MB).

**Names are optional, and private to whoever cuts a release.** The script contains no names. It
also looks for the names listed in `scripts/release_private_patterns.json` when that file exists.
Git ignores the file, so it is never committed, and whoever cuts a release can write one for
themselves: `{"personal": {"my-email": "(?i)jane@doe\\.example"}, "brand": {"old-name": "(?i)acme"}}`
(JSON, so a regular expression's backslashes are doubled). The gate's result line says how many
private patterns it used, and says so plainly when it used none. Every finding prints with file and
line; secrets are redacted in the output. Exit status 1 on any finding, 0 on a clean tree.
Documented exceptions live in `scripts/release_allowlist.txt`.

Run it on the repository, on the unpacked container image filesystem, and on the unzipped extension
build before publishing any of them.

**Which mode to trust.** Without `--git-tracked-only` it walks every file on disk, including things
Git ignores, such as a local test run's browser profiles and editor folders. With
`--git-tracked-only` it scans exactly what Git would commit.

Always run it on the **exact artefact being published**: a clean checkout, the unpacked image, the
unzipped extension. Never on a working folder as proof.

### Site check

```
backend\.venv\Scripts\python.exe scripts\check_site.py --templates
backend\.venv\Scripts\python.exe scripts\check_site.py --base-url http://127.0.0.1:8080 --env development
```

`--templates` renders every template with the sample contexts in `web/tests/sample_contexts.py` and
checks each page for: no inline script or style and no event handler attributes; exactly one `h1`,
one `main#main` and a skip link as the first focusable element; no skipped heading levels; unique
ids; a label for every form control and alt text on every image; a CSRF field in every POST form and
none in a GET form; an `aria-live` error summary wherever a POST form has editable fields; no
off-origin resources; no banned wording (claims left over from the retired desktop product,
testimonials, ratings, user counts, guarantees, fake urgency, and every name in
`scripts/release_private_patterns.json` if that file exists); `[[PLACEHOLDER]]` markers only
on legal pages and only inside a placeholder marker element; `noindex` outside production. It also
checks the colour tokens for WCAG contrast.

`--base-url` crawls a running server without signing in: every internal link, fragment and asset must
resolve, every HTML response needs a CSP without `unsafe-inline`, `unsafe-eval` or wildcards and with
`frame-ancestors 'none'`, the sitemap must list the public pages and no private ones, and robots.txt
must block everything outside production. It requests nothing from any other host.

Exit status 1 on any problem.

### Extension build verifier

```
cd extension
STARLING_STORE_BUILD=1 WXT_STARLING_ORIGIN=https://app.your-domain.example STARLING_OUT_DIR=.output-store pnpm build
node scripts\verify-extension-build.mjs .output-store
```

Replace the address with your own service origin: the build refuses the placeholder as written, by
design. These two lines use the `NAME=value command` form of bash, Git Bash and macOS; in Windows
PowerShell set `$env:STARLING_STORE_BUILD`, `$env:WXT_STARLING_ORIGIN` and `$env:STARLING_OUT_DIR`
first ([EXTENSION-PUBLISHING.md](EXTENSION-PUBLISHING.md#2-build-the-store-package) has the exact lines).

For an e2e build, add `--allow-local`:

```
$env:STARLING_OUT_DIR = ".output-e2e"          # bash: STARLING_OUT_DIR=.output-e2e pnpm build --mode e2e
pnpm build --mode e2e
node scripts\verify-extension-build.mjs .output-e2e --allow-local
```

It checks the built manifest and the whole bundle: manifest version 3; permissions exactly `storage`
and `identity`, with no optional permissions; no `key`, `update_url`, `externally_connectable`,
`oauth2`, `content_security_policy` or `chrome_url_overrides`; `homepage_url` is a bare https origin
and `host_permissions` is exactly the two X hosts plus that origin; name and description are
printable ASCII within the store's 75 and 132 character limits; version is `x.y.z`; a store build
carries no `version_name`; `minimum_chrome_version` is `120`; content scripts run only on X, never in
the page's main world and never in all frames. It then scans every text file in the bundle for native
messaging, a native host name, retired internal names, "local helper" and "local AI" wording, a local
model runtime name, the browser language-model API, scripted scrolling, source-map references, hidden
tab creation, and, unless `--allow-local` is given, any local service address.

Exit status 1 with every failure printed. The verifier is itself tested: a deliberately bad build
must fail it.

### Embedding matrix check

```
cd backend\engine
..\.venv\Scripts\python.exe tools\build_embedding_projection.py
```

Prints `OK: starling-embedding.npy is exactly the seeded projection (256, 96) float32` and exits 0
when the shipped matrix is exactly the seeded random projection it claims to be: no trained weights
and nothing third-party. Exit 1 with `MISMATCH` otherwise.

### The image and the clean checkout

```
scripts\lock-requirements.ps1
docker compose -f deploy/docker-compose.yml --profile test run --rm --build starling-test
```

The first regenerates `backend/requirements.lock` (exact versions with SHA-256 hashes for Linux
x86_64, CPython 3.11) inside the same base image the production image uses. The second builds the
image's `test` target and runs **both** suites inside it as root, with `network_mode: none`, so the
Linux-only privilege-separation tests run and nothing can reach the network. It runs both suites even
when the first fails and exits non-zero if either failed.

**First run on 2026-09-20**, and again on 2026-09-22 after the UX and AI passes (no dependency
changed, so the same lock). The lock of that date held 39 packages and 1,136 hashes, resolved inside
`python:3.11-slim` for linux/amd64. On 2026-09-22 the `test` target built and both suites passed
inside it, as root, with no network:
**server 1,458 passed, 1 skipped; engine 3,394 passed, 19 skipped, 2 expected failures**. The runtime image built too (686 MB)
and the release gate passed on its unpacked filesystem (293 files).

Only 1 server test skipped there instead of the 7 that skip on Windows: the Linux-and-root
privilege-separation checks ran inside the image, and passed. That run found three real defects that
no other layer could: a hosted-engine time-zone bug, an SSRF check whose answer depended on the
Python patch release, and this target being unable to read the contract it verifies. All three were
fixed.

Run again on the final commit of 2026-09-22 (after the product and design review), with the same lock:
server 1,493 passed, 1 skipped; engine 3,344 passed, 23 skipped, 2 expected failures (4 of the 23 are the niche-read checks on the evaluation personas, which live in `e2e/eval` and are not in the image; they pass everywhere else). The runtime image built (686 MB) and the release gate passed on its unpacked
filesystem.

Those are the image runs recorded here, and their counts are from those dates. The lock has been
regenerated since: in this release `backend/requirements.lock` pins 47 packages with 1,269 hashes,
and CI installs it with hash checking on every run.

Regenerate the lock when `requirements.in` changes, and run the test target before every release.

## The end-to-end suite

`e2e/` starts the **real** gateway, the **real** services process and **real** engine workers as
separate processes, with `STARLING_ENV=e2e`, and drives them the way a customer does. Only the third
parties are local stand-ins: a fake AI provider, a fake Stripe and a fake Google sign-in
(`e2e/harness/`). X is a local copy of its page structure, never x.com. Nothing here is evidence
about a live provider.
[e2e/README.md](../e2e/README.md) describes the harness, the fakes and their control endpoints.

There are two layers.

### API journeys

```
backend\.venv\Scripts\python.exe -m pytest e2e\tests -q
```

Needs only the Python environment: no Docker, no network, no accounts. Each run gets a fresh data
folder and free ports, and stops every process it started. Every journey reads the account's state
in three places, which must agree: the rendered website pages, `GET /api/v1/session`, and the control
database. Every group has a negative control that makes the same assertions fail by changing only the
harness.

| Journey | What it proves |
|---|---|
| `test_01_signup` | sign-up, the confirmation email, sign-in, the free allowance in all three views, CSRF and the Origin check |
| `test_02_connect` | the extension's PKCE exchange and every way it is refused; listing and revoking browsers |
| `test_03_drafting` | a real draft from a real engine worker; the stream contract; one draft charged; `ai_calls` priced from the price file |
| `test_04_idempotency` | replays, mismatches, provider failures, disconnects, polling a request id, the one-draft-at-a-time slot, rate limits; a press is never charged when every AI call failed |
| `test_05_limits` | the plan allowance, the fair-use ceiling, the owner's pause, the daily AI budget and the free-account trip |
| `test_06_billing` | checkout, duplicate and out-of-order webhooks, renewals, upgrade, scheduled downgrade and undo, cancellation, disputes, refunds, the portal; a declined first card finished on the invoice page with no second subscription |
| `test_07_deletion` | the data export, then deletion: Stripe cancelled, the customer's folder gone, the tombstone written, the token refused |
| `test_08_isolation` | two customers drafting at once: separate processes, separate folders, nothing crosses |
| `test_09_restart` | the gateway killed mid-draft and started again: the orphaned reservation is settled at start-up, the slot is free, nothing was charged, the counters add up |
| `test_10_google` | Continue with Google against the stand-in Google: a confirmed account with no password and no confirmation email; the extension connecting through the same sign-in; a cancelled Google sign-in going back to where it started |

### Browser journeys

```
node e2e\browser\run.mjs
node e2e\browser\run.mjs --journey 2 --skip-build
```

A real Chrome with the **real unpacked extension**, driven by Playwright. Prerequisites: Node.js 20 or
newer; once, in `e2e\browser`, `npm ci` and `npx playwright install chromium`; and the extension's
dependencies installed, because the runner builds the e2e extension first. The runner starts the
stack on port 8765 (the origin the e2e extension is compiled for), runs the journeys serially with a
fresh browser profile per file, stops everything and then checks that the port is free and every
process it started is gone. The browser is fenced in by a local proxy: a journey fails if the page or
the extension tries to reach any host other than the stack.

| Journey | What it proves |
|---|---|
| `01-new-customer` | sign-up with the mistakes people make, each answered in words; the emailed link; every account page; changing and resetting the password (a reset link works once, and signs that browser in); the data export; deleting the account. A second test confirms the email from another client and proves the sign-up tab carries on to the account by itself, and not before |
| `02-paying-customer` | walking away charges nothing; a declined card is said plainly and is not a dead end (finish on the invoice page, or choose another plan); pay, cancel, resume, upgrade |
| `03-extension-customer` | install, consent, the real `chrome.identity` sign-in, a reply drafted into X's own box, the allowance agreeing in the extension, the API and the website after every step, the limit, "See plans", paying, carrying on, Enhance, disconnecting. Starling never presses Post or Reply |
| `04-google-customer` | "Sign up with Google" through the stand-in account chooser: a confirmed account with no password and no confirmation email; the extension's sign-in window connecting through the same Google sign-in with nothing typed |
| `05-web-checked-draft` | a reply to a post that makes a claim gets one web search from the stand-in; its sources are listed under the draft as real links (site named, new tab, `noopener noreferrer`), as OpenAI requires for web search results; the new-account voice note shows with its "Add writing samples" button. A second test proves "Add my posts from this page" on the account's own profile: its own posts become writing samples, a repost stays out, and a second press adds nothing twice |

These exist because some failures only a browser can show. The ones this layer found, all fixed and
each pinned by a test, are listed in [e2e/README.md](../e2e/README.md#known-findings); the first was
that "Choose Starter" did nothing at all, because the site's own `form-action` policy blocked the
redirect to the payment page and no HTTP-level test could notice.

### What the end-to-end layer does not prove

Both layers use the fake AI provider, the fake Stripe and the fake Google for every call, and the X
fixture for every page. They prove that Starling's own parts agree with each other and never strand a
customer. They prove nothing about Stripe's real pages, Google's real sign-in, a real model's drafts,
real email delivery or the real x.com. Those are the [live checks that remain](#the-live-checks-that-remain).

## Mocked versus live

This is the most important table in this document. Every result recorded in this document came from
the left-hand column.

The author ran Starling as a free public beta in autumn 2026 on Fly.io, with the Anthropic provider
(Claude Haiku 5.5), web search off, a free-only plan file and Stripe not configured. The beta has
ended and no hosted Starling service is running now. This document records no results from the
beta. Real Stripe payments were never taken, and the billing results recorded here come from the
fake Stripe. The right-hand column is what an operator still has to check on their own accounts and
host.

| Area | Verified locally, how | Live check still needed |
|---|---|---|
| AI provider | Fake upstream in the server suite and the e2e harness; prices from a local file; metering, caps, budgets, key fallback, quota and rate-limit errors all exercised against the fake | Real key on a real account: reachability, draft quality, latency, real cost per press, cached-token share |
| Stripe | Fake Stripe answering the same REST shapes; the full subscription lifecycle, webhooks, retries and reconciliation | Sandbox checkout, portal and webhook delivery; then the same in live mode with a real card; Stripe Tax or Managed Payments if chosen |
| Email | `EMAIL_MODE=outbox`: messages stored, never sent; read back in tests, including the HTML version's structure | SMTP delivery through the chosen provider, whether the messages land in inboxes rather than spam, and how the HTML version renders in Gmail, Outlook and Apple Mail |
| Sign in with Google | A stand-in Google (discovery, JWKS, token endpoint, an account chooser) in the server suite, the API journeys and a browser journey; real RS256 signatures and the full token checks | A real OAuth client on the operator's domain: the consent screen, the redirect URI, brand verification, and sign-in with a Gmail and a Workspace account |
| Hosting | The image built and its `test` target passed as root with no network (2026-09-22); `deploy/` files reviewed | Deploy, TLS certificate, volume, the trusted-proxy check, snapshots, and one rehearsed restore |
| Extension | Type check, unit tests, both builds and the build verifier; the real unpacked extension in a real Chrome on a local copy of X's page structure | The real extension on the real x.com with a person signed in to X |
| Chrome Web Store | Nothing | Registration, listing, privacy practices answers, and review |
| Isolation between customers | Canary and ownership tests; the Linux-only ones skip outside the container | The same tests inside the built image on a real host |
| Backups and restore | Archive, verify, restore, retention and scheduler covered by tests | A real restore on a real volume, from an archive copied off the machine |
| Website | The website suite (281 tests in this release), the site check, the browser journeys, and a manual browser pass at 1280 px and 375 px | The live site on the operator's domain, with real legal text |
| AI drafting | The stand-in model in the engine suite, the API journeys and a browser journey; a measurement harness (calls, latency, gate firing, waits, trend notice time) against simulated answers; web search against the stand-in's Responses API | Whether drafts sound like the customer, a real model's act or wait calls, real latency and cost per press, real web search quality and cost |

## The live checks that remain

None of these can be run without the operator's own accounts. In the order it makes sense to do
them:

1. **Deploy** to the host: TLS, the volume, `/healthz`, `/readyz`, the trusted-proxy check.
2. **Email**: sign up with a real address and confirm the verification email arrives, the code and
   the link both work, the HTML version renders, and opening the link on a phone moves the sign-up
   tab on by itself.
3. **Google sign-in** (if offered): create the OAuth client, then sign up, sign in and connect
   Google on a password account ([DEPLOYMENT.md](DEPLOYMENT.md#step-7b-sign-in-with-google-optional)).
4. **AI**: `ai-check` against a real key; then draft quality, latency and cost from real `ai_calls`
   rows.
5. **Stripe sandbox**: checkout, the `invoice.paid` grant, upgrade, downgrade and undo, cancel, a
   failed payment with grace, refund, dispute, and the customer portal.
6. **Extension**: publish it, install it from the store, sign in, and draft on real x.com.
7. **Stripe live**: one small real payment with a real card, then refund it and check the plan
   changes.
8. **Restore**: take an archive, copy it off the machine, and restore it into maintenance mode.

## Latest recorded runs

Local, on Windows, with mocked providers. The first four rows are from this release, run with
Python 3.11.9. The rows after them were run on 2026-09-22 and are kept as history: no newer result
is recorded here for them.

| Suite | Result |
|---|---|
| Server (this release) | 1,610 passed, 7 skipped (Linux-root and POSIX-only tests) |
| Engine (this release) | 3,354 passed, 19 skipped, 2 expected failures (run it serially) |
| Website (this release) | 281 passed |
| Extension (this release) | typecheck clean; 442 passed in 41 files (24 of them test the build verifier itself) |
| Site check (2026-09-22) | PASS (153 renders of 43 templates) |
| Extension builds (2026-09-22) | e2e and store builds passed the verifier |
| End-to-end, API journeys (2026-09-22) | 82 passed, no process left behind |
| End-to-end, browser journeys (2026-09-22) | 9 passed (5 files) in real Chrome with the real unpacked extension, no process left behind |
| AI evaluation, stand-in (2026-09-22) | `python -m e2e.eval`: 33 presses for four invented customers, 0 failed, 9 of 9 world-tool checks (claim searches, linked pages, story citations). `--golden`: all 33 golden answers come through as intended, 31 shipped intact and 2 held back as their golden answer says. Stand-in text only: nothing here judges a live model's writing |
| Inside the container image (2026-09-22) | server 1,493 passed, 1 skipped; engine 3,344 passed, 23 skipped, 2 expected failures (4 of the 23 are the niche-read checks on the evaluation personas, which live in `e2e/eval` and are not in the image; they pass everywhere else), as root, with no network, so the Linux-only privilege-separation tests run. The runtime image (686 MB) passed the release gate on its unpacked filesystem |
| Release gate (2026-09-22) | PASS, 881 files (everything Git would commit) and the store build (17 files) |

In continuous integration, on Linux, this release gives 1,611 passed and 6 skipped for the server
suite (one POSIX-only test runs there that Windows skips), and the same engine, website and extension
results as above.

On 2026-09-22 the server suite took about 80 seconds with 4 workers and the engine suite about 5
minutes.

The billing suite also has a seeded storm of random billing events
(`tests/billing/test_hardening_property.py`) that checks after every step that no account holds an
allowance a paid invoice does not justify. The normal run includes a small one (5 seeds of 40 steps).
`STARLING_BILLING_SOAK=1` widens it to 25 seeds of 120 steps, about half a minute; add
`STARLING_BILLING_SOAK_SEEDS=150` and `STARLING_BILLING_SOAK_STEPS=250` for the width the billing
hardening pass used, about three and a half minutes. It found the most serious defect of that pass
and is worth running after any change to `billing/`.

The extension count first fell from 331 to 313, because two modules left over from the local product
and their tests were retired, then rose to 359 with the security pass and to 393 with the UX and AI
passes; nothing that ships lost a test. It is 442 in this release.

**Why 19 engine tests skip.** They calibrate the quality gates and scrubbers against a private data
set that is not part of the repository, and they skip without it. Each of those tests calls
`pytest.skip` at run time when it finds neither the data set's fixture nor enough rows in the
database, with reasons such as "no archive fixture and none in the database". Thirteen come from one
helper in `test_gate_calibration.py`; the other six are one each in six files. They are not broken
tests and they are not a coverage gap you can close: the behaviour they would calibrate is exercised
by the other 3,354.

**The 7 skipped server tests** are the Linux-and-root privilege-separation checks. They run in the
container image's `test` target. In the image runs of 2026-09-22 recorded above, only 1 server test
skipped.

**The 2 expected failures** in the engine suite are marked as such in the suite itself.

## If a test fails after a change

| Symptom | Look at |
|---|---|
| Many server tests fail at import or fixture setup | the control database schema: run `migrate`, check `db-status` |
| `test_foundation` fails on plans or prices | `backend/server/config/plans.toml` and `ai_pricing.json` |
| Billing tests fail after a plans change | `amount_cents`, `currency` and `interval` must match what the fake Stripe serves |
| Services tests fail with "no price configured" | a model in `AI_MODEL_*` has no entry in `ai_pricing.json` |
| Site check fails with placeholder problems | a `[[PLACEHOLDER]]` escaped a legal page, or the draft banner is missing outside production |
| Release gate fails on a doc you wrote | you wrote a machine path, something shaped like a key or token, or a name listed in your own `scripts/release_private_patterns.json`. The built-in patterns are at the top of `scripts/check_release.py` |
| Extension verifier fails on host permissions | `WXT_STARLING_ORIGIN` does not match what the manifest expects, or a permission crept into `wxt.config.ts` |
