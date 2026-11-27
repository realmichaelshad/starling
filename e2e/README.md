# End-to-end journeys

Everything here runs **locally against mocked providers**. The gateway, the services process
and the per-customer engine workers are the real ones, started as real processes; only the
third parties (OpenAI, Stripe and Google sign-in) are local fakes. Nothing in this folder is
evidence that the product works against the live providers, and nothing here should ever be
described as live or production-verified.

```
e2e/
  harness/        the stack: three fake providers, the services process, the gateway
  tests/          the API-level journeys (pytest + httpx)
  browser/        the browser journeys (Playwright, real Chrome, the real unpacked extension)
  .run/           one folder per run: logs, data, the state file, screenshots (git-ignored)
```

## Prerequisites

* Python 3.11 with the repository's virtual environment (`backend/.venv`). The harness always
  runs the engine workers with the same interpreter it runs itself, so use that one.
* Nothing else. No Docker, no network, no accounts, no keys. The harness binds only
  `127.0.0.1` and picks free ports at run time.

## Running

From the repository root:

```
backend\.venv\Scripts\python.exe -m pytest e2e\tests -q
```

That starts a harness of its own, runs every journey and stops everything again, engine
workers included. Each run gets a fresh data folder, so two runs never share accounts,
allowances or rate-limit budgets.

To keep a stack up - for a browser session, or to run the journeys repeatedly without paying
for start-up each time:

```
backend\.venv\Scripts\python.exe -m e2e.harness up --detach --state e2e\.run\state.json
set STARLING_E2E_STATE=e2e\.run\state.json
backend\.venv\Scripts\python.exe -m pytest e2e\tests -q
backend\.venv\Scripts\python.exe -m e2e.harness down --state e2e\.run\state.json
```

`up` without `--detach` stays in the foreground and stops everything on Ctrl-C. `status` prints
the state file and whether the gateway answers. Journeys that restart the services process are
skipped when the session attached to a stack it did not start.

Two options exist for a browser run, which needs a fixed origin (the extension compiles one in)
and the id of the unpacked extension:

```
... -m e2e.harness up --detach --gateway-port 8765 --extension-id <32 letters a-p>
```

Both also read `E2E_GATEWAY_PORT` and `E2E_EXTENSION_ID` from the environment.

## What is mocked, and what is not

