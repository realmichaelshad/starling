# Security review, 2026-09-20

A security review of the whole product by four independent code reviewers (accounts and the owner
dashboard; the customer API, the isolation between customers and the AI/fetch services; the Chrome
extension; the per-customer engine), a black-box pass against the running site, and a check of every
pinned dependency against the OSV vulnerability database. Each finding below was reproduced before
it was fixed, and each fix has a test that fails without it.

This is a record of what was found in this code on this date, by people reading it and attacking a
local copy. It is not a penetration test of a deployed service, and it is not a guarantee. The
[security model](SECURITY-AND-PRIVACY.md) describes how the product is meant to work; this document
says where it did not.

## Found and fixed

| # | Severity | What was wrong | What an attacker could do | Fix |
|---|---|---|---|---|
| 1 | **Critical** | The engine's safety scan had one expression that backtracked quadratically over any long run of letters, digits, `+` or `/` | One post on X containing a base64 blob, a hash dump or a long URL froze the engine of every customer who pressed the button on it: 13 seconds at 3,200 characters, about 20 minutes for one long post, hours at the scan's limit, with the interpreter lock held so not even a cancel was answered | The rule is rewritten to anchor on the rare thing (`api key`) and look either side of it; 100,000 hostile characters now take under 2 ms. Checked against the old rule on 60,000 generated texts when written, and on 20,000 in the suite, with no difference. `tests/test_safety_scan_is_linear.py` |
| 2 | **High** | `/_dev/outbox` (email bodies, for local development) was gated on `STARLING_ENV` alone, and that variable defaults to `development` | A deployment that set everything correctly and merely forgot `STARLING_ENV` started without a warning. Anybody could then request a password reset for any address and read the reset link from `/_dev/outbox`: full takeover of any account, including the owner's | The route needs three independent conditions (not production, the site's own address is this machine, email is in outbox mode); the deployed image defaults to production so a forgotten variable fails closed; development mode on a public address is reported at start-up. `tests/core/test_hardening_devroutes.py` |
| 3 | **High** | The routes that reach an engine without using a draft (the posting-time helper, the customer's saved material) checked only that the account was not suspended | Engines are the scarcest thing on the machine (six by default) and one with a request in flight is never evicted. A few throwaway accounts that never confirmed an email could hold all of them, and every paying customer got "Starling's AI isn't responding" | Those routes now need a confirmed email and the accepted disclosure, are rate limited per account, and share the account's single request slot. `tests/engines/test_hardening_unmetered.py` |
| 4 | **High** | Every refused API request, every request from a caller already over a rate limit, and every `/readyz` probe cost one synced write on the control database's single writer | No account needed: a flood of requests with no token queued a transaction each, behind which every sign-in, draft and Stripe webhook waited, until they timed out | Refusals are counted in memory with at most one flush waiting on the writer; a caller over a limit is answered from memory until the window ends; the readiness answer is reused for five seconds. `tests/core/test_hardening_flood.py` |
| 5 | Medium | The email-change "undo" link restores the old address, and that address became free the moment the change was confirmed | With a hijacked session: change the address, then register a throwaway account on the old one. The victim's undo link then could not restore it, reported plain success anyway, and one password reset gave the account back to the attacker for good | An address cannot be registered while its owner can still take it back; an unconfirmed account parked on it gives it up; and if a confirmed account holds it the page says so, in red, instead of "success". `tests/core/test_hardening_revert.py` |
| 6 | Medium | The X page could drive Starling's chat: a keyboard guard handed keys to the panel's own handler without checking they were real | x.com's script (or anything injected into it) could wait until the user was typing and dispatch a synthetic Enter: a half-written message and its attachments sent, and a draft used, at a moment the page chose | Keys the page made up (`isTrusted === false`) are ignored, as the sibling handler already did. `tests/keyboard-isolation.test.ts` |
| 7 | Medium | The extension's device token lived in `chrome.storage.local`, which content scripts can read, and content scripts run inside x.com's renderer. Only a test that searched the source for the key's name stood in the way | No live path was found. But one added storage read, or one compromised dependency in the content script, would have exposed a bearer token valid for up to a year | The worker closes that storage area to everything but itself and extension pages on every start; the content script no longer calls `chrome.storage` at all, and its own five preferences go through a fixed-list message. `tests/no-token-in-content.test.ts`, `tests/page-prefs.test.ts` |
| 8 | Medium | A store build accepted `https://app.example.com` as its service address, the build verifier certified it, and a unit test asserted it | `example.com` is a real host that belongs to IANA. A package built with the commonest placeholder there is would send every token and every post to a server nobody here runs | The documentation domains are refused by every build. `tests/config.test.ts` |
| 9 | Medium | The services process (it talks to the AI provider and fetches pages from the internet) inherited `STARLING_SECRET_KEY_PREVIOUS`, the old gateway key that stays valid for the 30 days of a rotation | Code execution in that process would have yielded the key that signs sessions and encrypts the owner's two-factor secret | It is excluded by name, and anything whose name contains `SECRET`, `PASSWORD` or `PASSPHRASE` is now withheld from that process. `tests/services/test_hardening_secrets_and_fetch.py` |
| 10 | Medium | The page and image fetcher honoured a customer's services token without checking the customer, unlike the AI proxy | A token taken from a compromised engine kept working as an outbound fetch proxy, from the owner's address, for the rest of its day after the owner had suspended the account | The fetcher checks that the account is active (answers are reused for 30 seconds). Same test file |
| 11 | Medium | Lists in the engine's request models capped how many items they held, not how long each was; several fields and every free-form dictionary had no limit | Bounded only by the 1 MiB body limit: it is what carried 100 KB into finding 1, and it let unbounded text into the prompt and the customer's database | Every attacker-authored string, list item and dictionary has a finite limit, generous against X's real limits. `tests/test_request_field_limits.py` |
| 12 | Low | `MAX_DEVICES_PER_ACCOUNT` was checked when a connection was approved, but a device only exists once its code is redeemed | Approve thirty requests, then redeem them: thirty devices on an account limited to twenty | The limit is checked again where the device is created. `tests/core/test_hardening_revert.py` |
| 13 | Low | The client that carries `AI_API_KEY` was the only HTTP client that honoured `HTTPS_PROXY` and `SSL_CERT_FILE` from the environment | Whoever can set an environment variable on the container could route the key through their own proxy | `trust_env=False`, like every other client |
| 14 | Low | The build verifier's check on `web_accessible_resources` had a clause that could never be true, ignored `extension_ids`, and the verifier never looked for remotely loaded code. The documentation said the verifier was tested; nothing tested it | A regression in what the package exposes or loads would have been certified | The check is rewritten; `eval`, `new Function`, remote scripts, remote `import()` and `importScripts` fail a build; and the verifier has 22 tests. `tests/verify-build.test.ts` |
| 15 | Low | The image handed to X's composer took its MIME type and file name from the service's say-so | A compromised service could label an image `text/html` named `../invoice.html` | Both come from the validated data URL. `tests/image-attach.test.ts` |
| 16 | Low | The page address sent with each request was `location.href` | The user's search terms on X (`/search?q=...`) and share-tracking identifiers went to the service, which the disclosure does not mention | Scheme, host and path only. `tests/page-address.test.ts` |

Smaller hardenings in the same pass: a percent-encoded host is refused by the fetcher before it can
reach a resolver; restoring a backup no longer follows a symbolic link when setting file modes; the
onboarding page checks who sent a message; the deployed image no longer contains `pip` or
`setuptools` (image scanners flag `setuptools`, and nothing installs packages at run time).

## Findings that were not about security

Looking at finding 2 showed that the documented deployment sequence could not be followed. The
sequence says "deploy with the Stripe sandbox first", but production mode refused
`STRIPE_MODE=test`, so the only way to rehearse payments on the deployed host was
`STARLING_ENV=development` on the public internet, which is exactly the configuration finding 2 is
about. `STRIPE_ALLOW_TEST_MODE=true` is now the explicit way: every production check still applies,
every page carries a "payments are in test mode" banner, nothing is indexed, and the owner dashboard
keeps saying so until live keys are in ([DEPLOYMENT.md](DEPLOYMENT.md#step-7-stripe)).

A separate pass checked what customers are told against what the code does. Customers were told
four things the code did not do, about what deleting an account does to the subscription and to
backups, about cookies, and about age. The pages now say what happens. Fourteen other statements to
customers were checked against the code and are true.

## Checked and found sound

The reviewers were asked to say what they verified, not only what they broke. In short:

- **Accounts.** argon2id with a bounded number of concurrent hashes; sign-up, sign-in and
  forgot-password answer identically whether or not the address exists, and do the same work; login
  throttling keys on the normalised address, so case, Unicode and plus-addressing do not dodge it;
  `X-Forwarded-For` is read only from a trusted proxy, right to left; session ids are 256-bit,
  stored only as a hash, in a `__Host-` cookie, rotated on sign-in, elevation and password change;
  every email token is 256-bit, hashed at rest, single-use inside one transaction, and never
  consumed by a GET.
- **CSRF and redirects.** Every POST outside the bearer-token API and the Stripe webhook verifies a
  token bound to the session (or to a pre-session cookie, which also stops login CSRF); `Origin: null`
  is accepted only with `Sec-Fetch-Site: same-origin`, which a page cannot set; `next` parameters that
  are protocol-relative, backslashed, absolute, `javascript:` or contain control characters fall back
  to the default.
- **Extension sign-in.** S256 only; the redirect must be the listed extension's own
  `chromiumapp.org` address; codes are 256-bit, single-use, 120 seconds, bound to challenge, account,
  extension and redirect; a replayed code revokes the device it created; cookies never authenticate
  `/api/v1` and a device token never reaches an account, billing or admin page.
- **Owner dashboard.** A command-line grant, an owner address re-checked on every request,
  time-based codes that cannot be replayed inside their window and are limited to five attempts per
  five minutes, elevation for fifteen minutes, CSRF and one audit row on every action, 404 rather
  than 403 for everybody else.
- **Between customers.** Every id a customer can name (idempotency key, request id, device id,
  checkout session) is looked up together with the caller's account; engine routes, methods and
  paths are server-side constants, and only three request headers are forwarded; no engine header
  reaches the browser; services tokens are compared in constant time and the AI proxy looks up
  reservations and periods by token subject, so a compromised engine cannot bill or read another
  customer.
- **Fetching the internet.** 41 crafted addresses were tried against the fetcher: loopback in
  decimal, hex, short and IPv6-mapped forms, link-local and metadata addresses, private ranges,
  credentials, odd ports and schemes are all refused; the connection is pinned to the address that
  was validated, so DNS rebinding fails; every redirect is validated again; bodies are capped after
  decompression. The AI proxy refuses remote image URLs outright.
- **Backups.** scrypt, then AES-256-GCM with the header as associated data, so reordering,
  truncation and bit flips are detected and a wrong passphrase is known before anything is decrypted;
  restore accepts only `control/control.db` and `tenants/<digits>/...`, creates files with
  `O_EXCL|O_NOFOLLOW` in a staging folder, verifies the whole archive against its manifest first,
  and renames existing data aside rather than deleting it.
- **The engine.** A default-deny gate in front of all 13 routes, with no public path; exactly one
  module opens network connections, all of them to the services process, with proxies ignored; no
  `subprocess`, `pickle`, `eval`, `yaml` or `ctypes` anywhere; every SQL value is bound; model output
  can produce text and three enumerated values, and never opens a URL, writes a file or changes a
  setting; 177 compiled expressions were timed against hostile input and only the one in finding 1
  was slow.
- **The extension.** None of the 10 `innerHTML` writes takes untrusted data (every page- or
  service-sourced string reaches the DOM through `textContent`); no `eval`, no remote code, no
  `postMessage` bridge to the page, no `externally_connectable`; the worker checks the sender of every
  message and dispatches through a fixed table; every request is built from a compile-time origin with
  `credentials: "omit"` and `redirect: "error"`, so the token cannot follow a redirect; all five shadow
  roots are closed and never exposed; and the "never presses Post" promise holds: every `.click()` in
  the code base is accounted for, and no key event is ever dispatched into X's composer.
- **From outside, against the running site.** No open redirect, no path traversal on `/static`, no
  host-header reflection, no reflected input on error or login pages, every state-changing GET is
  refused, every form POST without a valid token or from another origin is refused, oversized bodies
  get 413, the API description endpoints are off, and every page carries a strict
  Content-Security-Policy, `nosniff`, `frame-ancestors 'none'`, a same-origin opener policy and
  `no-store`.
- **Dependencies.** The 71 Python packages of the environment have no known advisory (apart from the
  environment's own `setuptools`, which the deployed image removes). Six npm **build and test tools**
  do; none of them is in the running service or inside the extension. They are listed, with the
  versions that fix them, in [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md#known-advisories-in-development-tools).

## Known limitations

Each of these was looked at during the review and left as it is. The table says why, and what to
change if you want it changed. The last row has changed since the review and says how.

| What | Why it is left | If you want to change it |
|---|---|---|
| The separate opt-in for learning from the user's visible activity is decided from a field the extension sends (`metadata.capture_mode`); the server enforces the main disclosure for everything, but tells a passive capture from an explicit one by that field | The extension is first-party code, and the engine independently refuses captures of posts that are not the customer's own. Somebody who controls the extension controls far more than this field | Have the engine issue a one-time id with each draft and require it for a publication proof |
| A services token lives for 24 hours and cannot be revoked; it is verified without a database lookup by design | The account behind it is checked instead, on the AI proxy and (since finding 10) on the fetcher, so a suspension bites within 30 seconds | Shorten `ENGINE_TTL_S` in `services/tokens.py` and re-mint from the pool |
| Rate limits are fixed windows, so about twice the limit can pass across a window boundary | For the tightest one (five two-factor attempts per five minutes) that is ten guesses at a six-digit code | A sliding window in `security/ratelimit.py` |
| The grace period after a failed renewal starts at the first failure the service *processed*, not the earliest one Stripe reports | It can only err in the customer's favour, by days; the opposite rule would let a replayed old event cut access instantly | `billing/processor.py`, pinned by a test that says so |
| Enrolling the owner's authenticator for the first time needs only the owner's password, and elevation survives a password change | Both are the conventional behaviour; enrol two owner addresses, as [OPERATIONS.md](OPERATIONS.md) says | – |
| In development the engine listens on a loopback TCP port and writes its token to a file that other users of a shared Windows machine could read | Development only: production engines use per-customer unix sockets and receive the token on standard input, never on disk | Do not develop on a shared machine |
| A finished draft stays in `usage_reservations.result_json` for 15 minutes so that a dropped connection can be replayed, and so it is in a backup taken in that window | It is what makes "the draft arrived although the connection dropped" work; backups are encrypted | Shorten `RESULT_TTL_S` in `usage/service.py` |
| `/readyz` needs no account and answers with four booleans | The host's health check needs it; it says nothing else | Bind it to the internal port on a host that supports that |
| The Content-Security-Policy has no `report-to`, and there is no `Cross-Origin-Resource-Policy` | Nothing is embedded cross-origin and there is nowhere to send reports yet | Add them when there is a reporting endpoint |
| At the time of the review, dead code from the local desktop product remained in the engine (`importer.py`'s archive import, some settings schemas with an `api_key` field, stubbed image search). `importer.py` and the `api_key` settings have since been removed. The stubbed image search in `images.py` is still there | No route reaches any of it (a test walks the live router against the contract), so it is a reading hazard, not a hole; removing it safely is a refactor with its own risk | Delete it behind the route-matrix test |

## What this review did not cover

- A deployed service: TLS configuration, the host's network, the volume, real backups.
- Stripe's, the AI provider's and the email provider's own security.
- The Chrome Web Store review, and X changing its page or its rules.
- Social engineering of the owner, and the owner's own machine.
- Load: nothing here was run at scale, and the single-machine design has a ceiling
  ([README](../README.md#status-and-limitations)).

Do a penetration test against the deployed service before taking payments at any volume.
