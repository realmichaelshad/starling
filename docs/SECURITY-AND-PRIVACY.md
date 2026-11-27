# Security and privacy

Last reviewed: 2026-09-18.

This page explains the security model agreed in `docs/architecture/CONTRACTS.md` (sections 2, 4.3,
6, 9, 11 and 13) in plain terms:

- Everything described here is implemented and covered by tests. The tests run against local
  stand-ins, never a live provider; see [TESTING.md](TESTING.md#mocked-versus-live). What ran live,
  and what never did, is under [What is unverified here](#what-is-unverified-here).
- This page is not legal advice. The legal pages in `web/templates/legal/` are draft text with
  placeholders, which the operator completes and has counsel review before launch.

## Summary

- Customers sign in on the website. The extension gets its own revocable device token through a PKCE
  sign-in flow.
- Owner admin access needs all of: a command-line grant, an approved owner email, a TOTP code and
  recent re-confirmation. Every admin action is audited.
- Each active customer's engine runs as its own operating-system user with its own data folder.
  Engines never hold AI, Stripe, email or session secrets.
- Secrets live in the hosting provider's secret store and are split so each process holds only what
  it needs.
- Usage is reserved before any AI call. Per-request caps, per-customer ceilings and global budgets
  bound what can be spent.
- Prompts, drafts, tokens, passwords and email bodies are never written to logs, and AI metering
  never stores prompt or output text.

## Customer sign-in (website)

- **Passwords.** Accounts use an email address and password. Passwords are hashed with Argon2
  (`argon2-cffi`); cost settings are `ARGON2_TIME_COST` (3), `ARGON2_MEMORY_KIB` (65536),
  `ARGON2_PARALLELISM` (4) and `ARGON2_MAX_CONCURRENT` (4).
- **Email verification.** The email must be verified before any draft; otherwise the API answers
  `email_unverified`.
- **Email tokens.** Verification, password reset and email change use single-use tokens that are
  stored only as hashes.
- **Web sessions.**
  - The session id is stored only as a hash, with a CSRF secret per session and both an idle and an
    absolute expiry.
  - The list of active sessions records an IP prefix (not the full address) and the browser's user
    agent.
  - **In code today:** when the site uses https, the session cookie uses the `__Host-` prefix (a
    Secure cookie bound to the exact host).
- **Password reset** signs the person out of every web session and revokes every extension device,
  through the account's "auth epoch", and then signs in the browser that used the link, on a new
  session. Whoever holds the link has just chosen the password and could sign in with it a second
  later, so making them type it again protected nothing.
- **Confirming a sign-up signs the person in.** The confirmation email carries a 6-digit code as well
  as the link. Both are tied to the browser that signed up (a random value in that browser's notice
  cookie, stored on the token row only as a hash), so they need two things at once: that browser and
  the inbox. The code is stored as a keyed HMAC, works once, and stops after 5 wrong tries (the link
  still works). The link opened in another browser confirms the address but signs nobody in there.
  The page where the person signed up then carries on by itself: it asks `GET /verify-email/status`
  every few seconds and, once that sign-up's own link has been opened, signs its own browser in
  (`POST /verify-email/continue`, once). That proves no more than the sign-in page would, since that
  browser typed the account's password, so it is refused after a password change and for a second
  sign-up whose password still waits on the token (below), which only the code or the link in its
  own browser can finish. The status answers only the browser that signed up, about its own
  sign-up, and says the same "waiting" for an address that already had an account. When
  an address whose first account was never confirmed signs up again, the second sign-up's password
  is held on the token and applied only if the same browser confirms, and every session and device
  of the first sign-up is revoked: an account parked on someone else's address cannot outlive its
  owner's arrival. None of this changes what the sign-up page shows, which is identical whether or
  not the address had an account.
### Sign in with Google

Optional (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`), implemented in `auth/google.py` (protocol),
`auth/service.py` (account rules) and `auth/google_routes.py` (pages).

- **The flow.** OpenID Connect authorization code flow with PKCE (S256), a fresh `state` and `nonce`
  per attempt, and `prompt=select_account`. What the return trip needs travels in one signed,
  HttpOnly, SameSite=Lax cookie that lives 10 minutes; `state` must match it, so nobody can finish a
  sign-in in someone else's browser. The code is exchanged directly with Google over TLS (client
  secret plus PKCE verifier), and the ID token is verified in full: RS256 only (whatever the token
  claims), Google's published keys (cached, refetched for an unknown key id at most once a minute),
  issuer, audience, authorized party, expiry and issue time (5 minutes of skew), and the nonce.
  Scopes: `openid email` only.
- **Matching accounts.** By the Google account's stable id (`sub`), never by address, in
  `account_identities` (one Google account per Starling account). A new Google account is matched
  to an existing Starling account by address only when Google is authoritative for that address,
  following Google's own guidance: Gmail addresses and Google Workspace accounts (the `hd` claim).
  `hd` is not compared with the address's domain: it names the organization's primary domain, and
  a Workspace address may sit on one of its verified secondary domains, which Google issues only to
  the organization that proved it owns them. Comparing would only send those people to the
  password step. A token that claims otherwise fails the signature check.
  For any other address, `email_verified` only says Google checked it once, so the account's
  password is required once before the two are joined (the Google sign-in waits in a 10-minute
  signed cookie for the password sign-in to finish). An unverified Google address is refused.
- **The pre-hijack.** If the existing account on an authoritative address was never confirmed, the
  Google user is its owner: the account is handed over, its old password removed, and every session,
  device and outstanding link revoked. An attacker who parked an account on someone's Gmail address
  in advance, knowing its password, loses it the moment the owner arrives.
- **New accounts.** Created confirmed, with no password (`accounts.NO_PASSWORD`, which can never
  verify, and a wrong-password sign-in against it costs the same time as any other). From the
  sign-up page an account is made at once; from the sign-in page the person is asked first, because
  they may have picked the wrong Google account. Sign-up limits, the disposable-domain list and the
  email-change hold on an address all apply.
- **Accounts without a password.** Where a password would be asked for (change email, delete the
  account, add a password), a sign-in within the last 10 minutes counts instead; after that the page
  offers "Confirm with Google", a fresh round trip that must return the same Google account. The
  owner area still needs a password and an authenticator. Google can be disconnected only once the
  account has a password.
- **Notices.** Connecting Google, disconnecting it and adding a password each send an email.
- **Secrets.** `GOOGLE_CLIENT_SECRET` stays in the gateway: the services process and every engine
  have it withheld by name.

**Session lifetimes.** A web session expires after **14 days idle** and **30 days** in any case. The
idle clock is extended at most once every 5 minutes, so an active browser does not write on every
request. "Sign out of other browsers" ends web sessions only; a password reset raises the account's
`auth_epoch`, which ends every session **and** every extension device at once.

**Email token lifetimes.** Address verification and email-change confirmation last 24 hours; a
password reset link lasts 60 minutes; the link sent to an old address so the owner can undo an email
change lasts 7 days. Every one is single use and stored only as a hash.

**Sign-in and account rate limits.**

| Action | Limit |
|---|---|
| Sign-up | 10 per hour per IP prefix |
| Sign-in | 60 per 15 minutes per IP prefix, and 10 per minute per email address |
| Repeated wrong passwords | after 5 failures for one address within an hour, each further attempt must wait: 2 seconds, then 4, 8, 16... capped at 300 seconds. There is no lockout, so the real owner always gets back in |
| Forgotten password | 20 per hour per IP prefix, 3 per hour per email address |
| Verification email resend | 1 per 60 seconds, 5 per day (signed out, the spacing rule is kept per browser, so the answer is the same whether or not the address has an account) |
| Sign-up code | 5 wrong codes per code, then that code stops (the link still works); the code form also counts toward the 30 per 10 minutes per IP prefix of every email-token form |
| Returns from Google | 30 per 10 minutes per IP prefix (each one costs a call to Google's token endpoint) |
| Billing page back from Stripe | 6 reads of Stripe per 10 minutes per account; past that the page still renders and the webhook still brings the change (the return link is a GET, so another site could load it repeatedly) |
| Extension connect (`/connect`) | 60 per 10 minutes |
| Device token exchange | 30 per 60 seconds |
| Authenticator codes and admin elevation | 5 per 5 minutes per account |
| Data export | 3 per hour |

IP limits count a network prefix (/24 for IPv4, /64 for IPv6), never a full address, and email limits
store a keyed hash of the address, never the address itself.

**Password rules.** 10 to 128 characters. A password is rejected if it is on the built-in common-password
list, if it has 2 or fewer distinct characters, or if it equals the email address or the part before
its `@`. Hashing is Argon2id with the cost settings above, with at most 4 hashes running at once so a
flood of sign-in attempts cannot exhaust the machine's memory.

**Disposable email domains.** `DISPOSABLE_DOMAINS_FILE` points at a plain text file of one domain per
line, which is reloaded whenever the file changes. **There is no built-in list**: leaving the setting
unset blocks nothing at all. If you want disposable domains blocked, you must supply and maintain the
file yourself.

## Owner admin access

Admin access requires all four:

1. The account is granted admin with the command line on the server.
2. The account's verified email is listed in `STARLING_OWNER_EMAILS`.
3. TOTP two-factor authentication. The TOTP secret is stored encrypted.
4. Re-confirmation within the last 15 minutes before any admin change (POST).

Other rules:

- Every admin action writes an `admin_audit` row: who, what, which account, parameters, IP prefix
  and time.
- Admin pages never read customers' engine databases.
- **In code today:** production refuses to start while `STARLING_OWNER_EMAILS` is empty.
**Granting admin.** From inside the container:

```
python -m starling_server.cli admin-grant --email you@your-domain.example
python -m starling_server.cli admin-revoke --email you@your-domain.example
```

The grant is refused unless the account already exists, is verified, is active, and its address is in
`STARLING_OWNER_EMAILS`. It prints `admin-grant: done for account <uuid>`, or a refusal and exit 1.

**Enrolling the authenticator.** Sign in on the website and open `/admin`; you are sent to
`/admin/totp`, which asks for the account password plus a code from an authenticator app. Every admin
POST afterwards needs an elevation from the last 15 minutes, which `/admin/elevate` grants against a
fresh code. A used code cannot be replayed: the time step is recorded.

**There is no QR code and there are no recovery codes.** Copy the secret into your password manager
while you enrol.

**Recovering a lost authenticator.** There is no self-service route. Run `admin-revoke --email ...`,
which removes admin access, the enrolment and any elevation, then `admin-grant --email ...` and enrol
again. Because that needs shell access to the container, enrol a second owner address as well so one
lost phone does not lock you out.

## Isolation between customers

In production, one container runs these processes:

| Process | Operating-system user | Listens on | Secrets it holds |
|---|---|---|---|
| supervisor | root | the spawner socket (gateway only) | none |
| gateway | `starling-gw` | port 8080, behind the host's TLS proxy | `STARLING_SECRET_KEY`, Stripe keys, SMTP credentials, `STARLING_SERVICES_TOKEN_KEY` |
| services | `starling-svc` | the services address set by `STARLING_SERVICES_LISTEN`; engines and the gateway connect to it | `AI_API_KEY`, `AI_API_KEY_SECONDARY`, `STARLING_SERVICES_TOKEN_KEY` |
| engine, one per active customer | its own user id (200000 + the customer's number) | its own socket | only its per-engine tokens, received at start-up |

How the separation is enforced:

- **Files.** Each customer's data lives in `/data/tenants/<customer number>/`, owned by that engine's
  user, with mode 0700 so no other engine can read it.
- **Sockets.** Each engine's socket directory lets only that engine and the gateway connect.
- **Engine tokens.** The gateway gives each engine a random worker token when it starts. The engine
  accepts requests only with that token.
- **Services tokens.** Callers of the services process present a signed token, checked without a
  database lookup:
  - Format: `svc1.<subject>.<expires_at>.<nonce>.<signature>`, where the signature is HMAC-SHA256
    with `STARLING_SERVICES_TOKEN_KEY`.
  - An engine's token names its customer and is valid for 24 hours. A fresh one is minted every
    time the engine starts.
  - The gateway's own admin tokens are valid for 5 minutes.
  - The services process compares signatures in constant time and rejects expired tokens and
    unknown subjects.
  - Because the customer comes from the signed token, a token for customer A can never bill
    customer B.
  - Engines receive only their token, never the key that signs it.
- **Memory.** Engine memory is limited (`ENGINE_MEMORY_SOFT_MB` 512, `ENGINE_MEMORY_HARD_MB` 768),
  and idle engines stop after `ENGINE_IDLE_SECONDS` (1800).
- **In code today:** production refuses to start unless `STARLING_ISOLATION=os`. The single-user
  mode `none` is allowed only in development and automated tests.

Planned tests: a two-customer canary test proves no data crosses between customers through
responses, AI prompts or files. A Linux container test proves engine A cannot read engine B's files
or socket.

Both exist and pass. The canary runs in every server test run; the file-and-socket ownership checks
are Linux-and-root only and run inside the container image's test target, where they passed on
2026-09-22 ([TESTING.md](TESTING.md#the-image-and-the-clean-checkout)).

## Secrets

| Secret | Purpose | Held by | Rotation |
|---|---|---|---|
| `STARLING_SECRET_KEY` | web sessions, CSRF tokens, encryption of stored authenticator secrets, and the key behind deletion tombstones | gateway and operator commands | Set `STARLING_SECRET_KEY_PREVIOUS` to the old value for an overlap of about 30 days, then clear it. See the warning below. |
| `STARLING_SERVICES_TOKEN_KEY` | signs services tokens (base64url, at least 32 bytes) | gateway, services and the supervisor; never engines | Set the new value and restart the whole container, so all three processes change together. There is no previous-key overlap; in-flight drafts fail and retry. |
| `AI_API_KEY`, `AI_API_KEY_SECONDARY` | OpenAI | services | [AI-PROVIDER.md](AI-PROVIDER.md#api-keys-and-rotation) |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_SECRET_PREVIOUS` | Stripe | gateway | [PAYMENTS.md](PAYMENTS.md#rotating-secrets) |
| `SMTP_PASSWORD` | sending email | gateway | in the email provider's dashboard |
| `BACKUP_PASSPHRASE` | encrypting backup archives (AES-256-GCM, key derived with scrypt) | the supervisor's backup thread and the operator commands; keep a copy in the password manager | A new passphrase applies only to new archives. Keep the old one until every archive encrypted with it has been pruned. |

Rules:

- Keep secrets only in the host's secret store. Never commit a `.env` file; `.gitignore` excludes
  them, and `.env.example` contains no secrets.
- **In code today:** `STARLING_SECRET_KEY` and `STARLING_SERVICES_TOKEN_KEY` must decode to at least
  32 bytes. `.env.example` shows a command that generates a suitable value.
- **In code today:** production refuses to start without `STARLING_SECRET_KEY` (gateway) and
  without `STARLING_SERVICES_TOKEN_KEY` (gateway, services and supervisor).
- **In code today:** in development and automated tests, if `STARLING_SERVICES_TOKEN_KEY` is empty,
  it is derived from `STARLING_SECRET_KEY` so one `.env` works for both processes. Production needs
  a separate, explicit value so the services process never holds the gateway's session secret.
- **In code today:** `scripts/check_release.py` scans a folder for provider keys, private keys,
  personal data and forbidden files such as databases, env files and logs.

### Service-to-service settings

These settings control how the gateway and engines reach the services process (defaults are in code
today):

| Setting | Default with OS isolation (production) | Default without isolation (development, automated tests) | Meaning |
|---|---|---|---|
| `STARLING_SERVICES_TOKEN_KEY` | none; required | derived from `STARLING_SECRET_KEY` when empty | signs the services tokens described above |
| `STARLING_SERVICES_URL` | `unix:/run/starling/services.sock` | `http://127.0.0.1:8766` | where the gateway reaches the services process |
| `STARLING_SERVICES_LISTEN` | `unix:/run/starling/services.sock` | `tcp:127.0.0.1:8766` | where the services process listens |

The services process is never exposed on the public port: it accepts a Unix socket or a loopback
TCP address, and refuses any other TCP address. The Unix socket default does not work yet under OS
isolation, because the supervisor creates `/run/starling` as root and the services user cannot bind
a socket in it. The reference deployment (`deploy/fly.toml`) sets both addresses to loopback TCP
instead, which is what the beta ran ([CONFIGURATION.md](CONFIGURATION.md)). A loopback port has no
group restriction: any process in the container can connect to it, so the signed services token is
what limits a caller.

## Extension sign-in

Starling uses Chrome's identity API with an authorization code flow and PKCE:

1. The extension creates a random code verifier and state value, then opens the website's
   `/connect` page with `chrome.identity.launchWebAuthFlow`.
2. The gateway accepts only extension ids listed in `STARLING_EXTENSION_IDS`, and only the redirect
   address `https://<extension id>.chromiumapp.org/connect`.
3. The person signs in if needed. The approval page shows the extension id and device name, and
   approval is a form submission with CSRF protection, never a plain link.
4. The gateway issues a 256-bit code. The code is single-use, valid for 120 seconds, and tied to the
   challenge, the account, the extension id and the redirect address.
5. The extension checks the state value and exchanges the code plus verifier at
   `POST /api/v1/devices/token`.
6. The extension receives a device token (`stl_dev_...`) with these properties:
   - 256 bits of randomness, stored on the server only as a SHA-256 hash
   - expires after 60 days without use, and after 1 year in any case
   - revoked on sign-out, from the website's device list, and on password reset

Where the token is kept, and what the page can reach:

- The device token lives in `chrome.storage.local`, and the extension's worker closes that area to
  everything but itself and the extension's own pages (`setAccessLevel`, on every start). The content
  script runs inside x.com's renderer and cannot open it: it never calls `chrome.storage`, and its own
  five preferences go through one message with a fixed list of keys (`extension/shared/page-prefs.ts`).
- The X page cannot drive the panel. Keys the page makes up are ignored (`isTrusted`), there is no
  `postMessage` bridge and no `externally_connectable`, and the panel lives in closed shadow roots.
- The address of the page a post was seen on is sent with its path only: never the query string
  (what the user searched for) or the fragment.

Boundaries:

- `/api/v1` accepts only device tokens, ignores cookies and sends no CORS headers.
- Device tokens cannot reach the website's account, billing, password or deletion pages.
- Every POST and DELETE from the extension carries an `Idempotency-Key`.

## Protection against unauthorized or abusive usage

| Risk | Control |
|---|---|
| A stolen device token | Stored hashed; expires; each device can be revoked; a password reset revokes all devices. |
| Creating many accounts for free drafts | Email verification; an optional disposable-domain list; per-plan daily limits; sign-up limited to 10 per hour per network prefix; and a tombstone kept after deletion. Each account stores an `allowance_key`: a normalised form of the address that collapses plus tags and, for Gmail, dots, so `name+x@`, `n.a.m.e@` and `name@` are one key. Deletion writes a tombstone keyed by a hash of that key, and signing up again with a matching address gives a **zero** free allowance until the customer subscribes. The sign-up IP prefix is stored on the account for abuse investigation. |
| Runaway AI spend | Per-request caps (12 calls, 8,000 input and 600 output tokens per call), per-customer fair-use ceilings, a background cap, global daily and monthly budgets, and the OpenAI project hard limit ([AI-PROVIDER.md](AI-PROVIDER.md#spending-safeguards)). |
| Double charges from retries | Idempotency keys: a repeated request replays the stored result instead of charging again. |
| Parallel flooding | One active request per account plus one short wait, and per-plan rate limits. |
| Payment abuse | Checkout sessions are created only by the server; prepaid cards are blocked by default (`STRIPE_BLOCK_PREPAID`). |
| Forged Stripe webhooks | HMAC signature check with a 300-second tolerance and constant-time comparison; the previous secret is accepted only during a rotation. |
| Server-side request forgery through links in posts | The world service fetches only public http(s) addresses and connects to the validated IP. Pages are limited to 500 KB, 8 seconds and 3 redirects (each re-checked); images to 5 MB. |
| Attacks on the website | CSRF checks on form submissions and strict security headers (below). |
| Development shortcuts left on | `/_dev/*` and `/_fake/*` return 404 in production, and production refuses unsafe configuration. |
| Abuse traced back through OpenAI | Each AI call carries a hashed customer identifier (`safety_identifier`), so abuse can be tied to one Starling customer without revealing who they are. |

**In code today, security headers on every response:**

- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- a `Permissions-Policy` that turns off camera, microphone, geolocation and payment
- `Cross-Origin-Opener-Policy: same-origin`
- `X-Frame-Options: DENY`
- a Content Security Policy on HTML pages that allows only Starling's own origin, with form posts
  also allowed to Stripe Checkout and the Stripe billing portal
- HSTS for one year when the site uses https
- an `X-Request-Id` header

**In code today, production refuses to start unless:**

- the public URL uses https
- OS isolation is on
- the session and services keys are set
- SMTP email is configured
- extension ids and owner emails are listed
- no business-detail placeholders remain
- the Stripe key is live and matches the mode, when a plan is paid (a plans file with no paid plan
  runs without Stripe, and `STRIPE_ALLOW_TEST_MODE=true` allows a rehearsal with Stripe's sandbox)

## How data flows for one draft

1. The person presses a Starling button on x.com. The extension sends the request, including the
   post's text and image references, with its device token to `POST /api/v1/assist/stream` over
   HTTPS.
2. The gateway checks, in order:
   - the account is active and its email verified
   - consent is current and the service is not paused
   - the plan, rate limits and concurrency allow the request
   - drafts are available and the fair-use ceiling is not reached

   It then reserves the draft.
3. The gateway starts or reuses that customer's engine and forwards the request over the engine's
   local socket.
4. The engine builds prompts from the post, the customer's saved writing samples and preferences,
   and the shared news pool. It may ask the world service to fetch a linked page or an image. When
   the post makes a checkable, time-sensitive claim that neither its links nor the news pool cover,
   it may ask for one web search: the query is the claim's own sentence from the public post, never
   a protected post and never the customer's own writing, and the services process sends it to
   OpenAI's `web_search` tool with `store: false`. The pages whose facts reach the writer are listed
   under the draft as links.
5. The engine calls the services AI proxy with its signed token and the reservation id. The proxy
   checks the reservation is active and within caps and budgets, then calls OpenAI with the
   operator's key.
6. Text streams back from OpenAI through services, the engine and the gateway to the extension. When
   the first text reaches the person, the draft counts as used.
7. The gateway charges or releases the draft and sends the final result and updated usage. The
   result is kept for 15 minutes so a retried request is not charged twice.
8. The person reviews and edits the draft in X's text box and publishes it themselves.

### The same draft from the writing page (phones)

Chrome extensions do not run on phones, so the website has a writing page (`/write`,
`backend/server/starling_server/web/write.py`). It is the extension's drafting with a website session
instead of a device token:

1. The person signs in on the website. Before anything can be sent from the page, it shows its own
   disclosure once (what is sent, who receives it, the promises) and records the answer in `consents`
   as kind `web_disclosure`, at its own version. Withdrawing it on the page stops everything below.
2. They paste the post they are answering (its words, optionally the author), or its link. A link to a
   post on X goes to `POST /write/link`; the gateway passes only the post's own address to the services
   process, which asks X's published oEmbed endpoint (`publish.twitter.com`) for that post's words and
   author through the same SSRF-safe fetcher as page fetches. X receives the link and the server's
   address, never anything about the person. Nothing scrapes x.com or calls X's internal endpoints.
3. "Write it" sends a JSON request with the session cookie, the CSRF token in a header and an
   idempotency key to `POST /write/draft`. From here it is steps 2 to 7 above, through the same function
   as `POST /api/v1/assist` (the same allowance, rate limits, fair use, pause, slot and settlement): the
   gateway builds the engine request from what was pasted (the post's words, author, link, its id and
   the creation time the id carries, links in its text) and leaves out what only the extension can see
   on x.com. The answer is not streamed, so nothing is shown before the final draft and a dropped
   connection never charges a draft that was not delivered; the phone collects a finished draft with
   the same key without paying twice.
4. The person copies the draft, or follows the link that opens X's own post box with the draft in it,
   and presses Post in X themselves.

Writing samples added on the page are the engine's own `voice` material, the same list the extension's
panel fills. The page keeps the last Reply / Quote / New post choice in the browser's local storage,
and the installed app's service worker keeps only the offline page (fetched without cookies).

## Data inventory

The control database schema is in code today (`backend/server/starling_server/db/migrations.py`):

| Data | Where | Notes |
|---|---|---|
| Email, password hash, verification time, status, admin flag, encrypted TOTP secret, sign-up IP prefix | control database `accounts` | |
| Email tokens (hashes only) | `email_tokens` | verification, reset, email change |
| Web sessions (hashed id, CSRF secret, IP prefix, user agent) | `web_sessions` | |
| Extension sign-in requests and devices (hashed codes and tokens, device name, extension version, last use) | `connect_requests`, `devices` | |
| Consent records (kind, version, passive-learning choice, times) | `consents` | The extension's disclosure (`extension_disclosure`) and the writing page's (`web_disclosure`) are separate rows |
| Stripe customer and subscription mirror, checkout sessions, price map | `stripe_customers`, `subscriptions`, `checkout_sessions`, `price_map` | Card details stay with Stripe. |
| Plan periods, usage reservations, ledger and counters | `entitlement_periods`, `usage_reservations`, `credit_ledger`, `usage_counters` | A reservation's result is kept 15 minutes for replay. |
| AI metering (tokens, cost, status, times) | `ai_calls` | Never stores prompt or output text. |
| Service-wide budget counters | `budget_counters` | |
| Stripe webhook events, including the event payload | `billing_events` | |
| Admin audit log | `admin_audit` | |
| Outgoing emails (recipient, subject, body) | `email_outbox` | |
| Records kept after account deletion (hashed account identifier, Stripe customer id) | `tombstones` | |
| Engine state and service settings | `engines`, `service_state` | |
| Each customer's engine data | `/data/tenants/<customer number>/`, owned by that engine's user, mode 0700 | `starling.db` (SQLite in WAL mode, plus its `-wal` and `-shm` files): writing samples, drafts and their candidates, ratings, the queue, saved pages, snapshots, goals, campaigns, experiments and preferences; `voice_cards` (the model-written voice card, versioned, with the ids of its fixed examples) and `topic_candidates` (stories found for the account's beat, with their pages). `image-cache/`: images fetched for vision. `press-timing.jsonl`: request timings. The hosted engine writes no settings file and no dataset exports. |
| Encrypted backup archives | `BACKUP_DIR` (default `/data/backups`) | A compressed tar of the control database and every customer folder, with a manifest of sizes and SHA-256 hashes, encrypted with AES-256-GCM under a key derived from `BACKUP_PASSPHRASE` with scrypt. Written nightly at 03:00 UTC. |
| Server logs | the host's log service | JSON lines; never prompts, drafts, tokens, passwords or email bodies |

## Third parties that receive data

| Third party | What it receives | Source for its own terms |
|---|---|---|
| OpenAI | Prompts (post text and context, writing samples, preferences, news and page text), post images, a hashed customer identifier and, for a web search, either a query built from the public post being answered or a topic-research query naming only the account's beat labels. A topics answer contains no account data and is shared between accounts asking the same query for 60 minutes | API data is not used for training by default; abuse monitoring logs are kept up to 30 days (https://developers.openai.com/api/docs/guides/your-data). Web search results must be shown with visible, clickable citations (https://developers.openai.com/api/docs/guides/tools-web-search), which the draft card does |
| Stripe | Customer email and billing details entered on Stripe Checkout; subscription data | https://stripe.com |
| Email provider | Recipient address and email content | the chosen provider's terms |
| Hosting provider | Everything stored on the server, on an encrypted volume | https://fly.io/docs/volumes/overview/ (reference host) |
| Public websites | Server-side requests for pages and images linked in posts, and for news feeds, with no customer identifiers | – |
| X (oEmbed) | Only when a person gives the writing page a link to a post on X: that post's address, from the services process, with no customer identifiers | X's developer terms cover its embed endpoint; confirm the current terms before launch |

With `AI_PROVIDER=anthropic`, Anthropic takes OpenAI's place in this table. It receives the same
prompts and post images, and the hashed customer identifier as `metadata.user_id`. That provider has
no web search. The terms cited in the OpenAI row are OpenAI's, so read Anthropic's own terms and
retention policy ([AI-PROVIDER.md](AI-PROVIDER.md#anthropic-ai_provideranthropic)).

## Retention and deletion

| Data | Planned retention | Status |
|---|---|---|
| Stored results for replay | 15 minutes | contract |
| Extension sign-in codes | 120 seconds | contract |
| Device tokens | 60 days without use; 1 year at most. `last_used_at` is written at most every 10 minutes | Revoked and expired device **rows** are not swept on a schedule; they are deleted when the account is deleted |
| Web session rows | deleted 7 days after absolute expiry or revocation | swept opportunistically |
| Extension connect requests | deleted 1 day after expiry | swept opportunistically |
| Stored results for replay | cleared 15 minutes after the request is committed | – |
| Failure counts (`request_errors`) | 30 days | pruned daily by the dashboard's background job |
| Sent email copies (`email_outbox`) | 30 days after sending, in SMTP mode | – |
| Account deletion | Immediate on request, in one pass; a failed step leaves the account `deleting` and is retried 30 seconds after the next start-up and then hourly | Deletion needs the typed word `DELETE` plus the password. It signs every session and device out, cancels subscriptions **immediately** in Stripe (the Stripe customer itself is not deleted), deletes the customer's engine folder and their capture spool, anonymises the account row and writes a tombstone. Billing and usage history rows stay, so revenue records survive |
| Data export | Produced on demand, not stored; 3 per hour | One JSON file downloaded directly: `exported_at`, then `account`, `devices`, `sessions`, `consents`, `billing` and `usage` from the control database, plus `drafting` from the customer's engine (own material, drafts and their candidates, ratings, queue, saved pages, snapshots, goals, campaigns, experiments and preferences). It contains **no** password, authenticator secret, session id or device token |
| `billing_events` payloads, `ai_calls`, `admin_audit` | **not pruned** | Nothing deletes them today. Decide a period with counsel and prune by hand until there is code for it |
| Server logs | the host's own retention | JSON lines; never prompts, drafts, tokens, passwords or email bodies |
| Backup archives | the newest archive of each of the last 7 days, 4 weeks and 3 months | pruned after each nightly run, and by `backup-prune` |
| Fly.io volume snapshots | 5 days by default, configurable from 1 to 60 | https://fly.io/docs/volumes/overview/ |
| OpenAI abuse monitoring logs | up to 30 days | https://developers.openai.com/api/docs/guides/your-data |
| Stripe records | kept by Stripe under its own policies | the operator's decision, with counsel |

## Security invariants

Each rule below has a test in the server suite, and `docs/architecture/CONTRACTS.md` section 13
lists the full set of 14. The Linux-and-root ones (7, 8 and the file-mode checks) run only inside the
container image's test target; the recorded runs are in
[TESTING.md](TESTING.md#the-image-and-the-clean-checkout).

1. `/_dev/*` and `/_fake/*` return 404 in production.
2. Admin access needs a command-line grant, a verified owner email, TOTP, and re-confirmation within
   15 minutes for every change; every admin action is audited.
3. `/api/v1` accepts only device tokens; website session cookies are ignored there.
4. Device tokens cannot reach website account, billing, password or deletion routes.
5. Engines never receive AI, Stripe, SMTP or session secrets.
6. Logs never contain prompts, drafts, tokens, passwords or email bodies.
7. A two-customer canary test proves no data crosses customers through responses, AI prompts or
   files.
8. Engine A cannot read engine B's files or socket.
9. Admin pages never read customer databases.
10. Webhook signature checks reject wrong secrets, modified bodies and stale timestamps.

## If something goes wrong

- **AI key exposed:** follow [AI-PROVIDER.md](AI-PROVIDER.md#api-keys-and-rotation).
- **Stripe key or webhook secret exposed:** follow [PAYMENTS.md](PAYMENTS.md#rotating-secrets).
- **A customer account compromised:** revoke its devices and reset its password. A reset ends all
  sessions and devices.
- **Service pause:** the owner can pause the service, and customers then see `service_paused`.
  Pause with `python -m starling_server.cli ai-pause --reason "..." [--message "..."]` or from the
  dashboard's AI page, and resume with `ai-resume`. Incident steps for every alert are in
  [OPERATIONS.md](OPERATIONS.md).
- **Security contact:** the site serves `/.well-known/security.txt` with a `Contact:` line built
  from `STARLING_SUPPORT_EMAIL`, an `Expires` date a year out and `Preferred-Languages: en`. Set
  `STARLING_SUPPORT_EMAIL` to an address you actually read; production refuses to start while it is
  still a placeholder.

## Before you rotate `STARLING_SECRET_KEY`

It signs web sessions and CSRF tokens, encrypts stored authenticator secrets, and keys the deletion
tombstones. Rotating it:

- ends sessions once the overlap with `STARLING_SECRET_KEY_PREVIOUS` is removed;
- re-encrypts each stored authenticator secret the next time it is used, so long as the overlap is
  in place;
- **makes older deletion tombstones unmatchable**, because they are keyed by an HMAC of the old
  secret. An address whose account was deleted before the rotation can then sign up again and receive
  a full free allowance.

That last point is the reason not to rotate this key on a routine schedule. Rotate it when you have
reason to think it leaked. The step-by-step is in
[OPERATIONS.md](OPERATIONS.md#11-rotating-keys-and-secrets).

## What is unverified here

Every control on this page is implemented and tested locally, and the Linux-only isolation tests
passed inside the container image's test target on 2026-09-22
([TESTING.md](TESTING.md#the-image-and-the-clean-checkout)).

The author ran Starling as a free public beta in autumn 2026 on Fly.io, with the Anthropic provider
(Claude Haiku 5.5), web search off, a free-only plan file and Stripe not configured. The beta has
ended and no hosted Starling service is running now. The one finding from the beta recorded on this
page is the services address under [Service-to-service settings](#service-to-service-settings).
Real Stripe payments were never taken, and the billing tests run against the stand-in Stripe in this
repository.

What has not been done:

- a real restore from a real archive on a real volume;
- any security review by a third party. The project's own security review, with everything it found
  and what was left as it is, is [SECURITY-REVIEW-2026-09-20.md](SECURITY-REVIEW-2026-09-20.md);
- a penetration test of a deployed service, which that review recommends before taking payments at
  any volume.
