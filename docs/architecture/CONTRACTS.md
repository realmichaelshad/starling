# Starling hosted architecture: contracts

This file is the single source of truth for interfaces between components. Every
workstream builds against it. Change this file first, then the code.

Conventions used everywhere:

- Times are unix seconds (UTC) as integers. Money is integer micro-dollars (`1 USD = 1_000_000`).
- Identifiers: `account_id` is a UUID4 string; `tenant_seq` is a positive integer assigned once per
  account and used for OS user ids and data folder names.
- JSON is UTF-8. Every error body is `{"error": {"code": "...", "message": "..."}}` plus optional
  fields listed per route.
- Python 3.11. Server dependencies are limited to: fastapi, starlette (imported directly), uvicorn[standard],
  pydantic, httpx, jinja2, python-multipart, argon2-cffi, cryptography. The engine additionally uses numpy,
  scikit-learn, pillow and tzdata (no model runtime: onnxruntime was removed). Anything else needs a
  note in this file.

---

## 1. Repository layout

```
backend/
  engine/                      # the Starling intelligence engine (Python package `app`)
    app/                       # runs as one process per customer ("engine worker")
    tests/
    models/starling-embedding.npy   # seeded 256x96 random projection (no trained weights)
    tools/                     # build_embedding_projection.py (regenerates/checks the .npy), measure_worker.py
  server/
    starling_server/
      app.py                   # FastAPI app factory for the public gateway
      config.py                # typed settings from environment; startup validation
      db/                      # control database: migrations, writer thread, request_errors
      auth/                    # accounts, passwords, web sessions, CSRF, email tokens, TOTP
      account/                 # account pages, data export, deletion (and its resume hook)
      devices/                 # extension connect (PKCE) and device tokens
      usage/                   # reservations, ledger, limits, rate limits, account_state()
      billing/                 # stripe_client (httpx), webhooks, reconciler, entitlements, setup
      engines/                 # engine pool, spawner client, stream passthrough, capture spool
      services/                # AI proxy + world service incl. SSRF-safe fetch (its own process)
      api/                     # /api/v1 routes used by the extension
      web/                     # website routes (render templates from /web)
      admin/                   # owner dashboard routes
      email/                   # SMTP sender and development outbox
      system/                  # health, readiness, robots, sitemap, security.txt
      ops/                     # encrypted backups, verification, restore, retention
      security/                # headers, error shaping, rate limiting, crypto helpers
      supervisor.py            # container entrypoint: process tree and per-customer OS users
      cli.py                   # `python -m starling_server.cli ...` operator commands
    config/
      plans.toml               # plans, allowances, prices (Stripe lookup keys), ceilings, limits
      ai_pricing.json          # per-model token prices with effective dates and sources
    tests/
  requirements.in              # direct dependencies for the container image
  requirements.lock            # hashed Linux lock generated in the image build
  requirements-dev.txt
extension/                     # Chrome MV3 extension (WXT)
web/
  templates/                   # Jinja2 templates (marketing, docs, legal, auth, account, admin)
  static/                      # css/, js/, img/, fonts/ (no third-party requests)
deploy/                        # Dockerfile, docker-compose.yml, fly.toml
e2e/                           # harness (fake OpenAI and Stripe, process launcher), API and browser journeys
docs/                          # operator and architecture documentation
scripts/                       # check_release.py and developer helpers
brand/                         # logos, icons, wordmark
.env.example                   # every setting, no secrets
```

---

## 2. Processes, users and sockets

### 2.1 Production container (Linux)

| Process | OS user | Listens on | Secrets it may hold |
|---|---|---|---|
| supervisor | root | `/run/starling/spawner.sock` (mode 0660, group `starling-gw`) | `STARLING_SERVICES_TOKEN_KEY` (verifies the services token in spawn requests), `BACKUP_PASSPHRASE` (nightly backup thread) |
| gateway | `starling-gw` (uid 10001) | TCP `0.0.0.0:8080` (public, behind TLS proxy) | `STARLING_SECRET_KEY`, `STRIPE_*`, `SMTP_*`, services admin token |
| services | `starling-svc` (uid 10002) | `/run/starling/services.sock` (0660, group `starling-eng`) | `AI_API_KEY`, `AI_API_KEY_SECONDARY` |
| engine (one per active customer) | uid/gid `200000 + tenant_seq`, supplementary group `starling-eng` (gid 10010) | `/run/starling/engines/<tenant_seq>/engine.sock` | per-engine tokens received on stdin |

- Gateway supplementary groups: `starling-eng` (to reach services admin endpoints) and `starling-db`.
- Services supplementary groups: `starling-db`.
- Control database: `/data/control/control.db` (+ `-wal`, `-shm`), owner `starling-gw`, group
  `starling-db` (gid 10020), mode 0660; directory mode 2770.
- Customer data: `/data/tenants/<tenant_seq>/`, owned by the engine uid, mode 0700.
- Engine socket directory `/run/starling/engines/<tenant_seq>/` is created by the supervisor with
  owner = engine uid, group = `starling-gw`, mode 2750 (setgid), so the socket inherits the gateway
  group and no other engine can open it. Engines run with umask 007.
- Engines never receive `AI_API_KEY`, Stripe keys, SMTP credentials or `STARLING_SECRET_KEY`.
- The gateway runs as exactly ONE uvicorn worker process: per-account concurrency slots and the engine pool
  live in that process's memory. Concurrency comes from asyncio; blocking database work runs in threads.
- The gateway runs with uvicorn access logs off (`--no-access-log`): query strings carry email
  verification and password reset tokens.
- `/data/tenants` itself is owned by root with mode 0711; only the per-customer folders are readable by
  their engine.
- Nightly encrypted backups run in a thread of the supervisor (`ops.scheduler.start_backup_thread`); each
  run records `service_state['backup:last']`.
- `STARLING_MAINTENANCE=1`: the supervisor starts no gateway, services or engines and stays alive, so the
  machine and its volume stay up for a restore.

### 2.2 Development mode

`STARLING_ISOLATION=none` (allowed only when `STARLING_ENV` is `development` or `e2e`):
all processes run as the current user; engines and services listen on TCP `127.0.0.1` with an
ephemeral port reported in the READY line; data lives under `STARLING_DATA_DIR`. The gateway refuses
to start with `STARLING_ISOLATION=none` when `STARLING_ENV=production`.

---

## 3. Engine worker contract

### 3.1 Launch

- Command: `python -m app.worker` with working directory `backend/engine`.
- Environment (non-secret):