| Part | In these runs |
|---|---|
| Gateway | real (`uvicorn starling_server.app:create_app`, one worker) |
| Services process | real (`python -m starling_server.services.main`) |
| Engine workers | real, one process per customer, spawned by the gateway's development spawner |
| Control database | real SQLite, migrated from scratch per run |
| Website | the real templates and pages |
| Email | the development outbox (`EMAIL_MODE=outbox`, read through `GET /_dev/outbox`) |
| OpenAI | **fake** (`harness/fake_openai.py`) |
| Stripe | **fake** (`harness/fake_stripe.py`, wrapping the billing workstream's own in-memory fake) |
| Google sign-in | **fake** (`harness/fake_google.py`) |
| The world service | **off** (`STARLING_WORLD_OFFLINE=true`: no news refresh, no page fetches) |

### The fake AI provider

It answers the Chat Completions subset the services proxy sends, streaming and not, always
with a usage block. Answers are deterministic - the same prompt gives the same text and the
same token counts - so a journey can multiply them by `backend/server/config/ai_pricing.json`
and check what `ai_calls` recorded.

They are also shaped for the caller. The engine asks the model for several different things and
parses each answer differently, so one generic answer would make every draft fail its own
gates. The rules match the instruction the engine really sends:

| The engine asks for | The fake answers with |
|---|---|
| a draft with a `json_schema` response format | one draft object: `verdict` "act", `point` (the draft's first sentence) and the text |
| `Return valid JSON only.` | `{"candidates": [{"text": ...}, ...]}` (the legacy shape: one candidate) |
| a streamed draft (`Output format: first write ONLY the finished draft text`) | the draft, then the sentinel line, then its metadata JSON |
| `Return only the single sentence. No preamble.` | one sentence built from the prompt's own distinctive words |
| `Reply with OK or BAD: <phrase>.` | `OK` |
| `You verify quotes ... or NONE.` | a sentence copied verbatim out of the evidence it was given; for several claims at once, one numbered line per claim |
| a streamed reply that asks for its point first | a `POINT:` line, then the draft |
| an Enhance | the improved text with its point field |
| `POST /v1/responses` with the `web_search` tool | a `web_search_call` item and a message whose `url_citation` annotations cite the sources (outcome `search` in the call log, `totals.searches`) |

Every answer honours `max_completion_tokens` / `max_tokens`: an answer longer than the cap is cut and
ends with `finish_reason: "length"`, as a real model's would.
| the strategist chat prompt (`You are Starling ...`) | three short sentences of advice |
| the world digest prompt | a short cited digest |

Control endpoints (`<fake_openai_control>` in the state file):

| Route | Purpose |
|---|---|
| `GET /requests` | every call, with tokens, model, streaming, and the identity it was billed under |
| `POST /requests/reset`, `POST /reset` | forget the call log (and the queued failures) |
| `POST /fail` | queue one failure: `invalid_key`, `rate_limited`, `server_error`, `quota`, `slow` (`delay_s`), `cut_stream` (`after_chunks`), `empty_usage` |
| `GET /failures` | what is still queued |

### The fake Stripe

The API surface is the billing workstream's own fake, so the two never drift. On top of it this
harness adds what a browser needs: a hosted Checkout page and a Customer Portal page with a
button per outcome, each of which changes the same state Stripe would change and then delivers
the resulting events to `POST /webhooks/stripe`, correctly signed and in the order Stripe
produced them. `POST /_control/delivery` can duplicate them, shuffle them, or hold them back
entirely.

Two details are worth knowing:

* **Three hosted pages, as Stripe has three.** Checkout (`/c/pay/...`), the Customer Portal
  (`/p/session/...`) and the hosted invoice page (`/i/<invoice>`), where a first payment that was
  declined is finished. The first two send the customer back to the site, as Stripe's do. The
  invoice page does **not**, because Stripe's takes no return URL: it shows the invoice as paid and
  the customer finds their own way back. A stand-in that redirected would hide the moment worth
  proving, that the site is right when the customer returns by themselves.
* **The pages are served over TLS**, on a second port, with a throwaway self-signed certificate
  generated per run and never written anywhere permanent. Stripe's hosted URLs are https and
  the gateway refuses a session URL that is not (`billing/service.py`), so the fake has to be
  https too. Clients do not verify the certificate (`httpx.Client(verify=False)`; a browser
  wants `--ignore-certificate-errors`). The API itself stays on plain HTTP, so the gateway
  needs no certificate trust of any kind and production's strictness is untouched.
* **The fake's clock follows the real one.** The gateway checks webhook timestamps and resolves
  entitlement periods against the real clock, so a fake clock a year out would break both.
  `POST /_control/clock` moves an offset when a journey needs a period boundary to fall a few
  seconds from now, which is the only way a renewal or a cancellation at period end can be seen
  happening.

Control endpoints: `health`, `state`, `clock`, `delivery`, `deliver`, `pending`, `deliveries`,
`advance`, `pay_invoice`, `start_subscription`, `dispute`, `refund`, `cancel_now`,
`delete_customer`, `fail_next`, `reset`.

## The journeys

| File | What it proves |
|---|---|
| `test_01_signup.py` | sign-up, the verification email, sign-in, the free plan and its allowance in all three views; the CSRF token and the Origin check |
| `test_02_connect.py` | the extension's PKCE exchange, and every way it is refused; the device list and revoking |
| `test_03_drafting.py` | a real draft from a real engine worker; the stream contract; one credit; `ai_calls` priced from the price file |
| `test_04_idempotency.py` | replays, mismatches, provider failures, disconnects, polling a request id, per-account concurrency, rate limits |
| `test_05_limits.py` | the plan allowance, the fair-use ceiling, the owner's pause, the daily AI budget and the free-account trip |
| `test_06_billing.py` | checkout, duplicate and out-of-order webhooks, renewals, upgrade, scheduled downgrade and undo, cancellation, disputes, refunds, portal flows |
| `test_07_deletion.py` | the data export, then deletion: Stripe cancelled, folder gone, tombstone written, token refused |
| `test_08_isolation.py` | two customers drafting at once: separate processes, separate folders, no crossover |
| `test_09_restart.py` | the gateway is killed in the middle of a draft and started again: the orphaned reservation is settled at start-up, the draft slot is free, nothing was charged for the draft that never arrived, and the counters still add up |

`test_06_billing.py` also walks the two ways out of a declined first card: pressing the same plan
again leads to that invoice's own page and paying there activates the subscription the customer
already started (one subscription, one granted period, no second Checkout Session).

Every group has a **negative control**: a test that makes the same assertions fail by changing
something in the harness only - a queued provider failure, webhook delivery switched off, an
allowance moved by the owner's own action. None of them edits product code.

Each journey reads the account's state in three places and requires them to agree: the rendered
website pages, `GET /api/v1/session`, and the control database (read only).

### Accounts and rate limits

Every account is a real sign-up. The harness lists a trusted proxy
(`STARLING_TRUSTED_PROXIES=127.0.0.1`) and each simulated customer sends its own
`X-Forwarded-For` out of the IPv6 documentation range, so the per-network sign-up limit and the
per-account rate limits separate the journeys instead of adding up across them - exactly as
real customers, on real networks, are separated. Accounts are session scoped and asked for by
name (`accounts("drafting")`).

## The browser journeys

The same stack, driven through a real Chrome with the **real unpacked extension** loaded, on a local
copy of X's page structure (`browser/lib/x-fixture.mjs`; never x.com). They exist because some
failures only a browser can show: a Content-Security-Policy that blocks a redirect, a button that is
there in the HTML and unusable on the screen, a panel that squeezes its own content to a sliver.

Prerequisites: Node.js 20 or newer, and once, from `e2e/browser`: `npm ci` and
`npx playwright install chromium`. The extension's own dependencies must be installed
(`extension/`, `pnpm install`), because the runner builds it.

From the repository root:

```
node e2e/browser/run.mjs                 # build the e2e extension, start the stack, run everything, stop everything
node e2e/browser/run.mjs --journey 2     # one file
node e2e/browser/run.mjs --skip-build    # reuse the last extension build
node e2e/browser/run.mjs --headed        # watch it
```

The runner builds the extension in e2e mode, reads the unpacked extension's id from Chrome, starts
the stack on the fixed port the extension was compiled for (`8765`), runs the journeys serially
with a fresh browser profile per file, stops everything, and then **proves nothing was left
behind**: the port is free and every process the harness started is gone. Screenshots of every step,
a trace for a failure and an HTML report land in `e2e/.run/browser/<run>/`.

Every journey's browser is fenced in: a local forward proxy and a route guard refuse anything that
is not the stack itself, and a journey fails if the page or the extension tries to reach another
host. Chrome's own background traffic to Google is refused too, and listed rather than failed.

| File | What it proves |
|---|---|
| `01-new-customer.spec.mjs` | a stranger arrives, makes the mistakes people make (an empty form, an address that is not one, a password that is too short, the wrong password at sign-in) and is told what to fix in words; confirms their email from the outbox; opens every account page; changes their password; forgets it and resets it, and the reset link works only once; downloads their data (no hash or token in it); deletes the account, which refuses the wrong confirmation, signs them out and can never sign in again |
| `02-paying-customer.spec.mjs` | walking away from checkout charges nothing; a declined card is said plainly (never "confirming your subscription", never "you keep paid access") and is **not a dead end**: the customer finishes the payment on the invoice page, or chooses another plan and gets a checkout of its own; then pay, cancel, change their mind, resume, upgrade |
| `03-extension-customer.spec.mjs` | first install, consent, the real `chrome.identity` sign-in, a reply drafted into X's own reply box, the allowance agreeing in the extension, the API and the website after every step, running out, "See plans" opening pricing, paying, carrying on in the same tab, Enhance in the composer, disconnecting from the website. Starling never presses Post or Reply |
| `04-google-customer.spec.mjs` | "Sign up with Google" through the stand-in account chooser makes a confirmed account with no password and no confirmation email; the extension's sign-in window connects through the same Google sign-in, with nothing typed |
| `05-web-checked-draft.spec.mjs` | a reply to a post that claims a release "today" gets one web search (the stand-in's `/v1/responses`); the pages whose facts reached the draft are listed under it in the panel as links that name their site and open in a new tab with `noopener noreferrer`. The links are checked, not followed: the fence would fail the journey. A second test opens the signed-in account's own profile and presses "Add my posts from this page": its three posts become writing samples, a repost of someone else's stays out, and pressing again adds nothing twice |

## The AI evaluation

The journeys prove the parts agree; they cannot say whether a draft is any good, because the
stand-in's text is deterministic filler. `e2e/eval` is how to look at real output: it signs up
invented customers with plainly different voices (a terse ML engineer, a considered SaaS founder,
an energetic strength coach, and a brand-new account with no writing), gives each their own writing
samples, and presses ✦, the composer and chat on fixed posts through the whole product, exactly as
the extension does. It writes one report with every post, draft, decision, source and note, what a
good result would look like, and each press's time, AI calls, tokens and cost.

```
backend\.venv\Scripts\python.exe -m e2e.eval                                # the stand-in: free, checks the tool
backend\.venv\Scripts\python.exe -m e2e.eval --real --spend-up-to 3         # the real provider, with your key
```

- **The key** comes from the environment variable `STARLING_EVAL_AI_API_KEY`, or from a file
  `.env.eval` at the repository root holding one line `AI_API_KEY=...`. Git ignores `.env.*`; never
  commit a key. Only its last four characters are ever printed.
- **The spend** is capped by the product itself: the run's daily and monthly AI budgets are set to
  `--spend-up-to` (between $0.25 and $25), so every AI call after that is refused (one already running
  may finish). Give the key a hard limit in the provider's dashboard as well. A full run is about 30
  presses plus background work.
- **The real run** also turns the world service online, so it reads real news feeds and linked
  pages, and waits 45 seconds before each customer's Create post and chat so background topic
  research can finish (`--topic-wait`).
- **The report** is `e2e/.run/eval/<run>/report.md`, with `results.json` beside it for comparing two
  runs. `--personas terse,coach` runs a subset. Each press also gets pass/fail checks on the tools it
  should have used: a post that makes a claim must come back with web sources, a post with a link must
  cite its page, a created post must cite its story.
- **The world** is real on `--real`. On the stand-in, the linked pages and news stories come from
  `e2e/eval/world_fixtures.json` (`STARLING_WORLD_FIXTURES`, which production refuses), so reading a
  post's link and the news pool really run.
- **`--plans real`** uses the product's own `plans.toml` instead of the eval's relaxed plans, so the real
  Free caps (web checks, rates, the daily pause) show in the report.
- **`--golden`** makes the stand-in answer with the golden answers (`e2e/eval/golden.py`, turned into
  rules by `golden_script.py`), to check that the engine ships a great answer intact: the report then
  shows, per press, whether it came through unchanged, changed, blocked or held back. Not with `--real`.
- **Its own tests** (`python -m pytest e2e/eval`) check the guards: no real run without a cap and a
  key, and nothing starts before both are there.

### The stand-in's script hook

`E2E_AI_SCRIPT` names a JSON list of rules `{"match": [substrings, all must appear], "endpoint"?: "chat" |
"responses", "answer": str | object}`, consulted first for every call and re-read when the file changes.
A rule matches the call's messages in order, each written as `<role>` on its own line followed by its
content; for `/v1/responses` the instructions are the system message and the input the user message. A
`/v1/responses` answer may be `{"text", "sources": [{"url", "title"}]}`. `GET /_control/requests` returns
`{"totals", "calls"}`, each call with its full `messages`, `response_text`, `endpoint` and whether a rule
`scripted` it; cached tokens are reported per `prompt_cache_key` for a repeated prefix of at least 1,024
tokens. `E2E_WORLD_FIXTURES` gives any e2e run a fixtures file for the world service.

## Reading the logs

Each run writes `e2e/.run/<run-id>/logs/`:

| File | What is in it |
|---|---|
| `gateway.err.txt` | the gateway's own log at INFO: sign-ups, engine starts and stops, billing events, refusals |
| `gateway.out.txt` | usually empty (access logs are off by design: query strings carry tokens) |
| `services.err.txt` | the services process's JSON lines: budgets, upstream failures, key problems |
| `services.out.txt` | its one `READY` line |
| `fake-openai.*`, `fake-stripe.*` | the fakes' `READY` lines and any error they printed |
| `stripe-setup.txt` | what `stripe-setup` created in the fake, and the settings it produced |

The customer data of the run lives in `e2e/.run/<run-id>/data/` (the control database under
`control/`, one folder per customer under `tenants/`). The whole `.run` folder is git-ignored
and can be removed whenever no harness is up.

Start with `gateway.err.txt`. A failed draft almost always shows there first: `engine N failed
to start`, `assist stream for reservation ... hit the time limit`, or a billing event that
could not be processed.

## Known findings

Everything below was found by these journeys, not by a unit test, and is fixed. Each has a test that
fails without the fix.

* **"Choose Starter" did nothing in a real browser.** The billing form posts to the site and is
  answered with a redirect to the payment page, and a browser applies the page's `form-action`
  policy to that redirect. Fixed in `security/middleware.py`; the policy now names all three of
  Stripe's hosted origins (`tests/test_payment_page_policy.py`).
* **A declined first card was a dead end, twice over.** The return page said "Confirming your
  subscription" and polled for two minutes; the Billing page told a customer who had never had a
  paid plan that they "keep paid access" and offered the Customer Portal, which cannot pay an open
  invoice. Now both pages say the payment is not finished and lead to the invoice's own page, and
  choosing a different plan cancels the unfinished one (`02-paying-customer.spec.mjs`,
  `tests/billing/test_hardening_processor.py`).
* **The extension's panel squeezed the conversation to a sliver** while an account notice was showing,
  and showed the same two buttons twice. Fixed in `extension/entrypoints/content.ts`.
* **A gateway killed mid-draft left a reservation holding the customer's only draft slot** until the
  sweeper's timeout. Start-up now settles orphans (`test_09_restart.py`).
* **Every reply to a question failed validation** once the engine's writing plan passed 1,200
  characters. Found by drafting through the real engine; fixed with `direction.value_copies`.

* **Fixed: a chat press was charged even when every AI call failed.** These journeys found that, with
  the provider refusing everything, a chat press answered "I couldn't produce a version of that ..."
  with `ok: true`, and the gateway charged a credit for it. Now the engine reports the outage as an
  error, marks a genuinely gated answer `withheld`, and the gateway charges a usable result only when
  an AI call for it succeeded. `test_04_idempotency.py::test_a_chat_press_is_not_charged_when_every_ai_call_fails`
  proves it end to end.