| Variable | Value |
|---|---|
| `STARLING_HOSTED` | `1` |
| `STARLING_APP_DIR` | `/data/tenants/<tenant_seq>` |
| `STARLING_LISTEN` | `unix:/run/starling/engines/<seq>/engine.sock` or `tcp:127.0.0.1:0` |
| `STARLING_SERVICES_URL` | `unix:/run/starling/services.sock` or `http://127.0.0.1:<port>` |
| `STARLING_MODEL_QUALITY` / `_FAST` / `_VISION` | `starling-quality` / `starling-fast` / `starling-vision` |
| `STARLING_RESEARCH` | `proxy` |
| `STARLING_DISABLE_WARM` | `1` |
| `TZ` | `UTC` |
| `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` | `1` |
| `PYTHONNOUSERSITE` | `1` (an engine's home is the customer-writable data folder) |

The environment is built explicitly: nothing else is inherited (no `PYTHONPATH`, no proxy variables).

- Stdin: exactly one JSON line, then EOF:
  `{"worker_token": "<base64url, 43 chars>", "services_token": "<signed services token, see 4.3>", "tenant_id": "<uuid>"}`
- Stdout: within 30 seconds, one line `READY {"listen": "<unix:...|tcp:127.0.0.1:PORT>", "pid": <int>, "engine_version": "<semver>"}`.
  After READY, stdout carries JSON log lines only (no prompts, drafts or tokens).
- Shutdown: SIGTERM (or CTRL_BREAK on Windows dev) stops accepting requests, aborts in-flight work
  within 10 seconds, checkpoints SQLite WAL, exits 0.

### 3.2 Requests from the gateway

- Every request carries `Authorization: Bearer <worker_token>`; anything else gets the existing
  default-deny 404.
- Forwarded headers: `X-Starling-Request-Id` (the usage reservation id, a UUID; present on metered work;
  the services process accepts `[A-Za-z0-9_-]{8,128}`),
  `X-Starling-Time-Zone` (IANA name from the browser; used instead of the machine time zone).
- The engine attaches `X-Starling-Request-Id` to every services call made while serving that request,
  including calls from threads (copy the context explicitly).

### 3.3 Routes the engine exposes (everything else returns 404)

| Method and path | Purpose |
|---|---|
| `GET /livez` | `{"ok": true}`; no model or network calls |
| `POST /browser/assist` | modes `context` (✦ decide) and `schedule` |
| `POST /browser/assist/stream` | modes `chat` and `enhance_composer`; NDJSON (3.4) |
| `POST /snapshots/profile`, `POST /snapshots/metrics` | passive capture |
| `POST /outcomes/record`, `POST /images/feedback`, `POST /preferences/confirm` | feedback |
| `POST /own-material/list`, `POST /own-material/add`, `POST /own-material/remove` | writing samples |
| `GET /profile/export` | customer data export |
| `POST /internal/requests/{request_id}/cancel` | stop the in-flight pipeline for that request; 200 `{"ok": true, "cancelled": <bool>}` (`false` when nothing was running) |

Removed from the engine: model provider settings, local model routes, bootstrap, update, browser
engine, `/import` (optional extra later via a file path), `DELETE /data` (the gateway deletes the
customer folder), `/health` (replaced by `/livez`), and every route not listed above.

### 3.4 Engine stream events

One JSON object per line:

- `{"event": "status", "stage": "<text>"}`
- `{"event": "token", "text": "<text>"}`
- `{"event": "final", "response": <AssistResponse>}`, exactly once when the pipeline finishes
- `{"event": "error", "error": {"code": "<code>", "message": "<text>"}}`, terminal

`AssistResponse` keeps the shape in `extension/shared/types.ts`: `ok`, `decision`, `answer`,
`error`, `cancelled`, `generation_id`, `candidate_id`, `model`, `visual`.

Two optional draft fields the extension shows under the draft:

- `decision.sources`: at most 3 `{"url", "title"}` items, http or https only: the pages whose facts
  reached the writer, web search results first, then the post's own fetched links, then news-pool
  stories or a created post's story. The writer receives facts only from pages listed here, so no
  fact reaches a person without its link. The extension shows them as links that open in a new tab
  (OpenAI requires visible, clickable citations for anything drawn from web search results). A chat
  answer carries its pages as `research.sources`, with the same item shape.
- `decision.voice_note`: a sentence, set while the account has fewer than 10 texts of its own, saying
  drafts sound generic until Starling knows how the customer writes. Shown with a button to the
  writing samples in Settings.
- `decision.search_note`: a sentence, set when a web search was due for the post's claim and did not
  help: it came back after the 7-second budget, the search allowance is used up, or it found nothing
  that confirms the claim. Shown in the card's note line in place of the generic "No web check"
  caution.

Other fields the engine sends (all optional; the extension shows what it knows and ignores the rest):

- `/assist` (✦), `decision.*`: `source_line` (the pool story or page the read leaned on), `downside` (the
  risk line), `stats_line` (the post's live momentum, or its freshness), `plan_hint` (what outweighed the
  other options), `timing_line` (when to look at the post again), `score_band`, `web_checked` (the writer
  was given web facts), `model_decided` (true when the model made the act/wait call), `needs_review` with
  `rationale` replaced (a labelled repeat of an earlier draft).
- Request field `composer.topic` (optional, at most 500 characters; the website's writing page, section
  10): on a created post (`action` `post`, `intent` `create`) the subject the writer named. The post is
  written about it instead of a story from the account's beat: no topic search, no story list. Ignored
  on replies, quotes and Improve. The extension does not send it.
- `/assist/stream` composer (Create reply, quote or post; Improve), `decision.*`: `topic` (Create post:
  the title of the story the model chose; its page is first in `sources`), `stats_line` ("Based on: ..."),
  `plan_hint`, `timing_line` (the thread's life for a reply; the scheduler's headline, or the quiet-hour
  note, for a post), `thread_temperature` (`hot`, `cooling`, `dead`; replies), `schedule` (Create post: the
  one timing call and the Schedule button's target), `quiet_hour` (true: drafted at an hour this account
  rarely posts) with `resume_local` ("HH:MM", when its usual window opens), `wait_recommended` (true: a post
  written from the writer's beat, not from news; `search_note` says why), `web_checked` (absent on
  Improve), `needs_review` with `review_note` (a named weakness, such as a repeat), `score_band` and
  `growth_score`, `thread_parts` (when a thread was asked for: part 1 is `draft`), `image_decision`, and
  top-level `format_hint` (`{"suggested", "rationale"}`, when another format fits the post better).
- `decision.search_note` on a created post with no story says why: "I found nothing new in your beat
  today, ..." only when a search really ran; otherwise which cap refused it and when it comes back, that
  the search did not come back in time, or that there was no time left to look.
- Chat, top level: `answer`, `conversation_id`, `research` (`{"status", "sources"}`, at most 4 sources:
  the pages behind the answer), `radar` (`{"emerging", "trends" (at most 6), "summary", "limitation"}`),
  `preference_proposal`, `topic_memory`, and `decision` when chat drafted a post (as for the composer).

A result is **usable** when `ok` is true, neither `withheld` nor `unchanged` is true, and either
`decision.draft` or `answer` is a non-empty string. `unchanged: true` marks an Enhance that found nothing
to improve: `decision.draft` carries the customer's own words back so the composer keeps them, and
nothing in it is the service's work, so it uses no credit, even when a preview had already streamed
(section 7). `withheld: true` marks a chat answer that only explains why no draft passed the
engine's gates: it is shown to the customer but is not a product. When a refused AI call is what left
the result empty or withheld, the engine reports that failure as the error instead.

---

## 4. Engine → services

Base URL `STARLING_SERVICES_URL`. Headers on every call:
`Authorization: Bearer <services_token>` (identifies the customer) and either
`X-Starling-Request-Id: <reservation id>` or `X-Starling-Bucket: background`.
`GET /livez` needs no token. The world routes (4.2) and `GET /v1/models` also accept the gateway's admin
token.

### 4.1 `POST /v1/chat/completions` (OpenAI Chat Completions subset)

Accepted request fields:

- `model`: `starling-quality`, `starling-fast` or `starling-vision`
- `messages`: roles `system`, `user`, `assistant`; content is a string or an array of parts of type
  `text` or `image_url` (data URIs only: `data:image/(png|jpeg|webp|gif);base64,...`)
- `max_tokens` or `max_completion_tokens` (clamped to `AI_MAX_OUTPUT_TOKENS`, default 600)
- `stream` (bool); `response_format` (`{"type": "json_object"}` or `json_schema`)
- `temperature`, `top_p` (forwarded only if the model capability table allows them, today only for
  `gpt-4.1*` and `gpt-4o*`; otherwise dropped)
- Engines never send `stream_options`; the proxy adds `include_usage` upstream.

Upstream request built by the proxy (OpenAI Chat Completions):
`model` from `AI_MODEL_QUALITY` / `AI_MODEL_FAST` / `AI_MODEL_VISION` (default `gpt-5.6-luna`),
`reasoning_effort` from `AI_REASONING_EFFORT` (default `none`), `max_completion_tokens`,
`stream_options.include_usage=true` when streaming, `safety_identifier = sha256(tenant_id)[:32]`,
`prompt_cache_key = sha256(tenant_id + ":" + lane)[:32]`, `n=1`, no tools, no logprobs.
When a reasoning effort other than `none` is sent, `max_completion_tokens` is the visible cap plus a
reasoning allowance (minimal 512, low 2,048, medium 4,096, high 8,192, xhigh 16,384, any other effort
2,048): reasoning tokens share that cap, and a model that spent all of it thinking would return an
empty or cut answer. It is a ceiling, not a spend; only tokens used are billed and metered.
If upstream rejects a sampling parameter with 400 `unsupported_parameter`, retry once without it and
record the capability (only optional parameters are ever dropped). Models without a price in
`ai_pricing.json` are refused with 503 `ai_unavailable`, because their spend cannot be metered
(`ai-check` still tests them and warns).

Response: OpenAI-shaped JSON, or SSE passed through (`data: {...}` lines ending with `data: [DONE]`),
including the final usage chunk.

Errors from the proxy:

| HTTP | code | Meaning |
|---|---|---|
| 400 | `bad_request` | malformed body, remote image URL, disallowed field |
| 401 | `unauthorized` | unknown or expired services token, or the customer's account is not active |
| 402 | `fair_use_exceeded` | this customer's AI cost ceiling for the period is reached, or background calls exceeded their share |
| 409 | `reservation_inactive` | the request id is not in state `active` |
| 413 | `input_too_large` | estimated input tokens above `AI_MAX_INPUT_TOKENS_PER_CALL` (default 8000) |
| 429 | `call_cap_exceeded` | more than `AI_MAX_CALLS_PER_REQUEST` (default 12) for this request |
| 502 | `ai_auth` | upstream 401/403 with both keys |
| 502 | `ai_model` | upstream 404 for the model |
| 502 | `ai_quota` | upstream insufficient quota or spend limit; owner action needed |
| 503 | `ai_rate_limited` | upstream rate limit after retries; `Retry-After` header |
| 503 | `ai_unavailable` | upstream 5xx, overload or timeout after retries |
| 503 | `budget_exhausted` | global daily or monthly AI budget reached; `Retry-After` header |

Checks before each attempt, in one writer transaction and in this order: account active (401), owner
pause (503), reservation active and inside its hard wall (409), call cap (429), fair use and background
share (402), budgets (503). `Retry-After` is the time to UTC midnight, to the first of next month, or
the pause's `retry_after_s` (default 300).

Retries: at most 2 retries with exponential backoff and jitter on upstream 429 `rate_limit_exceeded`,
500, 502, 503, 504 and timeouts, honouring `Retry-After`, never exceeding the remaining request time.
Never retry quota or spend-limit errors. On upstream 401/403 with the primary key, retry once with
`AI_API_KEY_SECONDARY` if configured and raise an owner alert; the secondary key is then tried first
for 10 minutes.

Metering: each upstream attempt's `ai_calls` row is written when the attempt is admitted (status
`cancelled`, `finished_at` NULL) and completed afterwards, so parallel calls can never exceed the call
cap; retries count as attempts. The row records input, cached input and output tokens, the `estimated`
flag, the cost in micro-dollars, the status and the provider's own error code (for example
`rate_limit_exceeded`, `timeout`, `stream_interrupted`), and the reservation's `cost_micros`, `ai_calls`
and `heartbeat_at` are updated. Missing usage (timeout, aborted stream) is estimated as `ceil(chars / 4)`
input tokens plus 1,000 per image and flagged; upstream HTTP error responses are recorded with zero
tokens. Summary and admin-check calls count toward the global budgets only, never toward an account.

### 4.2 World service (shared, customer-independent)

| Method and path | Purpose |
|---|---|
| `GET /v1/news/headlines` | shared news pool from authored sources: Hacker News and OpenRouter read every 2.5 minutes, the RSS and Atom feeds every 5 minutes, with conditional requests; each item says how many independent sources carry the story, when the pool first saw it, how fast its points rise and whether it is breaking; item shape in `docs/architecture/news-pool-shape.md` (`services/world/news.py`) |
| `POST /v1/fetch` `{"url"}` | `{"final_url", "title", "text", "published_at"}` (no preview image); public http(s) only, connects to the validated IP, at most 500 KB, 8 s and 3 redirects (each re-validated) |
| `POST /v1/image` `{"url"}` | `{"data_url", "mime"}` for vision; http or https; image types only, at most 5 MB |
| `POST /v1/summary` `{"query_id"}` | `{"answer", "summary" (same text), "sources" (at most 6), "generated_at"}` for one of the authored query ids in `news-pool-shape.md`; never free text; cached 60 minutes, a stale copy is served if regeneration fails |
| `POST /v1/search` `{"query", "timeout_s"?, "purpose"?}` | one metered web search: OpenAI Responses `POST {AI_BASE_URL}/responses` with the `web_search` tool, through the proxy's upstream client, key fallback and metering (lane `starling-search`, the tool's per-call fee from the `tools` rows of `ai_pricing.json`). Tenant tokens only. `purpose` is `"claim"` (default: check one claim of the public post a press answers; needs `X-Starling-Request-Id`) or `"topics"` (what is new in the account's beat, the query built only from its beat labels, never a post or a note: for a press with `X-Starling-Request-Id`, or as background topic research with `X-Starling-Bucket: background` and no request id; a background search must be `topics`). `query` 8 to 300 characters; `timeout_s` 1 to 60, capped at `WEB_SEARCH_TIMEOUT_S`. Answers `{"query", "answer", "sources": [{"title", "url", "snippet"}] (at most 4), "searches", "queries"}`, plus `"cached": true` when a topics answer comes from the shared 60-minute cache (same query from any account; not metered, `searches` 0). Caps: one search per press; `WEB_SEARCH_PER_ACCOUNT_DAY` press searches per account per UTC day; background searches only on paid plans, at most `WEB_SEARCH_BACKGROUND_PER_ACCOUNT_DAY` (default 8) per account per UTC day, one at a time, within 60% of the account's background allowance (10% of the ceiling) and paced over the period (at most one every period-seconds-left / searches-left); every search only while the period's spend plus its worst-case cost stays under 95% of the plan's cost ceiling and searches (press and background) have used under 25% of it. Errors: 400 `bad_request`, 401, 429 `search_cap_exceeded` with `{"error": {"code", "message", "reason": "per_press" \| "daily" \| "plan_share" \| "background", "retry_after_s": int \| null}}` and `Retry-After` when known (the next UTC day, the period's end or the next paced slot), 504 `search_timeout` (`"reason": "timeout"`: the search's own deadline passed), 429 `call_cap_exceeded`, 402 and 503 as for the proxy, 503 `search_unavailable` (switched off, no price row, or `AI_PROVIDER=anthropic`) |
| `POST /v1/x/post` `{"url"}` | the website's writing page (section 10): one public post on X read through X's published oEmbed endpoint, `https://publish.twitter.com/oembed?url=<post>&omit_script=1&dnt=true`, through the same safe fetcher (5 s, 64 KB, 2 redirects). Admin tokens only (the gateway). `url` must link to one post on x.com or twitter.com; the request is rebuilt from the post's id and handle, so nothing else in the pasted link is sent. Answers `{"url", "status_id", "author_name", "author_handle", "text", "lang", "links" (its t.co links, at most 4), "has_media", "cached"}`; 404 `post_unavailable` when X will not show the post outside X (cached 2 minutes), answers cached 10 minutes per post, at most 120 lookups a minute for the whole service. Never loads x.com pages and never calls X's internal endpoints (`services/world/x_post.py`) |
| `GET /v1/models` | static list of the three lanes |

World errors: 403 `destination_blocked` (non-public address, a port other than 80 or 443, credentials in
the URL, numeric host names, `localhost`, `.local`, `.internal`), 413 `payload_too_large`, 415
`unsupported_content` (also XML that declares entities), 429 `rate_limited` (60 fetches per minute per
caller), 502 `fetch_failed`, 504 `fetch_timeout`. Proxy environment variables are ignored. With
`STARLING_WORLD_OFFLINE=true` (development and e2e only; refused in production) the news pool never
refreshes and fetch, image and summary fail fast without any outbound connection. With it,
`STARLING_WORLD_FIXTURES` (e2e only; also refused in production) names a file of public pages and news
stories that the offline fetcher and the pool serve instead, so journeys can exercise linked-page reading
and the news pool (`services/world/fixtures.py`).

Admin endpoints (gateway admin token only): `POST /admin/ai-check`, `GET /admin/status`.

### 4.3 Services tokens

The services process authenticates callers without a database lookup. Tokens are signed with
`STARLING_SERVICES_TOKEN_KEY`, held only by the gateway, the services process and the supervisor (which
verifies the token in spawn requests); never by engines.

- Format: `svc1.<subject>.<expires_at>.<nonce>.<signature>`
  - `subject`: `t-<tenant_id>` for an engine, or `admin` for the gateway's admin calls
  - `expires_at`: unix seconds (engine tokens: spawn time + 24 hours; admin tokens: now + 5 minutes)
  - `nonce`: 16 random bytes, base64url without padding
  - `signature`: base64url (no padding) of HMAC-SHA256(key, `"svc1.<subject>.<expires_at>.<nonce>"`)
- Verification: constant-time signature comparison, reject expired tokens, reject unknown subjects.
  A token for tenant A can never bill tenant B, because the tenant id comes from the signed subject.
- The engine pool mints a fresh engine token at every spawn and passes it on stdin (3.1).
- Implementation lives in `services/tokens.py` (owned by the services workstream) and is imported by
  the engine pool.

### 4.4 Services process

`python -m starling_server.services.main` listens on `STARLING_SERVICES_LISTEN` (`unix:/path`,
`tcp:127.0.0.1:PORT` or `http://127.0.0.1:PORT`; non-loopback TCP is refused), binds a unix socket itself
with mode 0660, then prints `READY {"listen": "<address>", "pid": <int>}`. Logs are JSON lines on stderr
without tracebacks or fetched URLs. At startup it clears budget stops that are below the limits
configured now (section 7), so raising a budget and restarting takes effect immediately.

---

## 5. Gateway ↔ supervisor (spawner)

Unix socket `/run/starling/spawner.sock` (development mode: an in-process spawner with the same
interface). Newline-delimited JSON requests and responses.

| Request | Response |
|---|---|
| `{"op": "spawn", "tenant_seq": 123, "tenant_id": "<uuid>", "secrets": {"worker_token": "...", "services_token": "..."}}` | `{"ok": true, "pid": 456, "listen": "unix:..."}` after READY, or `{"ok": false, "error": "..."}` |
| `{"op": "stop", "tenant_seq": 123, "grace_s": 10}` | `{"ok": true}` |
| `{"op": "delete_data", "tenant_seq": 123}` | stops the engine, renames `/data/tenants/<seq>` to `.deleting-<seq>-<hex>` (skipped by backups) and deletes it; leftovers of an interrupted deletion are removed at startup; `{"ok": true}` |
| `{"op": "status"}` | `{"ok": true, "engines": [{"tenant_seq", "pid", "rss_mb", "started_at", "over_soft_limit"}]}`; the gateway restarts an engine over its soft memory limit once it is idle |

Validation: `tenant_seq` integer 1..10,000,000; `tenant_id` UUID; `worker_token` base64url of 43
characters; `services_token` a valid signed services token for `t-<tenant_id>` (4.3).

---

## 6. Extension ↔ gateway API (`/api/v1`)

Base: `${STARLING_PUBLIC_URL}/api/v1`. Authentication: `Authorization: Bearer stl_dev_...` (device
token) unless marked otherwise. Cookies are ignored on `/api`. No CORS headers are sent (the extension
uses host permissions).

Request headers from the extension: `Idempotency-Key` (UUID v4) on every POST and DELETE,
`X-Starling-Extension: <version>`, `X-Starling-Time-Zone: <IANA>`.
Response header on every response: `X-Request-Id`.

Error body: `{"error": {"code", "message", "retry_after_s"?, "usage"?, "upgrade_path"?}}`.
`upgrade_path` is always a same-origin path. Unknown `/api/` URLs return 404 `not_found`, a wrong method
returns HTTP 405 with code `bad_request`, and a body that fails validation returns 400 `bad_request`. A
missing or unreadable `X-Starling-Extension` header counts as outdated (426).

Every refusal on a matched `/api/` route and every terminal stream error is counted in `request_errors`
(section 11) with its route template, code, account, mode and whether a reservation existed; never with
URLs, bodies or tokens.

### 6.1 Routes

| Method and path | Auth | Metered | Forwarded to engine |
|---|---|---|---|
| `GET /plans` | none | no | – |
| `POST /devices/token` | none (rate limited) | no | – |
| `POST /devices/current/revoke` | device | no | – |
| `GET /session` | device | no | – |
| `POST /consent` `{"version", "passive_learning"}` | device | no | – |
| `POST /consent/withdraw` | device | no | – |
| `POST /assist` (mode `context`) | device | yes | `/browser/assist` |
| `POST /assist` (mode `schedule`) | device + confirmed email + consent; 60 a minute; the account's one request slot | no | `/browser/assist` |
| `POST /assist/stream` (modes `chat`, `enhance_composer`) | device | yes | `/browser/assist/stream` |
| `GET /requests/{idempotency_key}` | device | no (replay) | – |
| `POST /requests/{idempotency_key}/cancel` | device | – | `/internal/requests/{id}/cancel` |
| `POST /snapshots/profile` | device + passive-learning consent | no | `/snapshots/profile` |
| `POST /snapshots/metrics` | device + consent | no | `/snapshots/metrics` |
| `POST /outcomes` | device | no | `/outcomes/record` |
| `POST /images/feedback` | device | no | `/images/feedback` |
| `POST /preferences/confirm` | device | no | `/preferences/confirm` |
| `GET /own-material` | as `schedule` above | no | `/own-material/list` |
| `POST /own-material` | as `schedule` above | no | `/own-material/add` |
| `DELETE /own-material/{id}` | as `schedule` above | no | `/own-material/remove` |

The four unmetered routes that reach an engine START it, and engines are the scarcest resource
(`ENGINE_MAX_LIVE`), so they pass the same gate as the metered ones (`_forward_unmetered`): account
active, email confirmed, disclosure accepted, `ENGINE_UNMETERED_PER_ACCOUNT` (60 a minute), and the
account's single request slot. An engine with a request in flight is never evicted; without the gate a
few unconfirmed accounts could hold every engine.

The website's writing page (section 10, `POST /write/draft`) drafts through the same function as
`POST /assist` (`api.engine_routes.metered_assist`): consent, circuit breaker, `usage.reserve` (allowance,
rate limits, fair use, pause), the account's one request slot, the engine's `/browser/assist`, settlement
and the stored result. It sends mode `enhance_composer` (the extension's Create reply, quote or post),
which `/api/v1/assist` itself still refuses from the extension; its reservations record `route`
`/write`, and it checks its own disclosure (`web_disclosure`, section 11) instead of the extension's.

`/assist` rejects modes `chat` and `enhance_composer` with 400; `/assist/stream` rejects `context` and
`schedule`. Request bodies keep the shape of `BrowserAssistRequest` in `extension/shared/types.ts`;
chat messages are limited to 2,000 characters in hosted mode. Bodies of both routes are limited to
256 KiB (413 `payload_too_large`); attached images are optional context, so the extension keeps each
under about 90 KB and the whole body under 240 KB, and leaves out images that still do not fit.

`POST /consent` accepts only a `"version"` equal to `STARLING_CONSENT_VERSION`; any other version is 400
`bad_request`. The version is also compiled into the extension (`extension/shared/consent.ts`), so change
both together: publish the extension that shows the new disclosure, then raise `STARLING_CONSENT_VERSION`
and `STARLING_EXTENSION_MIN_VERSION` at the same time (older extensions then get 426
`extension_outdated`). `POST /consent/withdraw` also revokes the calling device, which signs that browser
out. `POST /devices/token` names the device with the label captured by `/connect`; a `device_label` in the
token request is ignored.

`GET /plans` (cacheable for 5 minutes) returns `{"unit": {"name", "label"}, "plans": [{"code", "name",
"monthly_credits", "amount_cents", "currency", "interval", "is_paid", "price_label", "rate_per_minute",
"rate_per_day"}]}` for public plans only (no Stripe ids, no cost ceilings). `GET /session` also starts the
account's engine in the background when the account is active, has accepted the current disclosure, AI is
not paused and a slot is free; it never waits for it and never evicts another customer's engine.
`POST /requests/{key}/cancel` is scoped to the calling account: 200 `{"ok": true, "cancelled": true,
"request_id"}` or 404 `not_found`. Reservations record `route` as `/assist` or `/assist/stream`.
`POST /assist` responses add the `usage` block (6.2) to the engine's response; a request cancelled before
the engine answered returns `{"ok": false, "cancelled": true, "usage": {...}}`.

Captures (`/snapshots/profile`, `/snapshots/metrics`, `/outcomes`, `/images/feedback`,
`/preferences/confirm`) never start an engine. When the engine is not running or forwarding fails, the
capture is spooled (`/data/spool/<tenant_seq>.jsonl`, at most 1 MB with the oldest entries dropped,
deleted with the customer's data) and the answer is 202 `{"ok": true, "queued": <bool>}`.
`/snapshots/profile` always needs passive-learning consent. A `/snapshots/metrics` capture needs it too
unless `metadata.capture_mode` is present and does not start with `passive`: the extension sends
`publication_proof` for a post published from a Starling draft, which needs only the main disclosure.

### 6.2 Session JSON (`GET /api/v1/session`)

The same structure, produced by one server function `account_state(account_id)`, is used by the
website account pages. Both must always show identical numbers.

```json
{
  "account": {"id": "uuid", "email_masked": "m***@example.com", "email_verified": true, "status": "active"},
  "plan": {"code": "starter", "name": "Starter", "status": "active", "interval": "month",
           "period_start": 1757800000, "period_end": 1760400000,
           "cancel_at": null, "scheduled_change": null, "payment_problem": false, "payment_issue": null,
           "upgradable": true},
  "usage": {"unit": "draft", "unit_label": "drafts", "allowance": 250, "used": 12, "reserved": 0,
            "remaining": 238, "resets_at": 1760400000, "near_limit": false, "fair_use_blocked": false,
            "per_request": 1},
  "consent": {"required_version": "2026-09-22.1", "accepted_version": "2026-09-22.1", "passive_learning": false},
  "service": {"status": "ok", "scope": null, "message": null, "retry_at": null},
  "extension": {"min_version": "1.0.0"}
}
```

- `account.status`: `active`, `suspended`, `deleting`.
- `plan.status`: `free`, `active`, `past_due`, `suspended`. `plan.code` is `free` when there is no
  entitling subscription.
- `plan.scheduled_change`: `null` or `{"plan_code": "starter", "effective_at": 1760400000}`.
- `plan.payment_problem`: a payment needs the customer. `plan.payment_issue` says which one, because
  each needs different words and a different button: `null`, or
  `{"kind": "first_payment" | "renewal" | "lapsed", "plan_code": "starter", "plan_name": "Starter"}`
  (`plan_code` and `plan_name` are `null` when the subscription's price maps to no plan).
  - `first_payment`: an `incomplete` subscription. The customer was never charged and is on Free. The
    page offers "Finish paying", which posts `plan_code` to `/billing/checkout` and lands on that
    invoice's hosted page (section 8).
  - `renewal`: `past_due` inside `PAST_DUE_GRACE_DAYS`. Paid access continues; the page offers the
    portal's `payment_method_update` flow.
  - `lapsed`: `past_due` after the grace window. The account is on Free until the payment succeeds;
    same button as `renewal`.
  Clients that do not know the field keep working from `payment_problem` alone.
- `plan.upgradable`: a public paid plan ranks above this one (price, then allowance). False on the top
  plan, where clients offer support instead of an upgrade.
- `usage.near_limit`: `used + reserved >= 0.8 * allowance` and `remaining > 0`.
- `usage.per_request`: credits one press uses (`AI_CREDIT_MULTIPLIER`, section 7), so clients never
  hard-code "1 draft".
- `service.status`: `ok` or `paused`. When `ok`, `scope`, `message` and `retry_at` are `null`. When
  `paused`: `scope` is `all` (owner pause, or a global AI budget exhausted) or `free` (the Free plan's own
  daily AI budget: only Free accounts are refused, paid plans keep drafting); `message` is the server's
  sentence, shown as is (clients add nothing like "Try again later" to it); `retry_at` is when drafting
  is expected back (epoch seconds; clients show it in the reader's time zone).

### 6.3 HTTP status → error code

| HTTP | Codes |
|---|---|
| 400 / 422 | `bad_request` |
| 401 | `signed_out` |
| 402 | `limit_reached`, `payment_required`, `fair_use_exceeded` |
| 403 | `email_unverified`, `consent_required`, `account_suspended` |
| 404 | `not_found` |
| 409 | `busy`, `duplicate_request`, `idempotency_mismatch` |
| 413 | `payload_too_large` |
| 426 | `extension_outdated` |
| 429 | `rate_limited` (with `Retry-After` and `retry_after_s`) |
| 500 | `server_error` |
| 503 | `ai_unavailable`, `service_paused` (with `Retry-After`) |

402 and near-limit responses include the `usage` block from 6.2 and `upgrade_path`
(`/pricing?from=extension&reason=limit`).

`POST /assist` returns the engine's own refusals (`{"ok": false, "error_code"}`, usually a services error of
section 4.1 passed on) as these errors, mapped as the stream route maps its `error` events:
`budget_exhausted` and `service_paused` → 503 `service_paused`, `fair_use_exceeded` → 402, the engine's
`bad_request` → 400, anything else → 503 `ai_unavailable`. The reservation is released first, so the
body carries the `usage` block and nothing was used. The gateway gives `/assist` 56 seconds (the engine
budgets 50, the extension waits 60).

Suspending an account from the dashboard also raises `auth_epoch`, so a browser that is already
connected gets 401 `signed_out` on its next call, while a new sign-in and the account's own metered
requests are refused with 403 `account_suspended`.

Internal only (never returned to clients): 409 `reservation_inactive`, used between the gateway, the usage
service and the services proxy when a reservation has already finished or expired. The gateway treats it as
a finished request.

### 6.4 Stream (`POST /api/v1/assist/stream`)

`Content-Type: application/x-ndjson`; headers are sent immediately and the first line arrives within
one second: `{"event": "status", "stage": "accepted", "request_id": "<idempotency key>"}`.
Proxies must not buffer (`Cache-Control: no-store, no-transform`, `X-Accel-Buffering: no`).

Events:

- `{"event": "status", "stage": "..."}` (engine stages passed through)
- `{"event": "queued", "position": 1}`
- `{"event": "token", "text": "..."}`
- `{"event": "heartbeat"}` after 10 seconds without other output
- `{"event": "usage", "usage": {...}}` after the reservation is committed or released
- `{"event": "final", "response": <AssistResponse>}` exactly once on success paths. When the customer
  stopped the request after text had appeared (the one ending that uses a credit without a usable
  result, section 7), the response carries `used_after_stop: true` so the client can say why.
- `{"event": "error", "error": {"code", "message", "retry_after_s"?}}`, terminal

Engine stages, after `accepted` and the engine's opening `thinking` (or `warming`), each sent only
when it is true for the press: `reading_links` (reading the pages the post or the chat message links),
`checking_web` (a web search for the post's claim has started), `finding_topics` (Create post or an ideas
question is choosing stories for the writer's beat, which may include one web search), `writing` (a draft
or answer call has started; sent again for a second attempt; it replaced "writing another version
(n/m)"), `checking` (the finished draft is going through the integrity checks). The extension labels
these five; a client must tolerate stages it does not know.

Limits: 240 seconds total; about 90 seconds to the first token.

### 6.5 Idempotency and replay

- `usage_reservations` has `UNIQUE(account_id, idempotency_key)` and stores `payload_hash`
  (SHA-256 of the canonical JSON body).
- Same key, different body → 409 `idempotency_mismatch`.
- Same key, state `committed` → replay: `/assist` returns the stored JSON; `/assist/stream` returns
  `final` and `usage` only. No second charge.
- Same key, state `reserved` or `active` → 409 `duplicate_request` (the client may poll
  `GET /requests/{key}`).
- `GET /requests/{key}` never charges: 202 `{"request_id", "state": "pending"}` while the request is still
  running for this account; 200 `{"request_id", "state": "done", "response": <AssistResponse>, "usage":
  {...}}` while a stored result exists; otherwise 404 `not_found`. After a dropped stream the extension
  polls it while the answer is 202 (until 240 seconds after the request started), then shows the stored
  result or "interrupted".
- Same key, state `released` or `expired` → a new attempt with the same key.
- Same key, state `committed` but nothing stored to replay (stopped by the customer after text was shown,
  or the stored result expired) → a new attempt, charged again if it runs. The extension only reuses a key for automatic
  retries before any response arrived, so this does not arise in normal use.
- Stored results are kept for 15 minutes. Only usable results are stored.
- Keys may not contain `#` (400).

---

## 7. Usage accounting (authoritative rules)

- **Unit:** one credit per metered request, multiplied by `AI_CREDIT_MULTIPLIER` for the configured
  quality model (default 1).
- **Reserve** (one `BEGIN IMMEDIATE` transaction on the writer thread), checked in this order:
  account active → email verified → consent current → service not paused → entitlement → rate limits
  → per-account concurrency (1 active; 1 queued for up to 15 seconds, otherwise 409 `busy`) →
  `available = allowance(period) + grants − committed − reserved ≥ credits` → period AI cost below the
  plan ceiling. Insert the reservation as `reserved`.
- **Active:** when dispatched to the engine. The services proxy admits AI calls only while the
  reservation is `active`.
- **Exposure:** the first `token` event forwarded to the client sets `exposure_at`.
- **Commit** in two cases only:
  - the final response is usable and at least one AI call for the request succeeded (`ai_calls.status =
    'ok'`; the attempt counter on the reservation also counts refused and failed calls, so it is not used
    for this). A streamed call is recorded by the services process just after its last line, so a call
    for the request that is still unrecorded (`finished_at` empty) counts as succeeded when the final is
    usable;
  - the customer ended the request after `exposure_at` was set: the extension's Stop
    (`POST /requests/{key}/cancel`, `finish(..., customer_stopped=True)`) or the client going away
    (release reasons `cancelled`, `client_disconnected`). They saw part of it; the stream's `final` says
    `used_after_stop: true`.
- **Release** in every other case, **even after text was shown**: a preview that the engine then
  withheld, an unchanged Enhance, engine errors, timeouts, an engine that went away, a busy slot,
  shutdown, cancellation before exposure, empty results, and answers that needed no AI. A provider
  outage or any other failure on Starling's side therefore never costs a customer a credit. What the AI
  cost is still recorded (`released_with_spend`).
- **Lock:** the per-account slot is held until the engine reports the request finished (final, error or
  cancel acknowledged), or until the hard wall of 300 seconds.
- **Expiry:** a sweeper runs every 30 seconds. `active` rows past the hard wall become `expired`
  and are released (release reason `expired`), exposed or not: the request died on Starling's side. The
  engine receives a cancel. Rows an older version settled as charged are `expired` with no
  `release_reason` and a `commit` ledger row; `verify_counters` treats them as charged.
- **Restart:** when the gateway starts, every row still `reserved` or `active` is released at once by
  the same rule (release reason `gateway_restart`). Only one gateway process exists, so nothing can be
  running them: they belong to requests that died with the previous process, and their engines have
  been stopped. Without this their credits would count as in use until the hard wall.
- **Counter check:** at startup and once a day the gateway compares `usage_counters` with the ledger
  and the reservations (`usage.service.verify_counters`). A disagreement raises the owner alert
  `alert:usage:counters` (dashboard and alert email); a clean check clears it.
  `python -m starling_server.cli usage-verify` prints every disagreement and exits 1 when there is one.
- **Background AI** (calls with `X-Starling-Bucket: background`) is capped at 10% of the plan's period
  cost ceiling and never consumes credits.
- **Recording:** every AI call's cost is recorded, including calls for released requests
  (`released_with_spend` counter, computed when the request is released; spend recorded after the
  release, from a call still running at cancel, is not counted there).
- **Cost columns:** `usage_counters.cost_micros` is the account's TOTAL AI spend in the period (press,
  background and system calls attributed to the account); `background_micros` is the background subset.
  The fair-use ceiling compares the total; background spend is additionally capped at 10% of the ceiling.
  The services process owns `cost_micros` and `background_micros`; the usage service owns `committed`,
  `reserved`, `granted` and `released_with_spend`.

### 7.1 Periods and entitlement

| Situation | Rule |
|---|---|
| No subscription | Plan `free`; monthly periods anchored to the email verification time |
| Signing up again with an address whose account was deleted | Plan `free` with a zero allowance and zero cost ceiling until the customer subscribes: deletion leaves a `tombstones` row keyed by the HMAC of the allowance key (section 11), which covers plus tags and, for Gmail, dots |
| New paid subscription | Paid period allowance is granted on `invoice.paid` using the invoice line's own period, so an old invoice can never grant a newer period; the first paid period stays free until its invoice is paid |
| Upgrade | New plan only after the proration invoice is paid; `allowance += (new − old) × remaining / period`, applied to the current period row (the cost ceiling is prorated the same way); replaying the payment changes nothing |
| Downgrade | Server-created subscription schedule; takes effect at period end |
| Cancel | Cancel at period end; paid until then, then `free` |
| `past_due` | Paid access for `PAST_DUE_GRACE_DAYS` (default 7) after the first failure; no new period allowance until paid: a provisional period carries the unused allowance of the last paid period, capped at 10% of the plan allowance, and becomes the paid period when the renewal invoice is paid |
| `incomplete`, `incomplete_expired`, `unpaid`, `canceled`, `paused` | `free`. `trialing` entitles like `active`, but like every status it grants nothing until an invoice has paid for a period, so a trial nobody paid for shows the free allowance |
| An entitling subscription with no paid period beside one that has | The paid one decides. Resolution walks the entitling subscriptions from the highest plan down and takes the first that a paid invoice (or its renewal carry-over) covers; an unpaid higher subscription never hides a lower one the customer paid for |
| Open dispute | `suspended`: metered requests are refused with 402 `payment_required` (the free allowance is not usable either). Closing as `won`, `warning_closed` or `prevented` restores access only when no other dispute of that customer is still open or was lost (the processor lists the customer's disputes at Stripe to decide); `lost` cancels the subscription, keeps the account suspended for good and raises an owner alert |
| Paid invoice for a subscription that has already ended | The period row is written but can never resolve, so the account stays `free`; owner alert `paid_invoice_without_access` (money arrived without access: refund or reinstate by hand) |
| Full refund (the subscription is cancelled immediately), customer deleted, subscription 404 | `free` |
| Unknown price id | `free` plus an owner alert |
| More than one entitling subscription | Highest plan (the older one on a tie) plus an owner alert; the daily resync and `resync_account` cancel the others immediately, crediting unused time to the customer's balance |

### 7.2 Plans file (`backend/server/config/plans.toml`)

```toml
[unit]
name = "draft"
label = "drafts"

[plans.free]
name = "Free"
monthly_credits = 25
cost_ceiling_usd = 0.30
rate_per_minute = 3
rate_per_day = 10
public = true

[plans.starter]
name = "Starter"
monthly_credits = 250
stripe_lookup_key = "starling_starter_monthly_usd"
amount_cents = 900
currency = "usd"
interval = "month"
cost_ceiling_usd = 4.50
rate_per_minute = 6
rate_per_day = 60
public = true

[plans.pro]
name = "Pro"
monthly_credits = 800
stripe_lookup_key = "starling_pro_monthly_usd"
amount_cents = 1900
currency = "usd"
interval = "month"
cost_ceiling_usd = 9.00
rate_per_minute = 10
rate_per_day = 150
public = true
```

Pricing page, checkout, webhooks, enforcement and both displays read this one file. In production the
gateway compares each Stripe price (amount, currency, interval) with this file at startup and refuses
to start on a mismatch.

---

## 8. Stripe

- API version pinned: `2026-08-26.dahlia`. Client: `billing/stripe_client.py` over httpx (no SDK).
- Signatures: HMAC-SHA256 over `"{t}.{raw_body}"`; accept any `v1` signature matching the current or
  previous webhook secret; tolerance 300 seconds (never 0); constant-time comparison.
- Field locations: billing periods from `items.data[0].current_period_start/end`; invoice to
  subscription via `parent.subscription_details.subscription` when
  `parent.type == "subscription_details"`; cancellation is `cancel_at_period_end` or a non-null
  `cancel_at`.
- Subscribed events: `checkout.session.completed`, `checkout.session.expired`,
  `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`,
  `customer.subscription.pending_update_applied`, `customer.subscription.pending_update_expired`,
  `invoice.paid`, `invoice.payment_failed`, `invoice.payment_action_required`, `charge.refunded`,
  `charge.dispute.created`, `charge.dispute.closed`, `customer.deleted`,
  `subscription_schedule.released`, `subscription_schedule.canceled`.
- Processing: verify, insert `billing_events` (status `received`), return 200, then a single serial
  queue worker processes events: refetch the latest objects, upsert the mirror, recompute entitlement,
  mark `processed` (or `failed` with the error). Startup re-queues `received`, `processing` and `failed`
  rows. Failed events retry with backoff (30 seconds doubling to 1 hour, at most 20 attempts, then an
  owner alert). An event from the other Stripe mode gets 400, is not stored and raises an owner alert;
  event types that are not subscribed get 200 and are not stored; bodies are capped at 1 MB; without a
  webhook secret the route answers 503. Late events for a deleted customer are ignored.
- Recovery: hourly reconciler lists events from the last 30 days; daily full resync of customers, one
  customer at a time through the same code as `reconcile.resync_account` (the owner dashboard's
  per-customer resync); a customer that fails at Stripe is counted and skipped.
- Checkout: sessions are created server-side only and recorded in `checkout_sessions`; the customer is
  created under the per-account lock (`billing/mirror.py`, shared by checkout, portal, schedule actions,
  resync and the event processor; never inside a database write transaction) with idempotency key
  `cust-<account_id>` (`cust-<account_id>-<replaced_customer_id>` after a Stripe-side deletion, so two
  deletions inside one second cannot share a key) and a database re-check. The mirrored customer is
  verified at Stripe on every checkout (`GET /v1/customers/<id>`): one deleted there whose
  `customer.deleted` webhook was lost is tombstoned and replaced instead of blocking checkout for ever.
  Live refetch of the customer's subscriptions before creating a session, and a new checkout is
  refused (portal offered) while a subscription is active, trialing, past_due, incomplete or paused,
  with two exceptions for a FIRST payment that never finished (every blocking subscription is
  `incomplete`), because the Customer Portal cannot pay an invoice that is already open:
  the same plan again returns that invoice's `hosted_invoice_url` (`CheckoutResult.kind == "invoice"`;
  paying it activates the subscription they already started, so no second one can appear), and a
  DIFFERENT plan cancels the unfinished subscription (only one of Starling's own plans, never one whose
  first invoice Stripe already shows as paid), expires its Checkout Session and opens a new checkout.
  Stripe's hosted invoice page takes no return URL: the customer comes back by themselves. Other
  open sessions for the account are expired; `expires_at` = now + 30 minutes + 60 seconds (tolerates a
  gateway clock slightly behind Stripe's); `client_reference_id` and metadata carry the account id;
  `payment_method_options.card.restrictions.funding_types_blocked=["prepaid"]` when
  `STRIPE_BLOCK_PREPAID=true`. The success page grants nothing; it polls `/billing/status`.
- Customer portal (created by the setup command): plan switching off; cancel at period end on; payment
  method update, invoice history, address and tax id on; email editing off. Sessions never fall back to
  Stripe's default configuration. `POST /billing/portal` takes `flow` = `payment_method_update`,
  `subscription_cancel` or empty (a plain management session).
- Return page: `GET /billing/return` reports `session_status` `pending` (a session still open),
  `complete`, `expired` (one hour past expiry) or `not_found`; `GET /billing/status` returns
  `{"state": <6.2>}` (JSON callers send `Accept: application/json` to get 401 instead of a login redirect).
  While `plan.payment_issue.kind` is `first_payment` the page does not wait: it says the payment is not
  finished and posts the plan to `/billing/checkout`. The poll also reloads when `payment_problem` appears.
- `form-action` in the site's Content-Security-Policy names every Stripe origin a billing form is
  redirected to, because a browser applies the policy to the redirect: `checkout.stripe.com`,
  `billing.stripe.com` and `invoice.stripe.com` (`security/middleware.py`).
- Upgrade: portal deep link `subscription_update_confirm` to the target price with
  `proration_behavior=always_invoice`.
- Downgrade: subscription schedule whose second phase uses the lower price from the current period
  end; undo releases the schedule.
- Setup command: idempotent through `lookup_keys` and `metadata.starling_setup`; never changes amounts
  of existing prices.
- Startup guard: key prefix matches `STRIPE_MODE`; `GET /v1/account` id matches `STRIPE_ACCOUNT_ID`
  (if a restricted key is not permitted to read the account, the id check is skipped with a startup
  warning, never a crash); event `livemode` matches the mode.
- Price history: `price_map` rows are never deleted. After a price change the old price ids stay mapped to
  their plan, so existing subscribers keep their plan until they change it; the plan's `lookup_key` moves
  to the new price, so checkout sells the new one.
- Webhook intake never answers 500 to a hostile request: a malformed `Stripe-Signature` (non-ASCII digits,
  thousands of digits, more than 16 signatures, oversized headers) is a shaped 400, and so is a body that
  is not a Stripe event (wrong encoding, lone surrogates, nesting deep enough to exhaust the parser).
- Full refund of a subscription charge: cancel the subscription immediately in Stripe and fall back to
  `free` (a refunded customer must not be charged again). Partial refunds change nothing automatically and
  raise an owner alert. Charges have no `invoice` field in this API version; they are mapped to
  subscriptions through `/v1/invoice_payments`.
- Stripe Managed Payments (merchant of record) is not used; its compatibility with subscription schedules
  and `always_invoice` upgrades is unverified.

---

## 9. Extension connect (sign-in)

1. The extension creates `code_verifier` (43+ base64url chars), `code_challenge = BASE64URL(SHA256(verifier))`
   and `state` (random), then calls `chrome.identity.launchWebAuthFlow` with:
   `${ORIGIN}/connect?client_id=<extension id>&redirect_uri=https://<extension id>.chromiumapp.org/connect&code_challenge=...&code_challenge_method=S256&state=...&device_label=...`
2. The gateway requires `redirect_uri == "https://<client_id>.chromiumapp.org/connect"` with
   `client_id` in `STARLING_EXTENSION_IDS`. The user signs in if needed. The approval page shows the
   extension id and device label; approval is a POST with CSRF, never a GET.
3. On approval the gateway issues a code (256-bit, single use, 120 seconds, bound to challenge,
   account, extension id and redirect URI) and redirects (303) to `redirect_uri?code=...&state=...`.
4. The extension checks `state`, then calls `POST /api/v1/devices/token` with
   `{"grant_type": "authorization_code", "code", "code_verifier", "redirect_uri", "client_id", "device_label", "extension_version"}`
   and receives `{"token": "stl_dev_...", "device_id": "...", "idle_expires_at": ..., "expires_at": ...}`.
5. Device tokens: 256-bit random with prefix `stl_dev_`, stored as SHA-256, 60-day idle and 1-year
   absolute expiry, `last_used_at` written at most every 10 minutes, valid only on `/api/v1`, revoked on
   sign-out, from `/account/devices`, on a password change and on a password reset (both via
   `auth_epoch`), and when the account is deleted. An account may hold at most
   `MAX_DEVICES_PER_ACCOUNT` live devices (default 20, 0 = no limit): at the limit the approval
   page says so and the pending request stays usable, so disconnecting a browser and pressing
   Approve again works.

6. Connect does not require a verified email. Until the address is verified the extension shows
   "Verify your email" and metered requests get 403 `email_unverified`.
7. The device id equals the connect request id, so replaying a code revokes the device it created; every
   redemption attempt uses up the code, including one with a wrong verifier. The approval page sends its
   own Content-Security-Policy whose `form-action` allows `https://<extension id>.chromiumapp.org`.

A relay-page fallback is used only if `launchWebAuthFlow` proves unusable; that decision is recorded
here when made. Sign-in inside the auth window is bounded by the service worker lifetime (about five
minutes for one pending call).

---

## 10. Website routes and template context

The gateway renders templates from `STARLING_WEB_DIR` (default `web/`). Every template receives:
`site` (`name`, `public_url`, `support_email`, `extension_install_url`, `legal` values or placeholders
including `retention_periods` and `refund_policy`, `env`, `google_sign_in`), `user` (`null` or `{"email",
"email_masked", "is_admin"}`: a person's own pages show their own address in full), `csrf_token`, `flash`
(list of `{"level", "text"}`), `request_path`. Each template's header
comment documents its exact context, and `web/tests/sample_contexts.py` holds examples. 404 and 5xx pages
render `errors/404.html` and `errors/500.html` (with `request_id`), falling back to plain text.

| Route | Template | Extra context |
|---|---|---|
| `GET /` | `index.html` | `plans`, `unit` |
| `GET /pricing` | `pricing.html` | `plans`, `unit`, `state` (if signed in) |
| `GET /docs/<page>` | `docs/<page>.html` | – |
| `GET /privacy`, `/terms`, `/refunds`, `/subprocessors`, `/acceptable-use`, `/contact` | `legal/<name>.html` | `subprocessors` |
| `GET/POST /signup` | `auth/signup.html` | `errors`, `form`, `next` |
| `GET/POST /login` | `auth/login.html` | `errors`, `form`, `next`, `google_link` (null, or the Google account waiting for this password: `{"email", "email_masked"}`) |
| `POST /logout` | – | – |
| `GET /verify-email/sent`, `POST /verify-email/code`, `POST /verify-email/resend`, `POST /verify-email/continue` | `auth/verify_sent.html` | `email_masked`, `email`, `code_form`, `confirmed`, `mail_link`, `errors` |
| `GET /verify-email/status` | JSON | `state`: `waiting` or `confirmed` (asked every few seconds by the check-your-email page) |
| `GET/POST /verify-email` | `auth/verify.html` | `token_valid`, `token`, `already_confirmed` |
| `GET/POST /forgot-password` | `auth/forgot.html` | `sent`, `errors`, `form`, `mail_link` |
| `GET/POST /reset-password` | `auth/reset.html` | `token_valid`, `token`, `errors` |
| `GET /auth/google` (`intent` login, signup or reauth; `next`), `POST /auth/google/link`, `GET /auth/google/callback` | redirects | – |
| `GET/POST /auth/google/new` | `auth/google_new.html` | `email`, `next`, `errors` |
| `GET /account` | `account/overview.html` | `state` (6.2), `devices_count` |
| `GET /account/billing` | `account/billing.html` | `state`, `plans`, `invoices_url_available`, `returned_from`, `chosen_plan` |
| `GET /account/devices`, `POST /account/devices/{id}/revoke` | `account/devices.html` | `devices` |
| `GET /account/data`, `POST /account/export`, `POST /account/delete` | `account/data.html` | `errors` (deletion fields `delete_password` and `delete_confirmation`, which must be `DELETE` in any case), `sign_in`, `plan` |
| `GET /account/security`, `POST /account/security/password`, `/account/security/email`, `/account/security/sessions`, `/account/security/google/disconnect`, `/account/security/reset-link` | `account/security.html` | `sessions`, `errors` (per form; general key `form`), `sign_in` |
| `GET /goodbye` | `goodbye.html` | `email`, `plan_name`, `pending` |
| Every other refusal (403, 413, 429, 503) | `errors/problem.html` | `status`, `title`, `message`, `action`, `secondary` |
| `POST /account/verify/resend` | redirect | – |
| `GET /connect`, `POST /connect/approve`, `POST /connect/deny` | `connect/approve.html`, `connect/error.html` | `extension_id`, `device_label`, `connect_request_id`, `connect_state`; the error page `error` |
| `POST /billing/checkout`, `/billing/portal`, `/billing/upgrade`, `/billing/downgrade`, `/billing/downgrade/undo`, `/billing/resume` | redirects | – |
| `GET /billing/return` | `billing/return.html` | `session_status`, `state` |
| `GET /billing/status` | JSON | `state` |
| `GET /extension/removed` | `extension_removed.html` | – |
| `GET /write` (share target: `title`, `text`, `url`) | `write/index.html` | `state`, `consent` `{"given", "granted_at", "version"}`, `disclosure`, `prefill` `{"text", "link", "shared"}`, `next_path` |
| `POST /write/consent` (`agree=yes`, `next`), `POST /write/consent/withdraw` | redirects | – |
| `POST /write/link` (JSON `{"url"}`) | JSON | `{"ok": true, "post": {"url", "status_id", "author_name", "author_handle", "text", "lang", "has_media"}}` or `{"ok": false, "link", "status_id", "message"}` |
| `POST /write/draft` (JSON, `Idempotency-Key`) | JSON | `{"ok", "request_id", "kind", "usage", "draft", "parts", "handoff" ({"label", "url"} or null), "voice_note"?, "review_note"?, "search_note"?, "sources"?}`, or with `draft` null: `"message"`, `"can_write_anyway"`, `"reasons"?` |
| `GET /write/requests/{key}` | JSON | 202 `{"request_id", "state": "pending"}`, 200 `{"request_id", "state": "done"}`, or 404 |
| `GET /write/voice`, `POST /write/voice/add`, `POST /write/voice/{id}/remove` | `write/voice.html` | `state`, `consent`, `disclosure`, `samples` (None or `[{"id", "text", "added_at"}]`), `limits`, `errors`, `form`, `unavailable`, `next_path` |
| `GET /manifest.webmanifest` | `application/manifest+json` | – |
| `GET /sw.js`, `GET /offline` | the service worker (`static/js/sw.js`, `Service-Worker-Allowed: /`), `offline.html` | – |
| `GET/POST /admin/totp`, `GET/POST /admin/elevate` (auth routes) | `admin/totp.html`, `admin/elevate.html` | `secret`, `otpauth_uri`, `errors`; `errors`, `next` |
| `GET /admin`, `/admin/customers`, `/admin/customers/{id}` (POST `grant`, `suspend`, `unsuspend`, `revoke-devices`, `resync-billing`), `/admin/failures`, `/admin/webhooks`, `/admin/audit`, `/admin/ai` (POST `test`, `pause`, `resume`) | `admin/*.html` | documented in each template header |
| `POST /webhooks/stripe` | – | – |
| `GET /healthz`, `/readyz`, `/robots.txt`, `/sitemap.xml`, `/.well-known/security.txt` | – | – |

Legal placeholders use the form `[[OPERATOR_COMPANY_NAME]]`. Production refuses to start while any legal
setting still holds a placeholder (section 12); development pages highlight the missing values.

The writing page (`web/write.py`) is the extension's drafting for a phone, where extensions cannot run.
Signed-in only (signed-out visitors go to `/login?next=` this page, shared text included). Every POST
needs the CSRF token (form field, or `X-CSRF-Token` on the JSON routes) and passes the origin check; the
JSON routes answer refusals with the error body of section 6.3. Nothing is sent from the page (no draft,
no link lookup, no writing sample) until its own disclosure (`web.write.DISCLOSURE`) is accepted, which
records `consents` kind `web_disclosure` at `web.write.WEB_DISCLOSURE_VERSION`; that version is separate
from `STARLING_CONSENT_VERSION`, which is compiled into the extension, and a test pins the text to its
hash. Drafts: section 6.1. A link to a post goes to the services process (`POST /v1/x/post`, section
4.2), at most 30 per account in 10 minutes. The hand-off link opens X's own post box
(`https://x.com/intent/post` with `in_reply_to` for a reply whose post id is known, `url` for a quote,
`text` alone for a post; omitted past 8,000 characters or for a thread); the person still presses Post.
Writing samples are the engine's own material of kind `voice`, through the same gate as
`/api/v1/own-material`. The manifest starts the installed app at `/write` and makes it an Android share
target (GET, so nothing is sent before the person presses a button). The service worker keeps only the
offline page and the stylesheets it names, fetched without cookies.

`POST /account/export` returns one JSON download: `exported_at`, then `account`, `devices`, `sessions`,
`consents`, `billing` and `usage` from the control database (no password, TOTP secret, session id or
device token), and `drafting` from the engine's `GET /profile/export` (own material, drafts and their
candidates, ratings, the queue, saved pages, snapshots, goals, campaigns, experiments and preferences).

---

## 11. Control database (SQLite, WAL)

One writer thread per process owns all writes (`db/writer.py`); reads use separate connections.
`PRAGMA journal_mode=WAL; busy_timeout=5000; synchronous=FULL; foreign_keys=ON`.

| Table | Key columns | Constraints and indexes |
|---|---|---|
| `schema_migrations` | version, applied_at | PK(version) |
| `accounts` | id, tenant_seq, email, email_norm, allowance_key, email_verified_at, password_hash, password_changed_at, auth_epoch, status, is_admin, totp_secret_enc, signup_ip_prefix, created_at, deleted_at | UNIQUE(email_norm); UNIQUE(tenant_seq); IDX(allowance_key) |
| `email_tokens` | id, account_id, purpose (`verify`, `reset`, `email_change`), token_hash, new_email, created_at, expires_at, used_at, browser_hash, code_hash, code_attempts, code_used_at, new_password_hash | UNIQUE(token_hash); IDX(browser_hash). An email change also sends the old address a 7-day `reset` token carrying the old address in `new_email`; using it restores the address, requires a new password and signs everything out. A sign-up `verify` token carries the 6-digit code (an HMAC) and the hash of the random value in the signing-up browser's notice cookie; a second sign-up for an unconfirmed address also holds its password hash, applied only when that browser confirms (migration 5) |
| `account_identities` | provider (`google`), subject, account_id, email, created_at, last_used_at | PK(provider, subject); UNIQUE(account_id, provider). Sign in with Google (migration 4); matched by the provider's stable subject id, never by address |
| `web_sessions` | id_hash, account_id, auth_epoch, csrf_secret, created_at, last_seen_at, idle_expires_at, abs_expires_at, admin_elevated_until, ip_prefix, user_agent, revoked_at | PK(id_hash); IDX(account_id) |
| `rate_limits` | bucket, key, window_start, count | PK(bucket, key, window_start); also records used TOTP steps so a code cannot be replayed |
| `connect_requests` | id, account_id, code_challenge, state_hash, extension_id, redirect_uri, device_label, code_hash, created_at, approved_at, expires_at, redeemed_at | UNIQUE(code_hash); the device issued for a request has the request's id |
| `devices` | id, account_id, token_hash, token_prefix, label, extension_id, extension_version, auth_epoch, created_at, last_used_at, idle_expires_at, abs_expires_at, revoked_at, revoked_reason | UNIQUE(token_hash); IDX(account_id) |
| `consents` | account_id, kind (`tos`, `privacy`, `extension_disclosure`, `support_access`, `web_disclosure`), version, passive_learning, granted_at, revoked_at | PK(account_id, kind, version). `web_disclosure` (migration 6, which rebuilt the table for its CHECK) is the website writing page's own consent, at `web.write.WEB_DISCLOSURE_VERSION` |
| `stripe_customers` | account_id, customer_id, livemode, created_at, deleted_at | PK(account_id); UNIQUE(customer_id) |
| `checkout_sessions` | session_id, account_id, price_id, status, created_at, expires_at | PK(session_id); IDX(account_id, status) |
| `price_map` | price_id, lookup_key, plan_code, interval, currency, unit_amount, active, livemode | PK(price_id) |
| `subscriptions` | subscription_id, account_id, customer_id, status, price_id, plan_code, current_period_start, current_period_end, cancel_at_period_end, cancel_at, canceled_at, ended_at, schedule_id, scheduled_price_id, pending_update, latest_invoice_id, latest_invoice_status, first_payment_failed_at, disputed_at, livemode, synced_at | PK(subscription_id); IDX(account_id, status) |
| `entitlement_periods` | id, account_id, source (`free`, `subscription`, `grant`), subscription_id, plan_code, period_start, period_end, allowance, cost_ceiling_micros, granted_by_invoice_id, created_at | UNIQUE(account_id, source, period_start); UNIQUE(granted_by_invoice_id) |
| `usage_reservations` | id, account_id, idempotency_key, payload_hash, route, mode, state, period_id, credits, exposure_at, ai_calls, cost_micros, result_json, result_expires_at, created_at, heartbeat_at, expires_at, finished_at, release_reason | UNIQUE(account_id, idempotency_key); IDX(state, expires_at) |
| `credit_ledger` | id, account_id, period_id, kind (`commit`, `admin_grant`, `admin_debit`, `upgrade_proration`, `refund_adjust`), amount, reservation_id, actor, reason, created_at | UNIQUE(reservation_id) WHERE kind = 'commit' |
| `usage_counters` | account_id, period_id, committed, reserved, granted, cost_micros, background_micros, released_with_spend | PK(account_id, period_id) |
| `ai_calls` | id, account_id, reservation_id, bucket, lane, model, input_tokens, cached_input_tokens, output_tokens, estimated, cost_micros, status, error_code, started_at, finished_at | IDX(account_id, started_at); IDX(reservation_id). Never stores prompt or output text |
| `budget_counters` | scope (`global_day`, `global_month`, `free_day`), window_key, cost_micros, limit_micros, tripped_at | PK(scope, window_key) |
| `billing_events` | event_id, type, livemode, object_id, account_id, status, attempts, last_error, received_at, processed_at | PK(event_id); IDX(status) |
| `admin_audit` | id, admin_id, action, target_account_id, params_json, ip_prefix, created_at | IDX(created_at) |
| `email_outbox` | id, to_email, template, subject, body, created_at, sent_at, attempts, last_error | IDX(sent_at) |
| `engines` | account_id, tenant_seq, pid, listen, state, started_at, last_used_at, rss_mb, restarts | PK(account_id) |
| `tombstones` | account_hash, customer_id, deleted_at | PK(account_hash); UNIQUE(customer_id). Account deletion writes `account_hash = HMAC(STARLING_SECRET_KEY, allowance_key)` (a re-used address gets no fresh free allowance; rotating the secret key makes older rows unmatchable). Billing writes `sha256("stripe-customer:<account_id>:<customer_id>")` with the customer id, and ignores late Stripe events for it |
| `service_state` | key, value_json, updated_at | PK(key) (owner pause, alerts, backup status) |
| `request_errors` | id, minute, account_id, route, mode, code, http_status, reserved, count, first_at, last_at | UNIQUE(minute, account_id, route, mode, code, http_status, reserved); IDX(last_at); IDX(account_id, last_at) (migration 3) |

`usage_counters` is updated in the same transaction as each ledger or reservation change; a nightly
check verifies the counters equal the ledger.

Value formats:

- `budget_counters.window_key`: UTC `YYYY-MM-DD` for scopes `global_day` and `free_day`; UTC `YYYY-MM` for
  `global_month`. A row counts as tripped when `tripped_at` is set or `cost_micros >= limit_micros` (so a
  zero limit allows no spend). The services process keeps `limit_micros` equal to the configured budget
  and, at startup, clears `tripped_at` on current windows whose spend is below the configured limit.
- `service_state` key `ai_paused`: JSON `true`/`false`, or `{"paused": bool, "message": str, "retry_after_s": int}`.
  A malformed value is treated as paused (retry after 300 seconds).
- `credit_ledger.kind`: `commit` (one per charged reservation), `admin_grant` and `admin_debit` (feed
  `usage_counters.granted`). Upgrade proration raises the entitlement period's `allowance` instead of
  writing ledger rows; `upgrade_proration` and `refund_adjust` are reserved and currently unused.
- Migration 2 adds indexes `usage_reservations(account_id, created_at)` and a partial index on
  `result_expires_at` where `result_json IS NOT NULL`.
- `request_errors` also counts the writing page's metered route, template `/write/draft` (mode
  `enhance_composer`); `METERED_ROUTES` includes it, so the dashboard's failure rate covers both.
- Migration 3 adds `request_errors` (`db/request_errors.py`): one row per UTC minute (`minute` = start of
  the minute) per account (`""` when unidentified), route template, mode (`""` if none), code, HTTP status
  and `reserved` (1 when the request already held a reservation), with a `count`. Failure rate over a
  window for the metered routes: `failed = SUM(count)` and `requests = reservations created + SUM(count
  WHERE reserved = 0)`. Rows older than 30 days are pruned.
- Owner alerts in `service_state` (the dashboard normalizes all three): billing `alert:billing:<kind>:<ref>`
  = `{"message", "details", "first_at", "last_at", "count"}`; engines `alert.engine_circuit.<tenant_seq>`;
  services `alert:ai_key_rejected` = `{"at", "key", "upstream_status", "retried_with"}`. Other keys:
  `backup:last` = `{"at", "ok", "archive", "size_bytes", "verified", "error"}` and `admin:ai_last_test` (the
  dashboard's last AI connection test). The dashboard adds two alerts of its own from measurements rather
  than stored rows: a failed or more than 26 hours old backup, and `disk:low` when the data volume has less
  than 15% free (an error below 5%). Alerts are emailed to `ALERT_EMAIL` at most once per key every 6 hours.

---

## 12. Configuration (environment)

Core: `STARLING_ENV` (`production` | `development` | `e2e`), `STARLING_PUBLIC_URL`, `STARLING_DATA_DIR`
(default `/data`), `STARLING_WEB_DIR`, `STARLING_SECRET_KEY` (≥ 32 bytes, base64url), `STARLING_ISOLATION`
(`os` | `none`), `STARLING_TRUSTED_PROXIES`, `STARLING_EXTENSION_IDS`, `STARLING_OWNER_EMAILS`,
`STARLING_SUPPORT_EMAIL`, `STARLING_EXTENSION_MIN_VERSION`, `STARLING_CONSENT_VERSION`,
`STARLING_SERVICES_TOKEN_KEY` (base64url ≥ 32 bytes; required in production), `STARLING_SERVICES_URL`
(gateway → services; default `unix:/run/starling/services.sock` with OS isolation, else
`http://127.0.0.1:8766`), `STARLING_SERVICES_LISTEN` (services bind address; default
`unix:/run/starling/services.sock` or `tcp:127.0.0.1:8766`), `STARLING_MAINTENANCE` (the supervisor starts
nothing; for restores), `STARLING_WORLD_OFFLINE` (development and e2e only: no outbound news or page
fetches; refused in production), `STARLING_WORLD_FIXTURES` (e2e only, with `STARLING_WORLD_OFFLINE`: pages
and news stories from a file; refused in production).

Legal and business: `OPERATOR_COMPANY_NAME`, `OPERATOR_REGISTERED_ADDRESS`, `OPERATOR_CONTACT_EMAIL`,
`OPERATOR_PRIVACY_CONTACT`, `OPERATOR_JURISDICTION`, `OPERATOR_GOVERNING_LAW_VENUE`, `OPERATOR_EFFECTIVE_DATE`,
`HOSTING_PROVIDER`, `EMAIL_PROVIDER`, `AI_SUBPROCESSORS` (default `OpenAI`), `RETENTION_PERIODS`,
`REFUND_POLICY`. Production refuses to start while any of these still holds a `[[PLACEHOLDER]]`, and
while `STARLING_SUPPORT_EMAIL` is unset. `STARLING_EXTENSION_INSTALL_URL` (optional, https) links pages to
the published store listing.

Email: `EMAIL_MODE` (`smtp` | `outbox`), `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`SMTP_STARTTLS`, `SMTP_FROM`.

AI: `AI_PROVIDER` (`openai`, the default, or `anthropic`: the services process then calls Anthropic's
Messages API and translates requests and answers to and from the OpenAI shape of section 4.1, the defaults
below become `https://api.anthropic.com` and `claude-haiku-5-5`, and `WEB_SEARCH` must be false; see
docs/AI-PROVIDER.md), `AI_BASE_URL` (default `https://api.openai.com/v1`; https required in production), `AI_API_KEY`, `AI_API_KEY_SECONDARY`,
`AI_MODEL_QUALITY`, `AI_MODEL_FAST`, `AI_MODEL_VISION` (default `gpt-5.6-luna`), `AI_REASONING_EFFORT`
(default `none`), `AI_PRICING_FILE`, `AI_CREDIT_MULTIPLIER`, `AI_MAX_CALLS_PER_REQUEST` (12),
`AI_MAX_INPUT_TOKENS_PER_CALL` (8000), `AI_MAX_OUTPUT_TOKENS` (600), `AI_TIMEOUT_S` (25),
`AI_DAILY_BUDGET_USD` (10), `AI_MONTHLY_BUDGET_USD` (90; both below OpenAI tier 1's $100 monthly limit),
`AI_FREE_TRIP_PCT` (80: once today's global AI spend reaches this percentage of the daily budget, free-plan
accounts are refused with 503 `service_paused` while paid accounts continue).

Secret rotation: `STARLING_SECRET_KEY_PREVIOUS` (optional) is accepted when verifying CSRF tokens and
signed cookies and when decrypting stored TOTP secrets; values are re-encrypted with the current key on
their next use. Remove it after the rotation window (default guidance: 30 days).

Plans: `PLANS_FILE`, `PAST_DUE_GRACE_DAYS` (7), `DISPOSABLE_DOMAINS_FILE`.

Stripe: `STRIPE_MODE` (`test` | `live`), `STRIPE_SECRET_KEY`, `STRIPE_ACCOUNT_ID`,
`STRIPE_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_SECRET_PREVIOUS`, `STRIPE_API_BASE` (tests and e2e only;
production requires `https://api.stripe.com`),
`STRIPE_PORTAL_CONFIGURATION_ID` (management: plan switching off),
`STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID` (used only for `subscription_update_confirm` upgrades; all paid
plan prices live under one Stripe product), `STRIPE_AUTOMATIC_TAX` (false), `STRIPE_TAX_CODE`,
`STRIPE_BLOCK_PREPAID` (true), `STRIPE_ALLOW_PROMO_CODES` (false).

Engines: `ENGINE_MAX_LIVE` (default 6 on a 4 GB machine), `ENGINE_IDLE_SECONDS` (1800),
`ENGINE_MEMORY_SOFT_MB` (512), `ENGINE_MEMORY_HARD_MB` (768), `ENGINE_UID_BASE` (200000; at least 100000).

Security and operations: `ARGON2_TIME_COST`, `ARGON2_MEMORY_KIB`, `ARGON2_PARALLELISM`,
`ARGON2_MAX_CONCURRENT`, `LOG_LEVEL`, `BACKUP_DIR`, `BACKUP_PASSPHRASE` (at least 16 characters; required
for the supervisor in production), `ALERT_EMAIL` (owner alerts by email).

Extension build variables (not server settings): `WXT_STARLING_ORIGIN` (the only origin the extension talks
to; https, except http on 127.0.0.1 or localhost in `--mode e2e`), `STARLING_STORE_BUILD=1` (store build
checks), `STARLING_OUT_DIR` (build output folder).

---

## 13. Security invariants (each has a test)

1. `/_dev/*` and `/_fake/*` routes return 404 when `STARLING_ENV=production`.
2. Admin access requires a CLI grant, a verified email in `STARLING_OWNER_EMAILS`, TOTP, and elevation
   within 15 minutes for every admin POST; every admin action writes `admin_audit`.
3. `/api/v1` accepts only device tokens; web session cookies are ignored there.
4. Device tokens cannot reach website account, billing, password or deletion routes.
5. Engines never receive AI, Stripe, SMTP or session secrets (environment and stdin are checked).
6. Logs never contain prompts, drafts, tokens, passwords or email bodies.
7. A two-customer canary test proves no data crosses customers through responses, AI prompts or files.
8. Engine A cannot read engine B's files or socket (Linux container test).
9. Admin pages never read customer databases.
10. Webhook signature verification rejects wrong secrets, modified bodies and stale timestamps.
11. Production refuses the e2e switches and endpoints (`STRIPE_API_BASE` other than Stripe's, an http
    `AI_BASE_URL`, `STARLING_WORLD_OFFLINE`).
12. HTTP access logs are off, so verification and reset tokens in query strings are never logged.
13. `request_errors` stores route templates, codes and counts only, never URLs, bodies or tokens.
14. In production the gateway writes JSON log lines (`starling_server/logs.py`); an exception logs its
    class name and source line, never the traceback text or the exception message.
15. Every state-changing website form passes two checks: the CSRF token, and the origin check. A present
    `Origin` must equal the public origin. Pages served with `Referrer-Policy: no-referrer` (email
    verification, password reset, extension approval) make real browsers send `Origin: null` with their own
    form POST, so a missing or `null` Origin is accepted only together with `Sec-Fetch-Site: same-origin`,
    which a page cannot set; `Origin: null` alone, or with any other `Sec-Fetch-Site`, is refused.

---

## 14. Ownership and internal Python interfaces

The foundation (config, control database, app factory, middleware, CLI dispatcher, test fixtures)
exists in `backend/server`. Each workstream owns the files listed below and must not edit files owned
by another workstream; cross-workstream needs are raised as a contract change here.

| Workstream | Owns |
|---|---|
| Foundation (orchestrator) | `config.py`, `db/*` (incl. `request_errors.py`), `app.py`, `cli.py`, `security/middleware.py`, `account/lifecycle.py`, `tests/conftest.py`, `tests/test_foundation.py`, `config/*`, `docs/architecture/CONTRACTS.md`, `scripts/check_release.py` |
| Server core | `auth/*` (incl. `deps.py` implementation), `devices/*`, `email/*`, `account/*`, `web/*`, `system/*`, `api/session_routes.py`, `security/*` except `middleware.py`, `auth/commands.py` |
| Services | `services/*` (AI proxy, world service, `client.py`, `commands.py`), its own entrypoint `services/main.py` |
| Usage | `usage/*` (`service.py`, `concurrency.py`, `lifecycle.py`) |
| Billing | `billing/*` (`stripe_client.py`, `webhooks.py`, `reconcile.py`, `entitlements.py`, `routes.py`, `setup.py`, `commands.py`, `lifecycle.py`) |
| Engine pool | `engines/*`, `api/engine_routes.py`, `supervisor.py` |
| Admin | `admin/*`, `web/templates/admin/*`, admin entries of `REQUIRED_CONTEXT` in `web/render.py` |
| Operations | `ops/*` (backup, restore), `deploy/*` |
| Website | `web/*` (templates, static files, `templating.py`, tests), `scripts/check_site.py` |
| Engine | `backend/engine/*` |
| Extension | `extension/*` |
| End-to-end | `e2e/*` |

Interfaces (signatures are binding; implementations replace the `NotImplementedError` stubs):

- `auth.deps`: `CurrentUser`, `DeviceAuth`, `get_current_user(request)`, `require_user(request)`,
  `require_verified_user(request)`, `require_admin(request)`, `require_device(request)`,
  `verify_csrf(request)`.
- `usage.service`: `UsageError(code, http_status, message, retry_after_s, usage)`, `Reservation`,
  `Replay`, `payload_hash(body)`, `reserve(db, settings, account_id, idempotency_key, body_hash, route, mode, now)`,
  `activate(db, reservation_id, now)`, `mark_exposed(db, reservation_id, now)`,
  `finish(db, settings, reservation_id, usable, result_json, now)`, `release(db, settings, reservation_id, reason, now)`,
  `replay(db, account_id, idempotency_key, now)`, `sweep_expired(db, settings, now)`,
  `settle_orphans(db, settings, now)`, `verify_counters(db)`,
  `account_state(db, settings, account_id, now)`.
- `usage.concurrency`: `async with account_slot(account_id, max_waiting=1, wait_s=15)` raising
  `UsageError("busy", 409, ...)` when the queue is full or the wait times out.
- `billing.entitlements`: `Entitlement`, `current_entitlement(db, settings, account_id, now)`.
- `engines.pool.EnginePool`: `ensure`, `request`, `stream`, `cancel`, `stop`, `delete_data`, `close`.
- `email.sender.send_email(db, settings, to_email, template, subject, body)`.
- `services.client`: `ai_check(settings)`, `services_status(settings)`.
- `billing.reconcile.resync_account(db, settings, client, account_id, now=None) -> dict`; never raises for
  Stripe errors (`{"ok": false, "reason": "no_customer" | "stripe_error", ...}`).
- `services.metering`: `free_trip_limit(settings)`, `clear_stale_trips(conn, settings, now)`.
- `db.request_errors`: `record(db, *, route, code, http_status, now, account_id=None, mode=None,
  reserved=False)` (fire and forget) and `prune(conn, now)`; `security.errors.record_api_error(request, exc,
  route_path=None)` records `ApiError`s.
- `account.deletion.resume_pending_deletions(db, settings, pool)`; `ops.scheduler.start_backup_thread(settings)`
  returns `(thread, stop_event)`.
- `request.state` attributes: `request_id` (middleware), `account_id` (`require_device`), `assist_mode` and
  `reservation_id` (engine routes).
- App state available to routes: `request.app.state.settings`, `request.app.state.db`,
  `request.app.state.templates`; lifecycle modules may add `request.app.state.engine_pool` (engine pool)
  and `request.app.state.billing_queue` (billing).
- Lifecycle hooks, started in this order and stopped in reverse: `usage.lifecycle`, `billing.lifecycle`,
  `engines.lifecycle`, `account.lifecycle` (needs the engine pool), `email.lifecycle`, `admin.lifecycle`
  (alert emails and the `request_errors` sweep); each may define `async def startup(app)` and
  `async def shutdown(app)`. `app.state.admin_background_jobs = False` disables the admin task in tests.
- CLI: modules `<package>.commands` expose `register(subparsers)`; registered modules are `db`, `auth`,
  `billing`, `services`, `engines`, `ops` and `admin`.
- Tests for a workstream live under `backend/server/tests/<package>/`. Run them from `backend/server`
  with `..\.venv\Scripts\python.exe -m pytest tests/<package>`.
