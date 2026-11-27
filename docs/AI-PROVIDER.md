# AI provider

Last reviewed: 2026-09-18; the web search facts were checked on 2026-09-22, and the drafting
pipeline sections were rewritten for the API-native pipeline on 2026-09-22. Model prices and limits
change. Check the linked official pages again before you rely on a number.

## In short

- By default, Starling's hosted service uses OpenAI `gpt-5.6-luna` through the Chat Completions API
  with `reasoning_effort: "none"`. The same model serves all three internal lanes: drafting
  (quality), quick helper calls (fast) and image reading (vision).
- The AI key belongs to the operator, the person or company that runs the instance. Only the
  services process on the server holds it. Customers, the extension and the per-customer engines
  never see it (see [SECURITY-AND-PRIVACY.md](SECURITY-AND-PRIVACY.md)).
- Estimated cost is about **$0.0036 for a typical draft** and about **$0.011 for a heavy one**. Hard
  caps keep any single draft under about $0.028 on this model.
- Real web search: OpenAI's `web_search` tool on the Responses API, for two things: checking a
  public post's claim (at most once per press), and finding what is new in the customer's own beat
  (background topic research, and one press-time search when Create post or a "what should I post
  about?" question finds nothing stored). About a cent a search; see [Web search](#web-search).
- Every drafting prompt starts with the same rules and the same block about the writer (a
  model-written voice card and a fixed sample of their own writing), so OpenAI's automatic prompt
  caching serves it from the second press on; see [Prompt layout and caching](#prompt-layout-and-caching).
- The OpenAI path and web search have run only against a fake provider, so quality, latency and
  real cost on OpenAI have not yet been measured on a live account. See
  [Live checks still pending](#live-checks-still-pending).
- `AI_PROVIDER=anthropic` runs the same service on Anthropic's Claude Haiku 5.5 instead, with web
  search off. The author's free public beta in autumn 2026 ran this way. See
  [Anthropic](#anthropic-ai_provideranthropic).

## How Starling uses the model

One customer "press" (one draft request) runs a pipeline in that customer's engine. The engine
calls the services AI proxy, and the proxy calls OpenAI.

| Workload property | Value (from the code and `docs/architecture/CONTRACTS.md`) |
|---|---|
| Model calls per press | 1 for every drafting surface when the draft passes the integrity checks (✦ reply or quote, composer reply or quote, a created post, an enhance) and 1 for chat; a second draft call only when the first failed an integrity check or repeated an earlier reply; plus at most one web search (a claim check, or a topics search when nothing is stored for Create post or an ideas question) and, for a post with an image, one image read. Hard cap 12 (`AI_MAX_CALLS_PER_REQUEST`). Measured on the offline harness with capable answers, 2026-09-22. |
| Input per drafting call | about 1,600–2,900 tokens (measured; the writer block grows with the voice card and the account's writing), of which the shared prefix is cached from the second press; hard cap 8,000 per call (`AI_MAX_INPUT_TOKENS_PER_CALL`); the engine trims the post and the nearest examples, never the prefix, to stay under 7,600 (`STARLING_INPUT_TOKEN_BUDGET`) |
| Background calls | the voice card (one quality-lane call when the account has 8+ texts of its own, again after 20 more or weekly while active), topic research (paid plans only: at most one web search every 45 minutes while the account is active, at most 8 a day, paced by the service so the background share lasts the period) and the trend radar's refresh; all metered as the account's background work, never on a press |
| Output per call | sized per request from X's length limit and the fields asked for (about 200–320 tokens for a draft object); hard cap 600 (`AI_MAX_OUTPUT_TOKENS`), plus a reasoning allowance when a reasoning effort is sent |
| Typical press in total | about 1,700–3,500 input + 100–250 output tokens (measured on the harness); the cost tables below predate the 2026-09-22 pipeline changes and are an upper bound |
| Heavy press in total | about 40,000 input + 2,500 output tokens (the pre-2026-09-22 estimate, kept as an upper bound) |
| Time limits | the engine gives a draft call 20 seconds and a helper call 8 seconds, inside a 50-second budget per press; each upstream call also times out after `AI_TIMEOUT_S` (25 seconds) |
| Inputs | text, plus post images sent as data URIs to the vision lane |
| Outputs | streamed text and JSON (`json_object` or strict `json_schema` response formats) |

## Why `gpt-5.6-luna` is the default

| What Starling needs | What OpenAI's official pages say about `gpt-5.6-luna` |
|---|---|
| A low price per token, because each press makes several calls | $0.20 input, $0.02 cached input, $1.20 output per million tokens. That is the lowest of the options reviewed below. |
| Fast, predictable short answers | Reasoning effort can be set from `none` to `max`. Starling uses `none`. Reasoning tokens are billed as output, so `none` also keeps cost predictable. |
| Reading images in posts | image input supported |
| Machine-readable intermediate steps | structured outputs supported |
| Live typing effect in the extension | streaming supported |
| Enough context | 1,050,000-token context window and 128,000 maximum output. Starling uses at most 8,000 in and 600 out per call. |
| A stable target | Listed among current models with no preview or limited-access wording, and not on the deprecations page. Generally available models get at least 6 months' notice before shutdown. It is the named replacement for `gpt-5-nano`, which shuts down on 2026-12-11. |
| Enough throughput for a new account | usage Tier 1 allows 500 requests and 500,000 tokens per minute |
| Cheap repeated instructions | Prompt caching is automatic for prompts of 1,024 tokens or more. Starling also sends a hashed `prompt_cache_key` per customer and lane. |

Temperature: the official model page does not document temperature support for this model, and
Starling does not depend on it. The proxy forwards `temperature` and `top_p` only when its capability
table allows them. If OpenAI rejects a sampling parameter, the proxy retries once without it.

## Verified sources

All pages were checked on 2026-09-14. Prices, usage tiers, spend limits and data handling were
re-checked on 2026-09-18 and were unchanged; see
[Prices re-checked on 2026-09-18](#prices-re-checked-on-2026-09-18).

| Fact | Official source |
|---|---|
| Capabilities, endpoints, context, output limit, rate limits by tier | https://developers.openai.com/api/docs/models/gpt-5.6-luna |
| Listed among current models | https://developers.openai.com/api/docs/models/all |
| Prices for `gpt-5.6-luna` and `gpt-5.6-terra` | https://developers.openai.com/api/docs/pricing |
| Deprecation notice policy; replacement for `gpt-5-nano` | https://developers.openai.com/api/docs/deprecations |
| Reasoning tokens are billed as output, and the output token limit also covers reasoning tokens | https://developers.openai.com/api/docs/guides/reasoning |
| API data is not used for training by default; abuse monitoring logs are kept up to 30 days | https://developers.openai.com/api/docs/guides/your-data |
| Usage tiers and monthly usage limits | https://developers.openai.com/api/docs/guides/rate-limits |
| Hard spend limits and their 429 error codes | https://developers.openai.com/api/docs/guides/spend-limits and https://developers.openai.com/api/docs/guides/error-codes |
| Automatic prompt caching | https://developers.openai.com/api/docs/guides/prompt-caching |
| Separate staging and production projects; spend alerts; key expiry and rotation | https://developers.openai.com/api/docs/guides/production-best-practices |
| Web search tool: $10.00 per 1,000 calls plus search content tokens at model rates (checked 2026-09-22) | https://developers.openai.com/api/docs/pricing |
| `gpt-5.6-luna` supports `web_search` on the Responses endpoint; reasoning efforts `none` to `max` (default `medium`); knowledge cutoff 16 February 2026 (checked 2026-09-22) | https://developers.openai.com/api/docs/models/gpt-5.6-luna |
| Web search request (`tools`, `search_context_size`, `filters.allowed_domains`, `include: ["web_search_call.action.sources"]`), the `web_search_call` and `url_citation` answer shape, the warning that effort `none` may give lower-quality results, and the rule that citations shown to users must be visible and clickable (checked 2026-09-22) | OpenAI's web search tool guide on developers.openai.com (the guides section, "Web search") |

## Alternatives considered

| Option | Price per million tokens (input / output) | Why it is not the default |
|---|---|---|
| OpenAI `gpt-5.6-terra` | $2 / $12 (cached input $0.20) | Ten times luna's price. It is available as a quality upgrade; see the cost tables below and [PLANS-AND-USAGE.md](PLANS-AND-USAGE.md#if-you-switch-drafting-to-gpt-56-terra). |
| Anthropic Claude Sonnet 5 (`claude-sonnet-5`) | $2 / $10 | About 10× luna on input and 8× on output. When this table was written, Starling's proxy spoke only OpenAI Chat Completions, so any other provider needed proxy work; the Anthropic path was added later ([Anthropic](#anthropic-ai_provideranthropic)). |
| Anthropic Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) | $1 / $5 | 5× luna on input and about 4× on output. Its retirement is announced for no sooner than October 15, 2026, which is a short planning horizon. |
| Google Gemini 3.8 Flash (`gemini-3.8-flash`) | $0.75 / $3.75 until 2026-12-31, then $1.50 / $7.50 | Thinking levels are low, medium and high; there is no minimal level. Through Google's OpenAI compatibility layer, reasoning cannot be turned off for Gemini 3 models, which adds latency and output tokens to many short calls. |

Sources: Anthropic models overview https://platform.claude.com/docs/en/about-claude/models/overview ;
Gemini pricing https://ai.google.dev/gemini-api/docs/pricing ; Gemini latest model
https://ai.google.dev/gemini-api/docs/latest-model ; Gemini OpenAI compatibility
https://ai.google.dev/gemini-api/docs/openai . A third-party leaderboard
(https://artificialanalysis.ai/leaderboards/models) was consulted only as a secondary signal on
quality and speed. It is not an official source.

## Estimated cost per draft

These estimates assume no prompt caching. Cached input tokens make the real cost lower.

| Scenario | Tokens | `gpt-5.6-luna` | `gpt-5.6-terra` for drafting calls only | `gpt-5.6-terra` for every call |
|---|---|---|---|---|
| Typical press | about 13,000 in + 800 out | ≈ $0.0036 | ≈ $0.03 | ≈ $0.036 |
| Heavy press | about 40,000 in + 2,500 out | ≈ $0.011 | ≈ $0.09 (derived, see below) | ≈ $0.11 |
| Largest possible under the hard caps | 12 calls × (8,000 in + 600 out) | ≈ $0.028 | – | ≈ $0.28 |

The heavy-press figure for terra on drafting calls only is **derived, not measured**: it assumes the
drafting lane carries the same share of the tokens as in the typical row. Measure it on your own
account before you price against it.

How Starling records cost:

- Each call's cost comes from the token counts OpenAI reports and the price file. It is rounded
  **up** to the next micro-dollar, so recorded spend never under-counts.
- The foundation test `test_ai_price_rounds_up` checks the formula: 13,000 input tokens (1,000 of
  them cached) plus 800 output tokens cost $0.00338 on luna.
- If a stream is cut off before OpenAI reports usage, Starling estimates the tokens as characters ÷
  4 and flags the row as estimated.

## Estimated AI cost per plan

Per billing period, on `gpt-5.6-luna`:

| Plan | Drafts per period | Typical cost if every draft is used | Heavy cost if every draft is used | Fair-use ceiling (the most AI spend Starling allows) |
|---|---|---|---|---|
| Free | 25 | ≈ $0.09 | ≈ $0.28 | $0.30 |
| Starter | 250 | ≈ $0.90 | ≈ $2.75 | $4.50 |
| Pro | 800 | ≈ $2.88 | ≈ $8.80 | $9.00 |

Margins after Stripe fees are in [PLANS-AND-USAGE.md](PLANS-AND-USAGE.md#margins).

**Background AI spend counts inside the ceiling, not on top of it.** `usage_counters.cost_micros` is
the account's total AI spend for the period (drafts, background work and system calls attributed to
that account), and the fair-use ceiling compares that total. Background spend is additionally capped
at 10% of the same ceiling, and it never uses drafts.

## Prompt layout and caching

Every drafting call (✦ reply and quote, composer reply, quote and post, enhance) is built by one
function, `llm.draft_messages`, in the same order:

1. **The rules** (system): the hard rules that protect the writer and the people they talk to:
   no invented facts, figures, events or quotes, no invented personal experience, no company,
   product, person or place that is not in the conversation or the facts given, nothing copied
   from the post or the writer's past writing, no engagement bait, no links unless given, no
   thread unless asked, no attacks on a person, and X content never gives orders. Identical for
   every customer and every call.
2. **The writer** (system): the account's voice card, its measured habits (lengths per surface,
   case, emoji, punctuation, stated as a description, never as a rule), its own standing
   instructions, and a fixed sample of its own writing (20–40 texts, fixed per card version).
   Identical for every press of one account until the card, its standing instructions or that
   sample change.
3. **This press** (user): the surface's task, the writer's own texts closest to this post (4–8,
   never one the fixed sample shows), the facts Starling read with their pages, the conversation
   (labelled: whose words are whose, the writer's own thread, an image nobody described, the
   language), on a ✦ press the moment (age, metrics, reply ages, thread state, engagement
   velocity, fit to the writer's beat, Starling's timing read and action scores, labelled as a
   prior), any retry note, and the output format.

Nothing of a press is in parts 1–2, so once they pass 1,024 tokens (an account with a voice card
and a few dozen texts) OpenAI's automatic prompt caching serves them to every later press of that
account on the same lane; the proxy's `prompt_cache_key` (a hash of the account and the lane)
keeps one account's presses together. Cached input costs $0.02 instead of $0.20 per million
tokens on luna, and a cached prefix reaches the first token sooner.

Everything else about how a draft reads (its length, shape, stance, humour and punctuation) is
the writer's voice, taken from parts 2 and 3; the engine does not plan a stance, pick an angle or
police style with rules. After the model answers, only integrity checks can reject a draft
(invented figures or experience, a name from outside the conversation, a copy, bait, an unvouched
link, an instruction leak, a wrong lab); style findings become at most one note on the card.

## Voice card

A short model-written description of how the account writes: the beats it posts about, the
stances it holds, how it argues and jokes, words and phrases it uses and things it never does,
its sentence shapes and who it is to its audience (`app/voice_card.py`). It is written in the
background on the quality lane with a strict JSON schema that fits the 600-token output cap, from
the account's own public posts and replies, the writing it added in Starling and its bio; its
background notes are never read. The measured habits are computed, not asked for. Each card is
stored versioned in the account's engine database with the corpus it came from and the fixed
sample chosen with it; a new card is made when the corpus grows by 20 texts, or after a week if it
changed, while the account is active, and a failed attempt waits an hour. Until an account has 8
texts of its own there is no card: drafts use the few texts there are (or a plain cold-start line)
and the card says drafts are generic until Starling knows the writer's voice.

The engine also keeps an instant, deterministic read of the account's niche (`app/niche.py`):
keyphrases from its own writing, bio, onboarding pillars and background notes, recomputed the
moment writing is added, with no model call. It decides what topic research looks up and how close
a post is to the writer's beat from the very first press; the voice card's beats replace it once
the card exists.

## Web search

Starling uses OpenAI's own `web_search` tool for two jobs: checking a post's claim, and finding
what is new in the customer's own beat (topic research, below). Everything else it reads comes
from the post's own links and the shared news pool.

**When it runs.** Only inside a press on a reply or quote, and only when all of these hold: the
post is public (a protected post never leaves the engine); it makes a checkable, time-sensitive
claim (a release, a price, a figure, something that happened today, not an opinion, a joke or a
question); it links no readable page of its own; and the news pool has no story on it. The query
is that one sentence of the public post, nothing from the account's archive. The search starts at
once, beside the rest of the press, and the draft waits for it at most 7 seconds from the start
(`WEB_SEARCH_TIMEOUT_S`, default 8, bounds the service side). A late search is dropped and the
card says so.

**The request** (services `world/search.py`, through the same upstream client as a draft: key
order and fallback, retries, deadline): `POST {AI_BASE_URL}/responses` with
`tools: [{"type": "web_search", "search_context_size": "low"}]` (`WEB_SEARCH_CONTEXT_SIZE`),
`include: ["web_search_call.action.sources"]`, `store: false`, `reasoning: {"effort": "low"}`,
`max_tool_calls: 1`, `max_output_tokens` 400 plus the reasoning allowance, and the hashed
`safety_identifier`. OpenAI also offers `filters.allowed_domains`; Starling does not restrict
domains. If a model refuses `max_tool_calls` or `reasoning`, the service asks once without it and
remembers that for the process.

**The model** is `WEB_SEARCH_MODEL`, which defaults to `AI_MODEL_FAST` (`gpt-5.6-luna`). OpenAI's
model page lists `web_search` on the Responses endpoint for it, reasoning efforts `none`, `low`,
`medium` (the default), `high`, `xhigh` and `max`, a knowledge cutoff of 16 February 2026, and
$0.20 input / $1.20 output per million tokens. Starling uses `low`: OpenAI warns that `none` can
give lower-quality search results.

**The answer.** A `web_search_call` item (its `action.query` and `action.sources`), then a
`message` whose `output_text` carries `url_citation` annotations (`url`, `title`, `start_index`,
`end_index`). The service removes the inline citation markup from the text and keeps each cited
page with the sentence that cites it. OpenAI requires web results shown to people to carry
clearly visible, clickable inline citations, so the rule in the engine is **no fact without its
link**: the writer is given facts from at most three pages, and exactly those pages are listed on
the card as `decision.sources` (web search results first, then the post's own links, then
news-pool stories).

**Caps.** At most one search per press. At most `WEB_SEARCH_PER_ACCOUNT_DAY` (default 20) per
account per UTC day; 0 turns search off for everyone. A search never takes an account over its
plan's cost ceiling: it runs only while the period's spend plus the most one search can cost
(about $0.025 on luna: two tool calls, 12,000 input and 2,448 output tokens) stays under 95% of
the ceiling, and while searches have used less than 25% of that ceiling in the period. Owner
pause, the per-request call cap, the global day and month budgets and the free-plan trip apply
exactly as for a draft, because each attempt is admitted and recorded by the same metering.

**Cost.** OpenAI charges **$10.00 per 1,000 calls** for the tool, plus the search content tokens
at the model's own rates. On luna at context size `low` that is about **$0.011 a search** (in
the test stand-in: 3,200 input + 90 output tokens = $0.0007, plus $0.01). The tool fee is a `tools`
row in `backend/server/config/ai_pricing.json` with its own `effective_from`; each search is one
`ai_calls` row (lane `starling-search`) whose cost is the tokens at the model's row plus the fee
times the number of `web_search_call` items. With search on and no `web_search` row, the services
process refuses to start in production and refuses searches elsewhere.

| Plan | Fair-use ceiling | Searches a period at most (25% share, about $0.011 each) |
|---|---|---|
| Free | $0.30 | about 5 |
| Starter | $4.50 | about 100 (and never more than 20 a day) |
| Pro | $9.00 | about 200 (and never more than 20 a day) |

Real searches can read more pages than the stand-in, so treat about a cent as the floor; measure
it from `ai_calls` on a live account (see [Live checks still pending](#live-checks-still-pending)).

**Topic research** (`app/topic_research.py`, services `purpose: "topics"`). The query names the
account's beat only, built from its beat labels ("What is new in the last few days in LLM evals
and latency? ..."): never a post, a draft or a sentence of a note. The service answers with the
most notable new developments, each cited, and the engine keeps each development with its page
and the time it was found, and ranks the stored ones with the shared news pool's stories that
touch the beat: freshest first, then how many sources carry it, the fit to the beat, and the fit
to what performed for this account; a story the account just posted about is not offered again.
Create post hands the model the best three (at most three pages, so every fact on the card has
its link) and the model chooses one; with nothing stored, one press-time topics search runs inside
the press's own caps; with nothing found, the post is written from the writer's beat without
claiming any news, and the card says why: "nothing new" only when a search really ran and found
nothing; a refused search says which cap and when it opens again ("This period's news searches on
your plan are used up; they come back on October 3"), a late one that it did not come back in time.
A "what should I post about?" chat answers from the same stories.

A topics search is **shared**: its query is built only from beat labels, so the service keeps each
answer for 60 minutes (like `/v1/summary`) and serves it to every account that asks the same query,
at no cost and without a metered call. Claim checks are never shared.

It runs as **background work** on paid plans: after a press, while the account is active (a press
in the last day), at most every 45 minutes and at most 8 times a UTC day on the engine side, for
the stalest beat, sent with `X-Starling-Bucket: background`. The service refuses a background
search that is not topic research, refuses it on the Free plan (a Free period's background share
is about one search; Free gets its topics from the shared news pool and a press's own search), and
caps it on its own: at most `WEB_SEARCH_BACKGROUND_PER_ACCOUNT_DAY` (default 8) a UTC day and one at
a time per account, never past 95% of the plan's ceiling with the worst-case cost included, never
more than 60% of the account's background allowance (itself 10% of the ceiling) with a typical
search's cost included, inside the same 25% search share as press searches, and **paced to last the
period**: runs per day = the background search budget left / what one search costs / the days left
in the period (at most one search every period-seconds-left / searches-left). At about a cent a
search, Starter's allowance is about 25 background searches a period, so they come about every 29
hours of a 30-day period rather than eight a day for three days. A refusal carries its reason and
`retry_after_s`, and the engine's research waits exactly that long.

**Why a search was refused** (`/v1/search`, 429 `search_cap_exceeded`): the error body carries
`reason` ("per_press": this press already searched; "daily": the account's searches for today;
"plan_share": the plan's search share or ceiling for the period; "background": a background cap:
Free, the background share, the background daily cap or the pace) and `retry_after_s` (the next
UTC day, the period's end, the next paced slot, or null). A search that does not finish inside its
deadline is 504 `search_timeout`. The engine words each for the customer (`main.search_refusal_sentence`).

## Switching models

The services process maps Starling's three internal lanes to real model names through environment
settings:

| Setting | Default | Meaning |
|---|---|---|
| `AI_PROVIDER` | `openai` | `openai` or `anthropic` ([Anthropic](#anthropic-ai_provideranthropic)) |
| `AI_BASE_URL` | `https://api.openai.com/v1` | API base URL |
| `AI_MODEL_QUALITY` | `gpt-5.6-luna` | drafting lane (`starling-quality`) |
| `AI_MODEL_FAST` | `gpt-5.6-luna` | quick helper calls (`starling-fast`) |
| `AI_MODEL_VISION` | `gpt-5.6-luna` | image reading (`starling-vision`) |
| `AI_REASONING_EFFORT` | `none` | sent to OpenAI as `reasoning_effort` |
| `AI_PRICING_FILE` | `backend/server/config/ai_pricing.json` | per-model prices used for metering |
| `AI_CREDIT_MULTIPLIER` | `1` | Drafts charged for each metered press. It is tied to the configured quality model, and only whole numbers of 1 or more are accepted. |

Steps:

1. On OpenAI's page for the new model, confirm it supports Chat Completions, streaming, structured
   outputs, image input (if it will serve the vision lane) and the reasoning effort you plan to set.
2. Add a price entry to `backend/server/config/ai_pricing.json`. Never edit an old entry when a price
   changes. Add a new entry with a later `effective_from` date; Starling uses the newest entry whose
   date has arrived. Example (`gpt-5.6-terra` already has an entry):

   ```json
   {
     "model": "gpt-5.6-terra",
     "effective_from": "2026-09-14",
     "input_per_mtok": 2.00,
     "cached_input_per_mtok": 0.20,
     "output_per_mtok": 12.00,
     "source": "https://developers.openai.com/api/docs/pricing"
   }
   ```

3. Change the `AI_MODEL_*` settings. You can move only drafting (`AI_MODEL_QUALITY`) and keep the
   other lanes on luna.
4. If the new model costs more, raise `AI_CREDIT_MULTIPLIER`, the plan prices and ceilings, or
   both. [PLANS-AND-USAGE.md](PLANS-AND-USAGE.md#if-you-switch-drafting-to-gpt-56-terra) works
   through the numbers.
5. Restart the services process, then run the AI connection check:

   ```
   python -m starling_server.cli ai-check          # add --json for the full result
   ```

   It sends a minimal request to each configured model with the configured key. The same check is a
   button on the owner dashboard's AI page (`/admin/ai`), which stores its last result.
6. Try several drafts in staging before changing production.

Already enforced by the configuration code: if any lane's model has no entry in the pricing file,
the services configuration check reports `no price configured for AI model ...`. In production that
check stops the process from starting.

Other providers: Anthropic is supported with `AI_PROVIDER=anthropic` (next section). With
`AI_PROVIDER=openai`, `AI_BASE_URL` can point at another endpoint, but the proxy always sends
OpenAI-specific fields (`reasoning_effort`, `safety_identifier`, `prompt_cache_key`,
`stream_options.include_usage`), and **there is no setting that switches them off**. A provider that
rejects an unknown field will fail every call. Web search additionally calls OpenAI's Responses
endpoint with its `web_search` tool; with another provider set `WEB_SEARCH=false`. Treat any other
provider as a code change to the services proxy, not a configuration change.

## Anthropic (`AI_PROVIDER=anthropic`)

Added 2026-10-30. The author's free public beta in autumn 2026 ran on it: Claude Haiku 5.5
(`claude-haiku-5-5`) on all three lanes, with web search off. The beta has ended and no hosted
Starling service is running now.

`AI_PROVIDER=anthropic` makes the services process call Anthropic's Messages API through Anthropic's
official Python SDK (`anthropic` in `backend/requirements.in`). `AI_API_KEY` and
`AI_API_KEY_SECONDARY` are then Anthropic keys (`sk-ant-...`); rotation, the key-rejected alert and
the AI connection check work as described below. Nothing changes for the engines: they still send the
OpenAI-shaped requests of CONTRACTS 4.1 to the proxy, and the proxy translates in both directions
(`backend/server/starling_server/services/anthropic_upstream.py`).

What changes with the setting:

| Setting | Default with `anthropic` |
|---|---|
| `AI_BASE_URL` | `https://api.anthropic.com` (leave it unset; set it only to reach a test double) |
| `AI_MODEL_QUALITY`, `AI_MODEL_FAST`, `AI_MODEL_VISION` | `claude-haiku-5-5` |
| `WEB_SEARCH` | `false`, and it must stay `false` |
| `AI_SUBPROCESSORS` | `Anthropic` |

The configuration check also refuses, so in production the services process does not start with:
a model that is not a Claude model, `WEB_SEARCH=true`, an OpenAI `AI_BASE_URL`, an
`AI_REASONING_EFFORT` other than `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`, and
`AI_MAX_INPUT_TOKENS_PER_CALL` above 60,000 (see Price below).

How a request is translated:

- System messages become the system prompt. User and assistant messages become the conversation, with
  consecutive messages from the same side merged and empty text dropped. The conversation always
  starts and ends with a user message, because Claude Haiku 5.5 refuses a final assistant message.
- Images (data URIs) become image blocks.
- A `json_schema` response format becomes Anthropic's structured outputs. Limits Anthropic does not
  enforce, such as a maximum length, are moved into the field's description by the SDK's
  `transform_schema`, so the model is asked to keep them but is not forced to. The engine checks its
  drafts itself, as before. A `json_object` response format has no Anthropic equivalent: the proxy
  adds "reply with one JSON object" to the system prompt and trims the answer to the object.
- `AI_REASONING_EFFORT` becomes Anthropic's effort: `none` and `minimal` become `low`; the others
  keep their names. Claude Haiku 5.5 decides for itself how much to think (adaptive thinking, its
  default), even at `low`. Thinking is billed as output and counts against the output cap, so the
  proxy adds the effort's reasoning allowance (2,048 tokens at `low`) to the engine's cap. Only tokens
  actually used are billed. Thinking text never reaches the engine.
- The hashed per-account safety identifier becomes `metadata.user_id`. `temperature`, `top_p` (which
  Claude Haiku 5.5 refuses), `prompt_cache_key`, `n` and `stream_options` are dropped. No prompt
  caching is requested (on Anthropic it is opt-in).

How an answer is translated:

- Text becomes the OpenAI answer; a stream becomes OpenAI stream chunks, then a usage chunk and
  `[DONE]`, which is what the proxy meters and the engine reads.
- Usage: input tokens plus cache reads become `prompt_tokens`, cache reads become `cached_tokens`.
- `end_turn` becomes `finish_reason: "stop"`; `max_tokens` becomes `"length"` (a cut answer).
- A refusal (Claude's safety classifiers declined the request) becomes a 400 `bad_request`, which
  the engine treats as one failed step, not as a provider outage. In a stream it arrives as an error
  event. It is never retried, and it is metered with the tokens Anthropic billed.
- Errors keep their meaning: 401 and 403 are `ai_auth` (the secondary key is tried), 404 is
  `ai_model`, a billing error or "credit balance is too low" is `ai_quota`, 429 is
  `ai_rate_limited`, 500, 502, 503, 504 and 529 (overloaded) are `ai_unavailable`. Rate limits,
  server errors, timeouts and network errors are retried by the proxy as for OpenAI; the SDK's own
  retries are off so that every attempt is metered.

Web search is off. Starling's web search is OpenAI's `web_search` tool on the Responses API, and it
has no Anthropic version here. Without it the engine checks claims only against the post's own links
and the shared news pool, and topic ideas come from the news pool; the privacy and subprocessors
pages leave web search out while `WEB_SEARCH` is off. The news digest (`/v1/summary`) goes through the
same proxy and works on Claude.

Price (`backend/server/config/ai_pricing.json`, from Anthropic's published figures on 2026-10-30;
check https://www.anthropic.com/pricing before relying on them):

| Model | Input | Cache reads | Output |
|---|---|---|---|
| `claude-haiku-5-5` | $0.10 | $0.01 | $0.50 per million tokens |

These are the prices for a prompt of 100,000 tokens or fewer. A longer prompt costs $0.50 input and
$2.50 output per million, which the pricing file cannot express, so the configuration check keeps
`AI_MAX_INPUT_TOKENS_PER_CALL` at 60,000 or less (the default is 8,000). Claude's tokenizer counts
text differently from OpenAI's, and thinking adds output tokens, so the per-draft estimates above do
not carry over; at these prices a typical press costs well under a tenth of a cent. The public beta
ran on this model, but no measured cost per press is recorded here.

Differences to watch after the switch:

- Latency and cost from thinking at `low` effort. Measure them on real presses.
- Refusals are new: a request Claude declines fails that one step.
- Images: Anthropic has its own limits on image size and dimensions. An image the proxy accepts but
  Anthropic refuses fails that one step with `bad_request`.
- Rate limits are Anthropic's own, by usage tier. The service's daily and monthly budgets apply as
  before.
- [Data handling](#data-handling) below describes OpenAI's terms. Read Anthropic's commercial terms
  and data retention policy, and update the privacy notes, before launch.

To go back to OpenAI: set `AI_PROVIDER=openai` (or remove it), put an OpenAI key in `AI_API_KEY`,
remove the `claude-haiku-5-5` model settings, set `AI_SUBPROCESSORS=OpenAI` and, if wanted,
`WEB_SEARCH=true`.

After any switch, restart the services process and run `python -m starling_server.cli ai-check`.

## API keys and rotation

Set-up:

- Create the OpenAI organization under the operator's own business. OpenAI recommends separate
  projects for staging and production.
- Put the production key only in the production host's secret store, as `AI_API_KEY`. It never goes
  in a file in the repository.
- OpenAI recommends expiry dates and a regular rotation schedule for keys. Add a calendar reminder.

Planned rotation (no downtime):

1. Create a new key in the same OpenAI project.
2. Set `AI_API_KEY` to the new key and `AI_API_KEY_SECONDARY` to the current (old) key, then restart
   the services process.
3. Run the AI connection check. If the new key is rejected (401 or 403), the proxy retries once with
   the secondary key and raises an owner alert, so customers keep working while you fix it.
4. When no fallback alerts have appeared for a day, revoke the old key in OpenAI, clear
   `AI_API_KEY_SECONDARY` and restart.

Owner alerts appear in three places at once: the owner dashboard's overview page, the
`python -m starling_server.cli alerts` command, and an email to `ALERT_EMAIL` (at most one email per
alert key every 6 hours; no email at all while `ALERT_EMAIL` is empty). The key-rejected alert names
the slot that failed (primary or secondary), never any key material.

Emergency (key exposed): revoke the exposed key in OpenAI at once, set a new `AI_API_KEY`, and
restart. Drafts fail with an AI error until the new key is in place, unless a different valid key is
set as `AI_API_KEY_SECONDARY`.

## Spending safeguards

Starling layers several limits, from the smallest scope to the largest:

| Layer | Default | What happens when it is reached | Where it is set |
|---|---|---|---|
| Per-press caps | 12 calls; 8,000 input tokens per call; 600 output tokens per call | the extra call is refused (`call_cap_exceeded` or `input_too_large`) | `AI_MAX_CALLS_PER_REQUEST`, `AI_MAX_INPUT_TOKENS_PER_CALL`, `AI_MAX_OUTPUT_TOKENS` |
| Per-customer fair-use ceiling | Free $0.30, Starter $4.50, Pro $9.00 per period | that customer gets `fair_use_exceeded` until the next period | `cost_ceiling_usd` in `plans.toml` |
| Web search | one per press; 20 per account per UTC day; never past 95% of the ceiling with the worst-case search cost included; at most 25% of the ceiling per period | the search is skipped (`search_cap_exceeded` with `reason` and `retry_after_s`) and the draft is written without it; the card says which cap and when it opens again | `WEB_SEARCH`, `WEB_SEARCH_PER_ACCOUNT_DAY`; the shares are fixed rules in `world/search.py` |
| Background topic research | paid plans only; one at a time per account; 8 per account per UTC day; paced so the background search budget lasts the period; at most 60% of the background allowance; inside the 25% search share and the 95% ceiling rule; a topics answer is shared for 60 minutes | the research waits until the refusal's `retry_after_s`; drafts are unaffected | `WEB_SEARCH_BACKGROUND_PER_ACCOUNT_DAY` (default 8); the shares and the pace are fixed rules in `world/search.py` |
| Background AI | 10% of the plan ceiling | background work stops; drafts are unaffected | fixed rule |
| Global daily budget | $10 | new drafts pause for everyone (`service_paused` with `Retry-After`) until 00:00 UTC | `AI_DAILY_BUDGET_USD` |
| Global monthly budget | $90 | same, until the first of the next month (UTC) | `AI_MONTHLY_BUDGET_USD` |
| Free-plan trip point | 80 (%) | once the day's global PRESS spend (background work excluded, so a launch day of Free voice cards cannot trip it) reaches this percentage of the daily budget, **free-plan** accounts are refused with 503 `service_paused` while paid accounts keep drafting | `AI_FREE_TRIP_PCT` |
| OpenAI project hard spend limit | set by you | OpenAI returns 429 `project_spend_limit_exceeded`. Starling reports `ai_quota` (owner action needed) and never retries. | OpenAI dashboard |
| OpenAI usage tier monthly limit | Tier 1: $100 | OpenAI refuses further usage that month | OpenAI; the tier rises with payment history |

Keep this order: **Starling monthly budget ≤ OpenAI hard spend limit ≤ usage tier monthly limit.**
Then Starling pauses cleanly with its own "service paused" message before OpenAI starts refusing
calls.

The defaults ($10 a day, $90 a month) are chosen to sit under Tier 1's $100 monthly limit, so a new
account is safe out of the box. Set the OpenAI hard spend limit to something between them ($95, for
example) and raise all three together as the account's tier rises. Never raise Starling's budget
alone: that only moves the failure from a clean pause to an `ai_quota` error customers cannot act on.

A budget stop lasts to the end of its window. Raising a budget and restarting the services process
clears a stop whose window is now under the limit. A budget of zero allows no spend at all.

The OpenAI limit is set in the dashboard under **Organization limits → Spend → Edit spend limit**,
with **Enforce a hard limit** turned on; spend alerts alone do not cap anything, and enforcement is
not instantaneous, so recorded spend can slightly exceed the figure you set
(https://developers.openai.com/api/docs/guides/spend-limits, checked 2026-09-18).

### Capacity by OpenAI usage tier

Usage tiers and limits from https://developers.openai.com/api/docs/guides/rate-limits ; draft counts
are estimates on `gpt-5.6-luna`.

| Tier | Paid to OpenAI to qualify | Monthly usage limit | Typical drafts (≈ $0.0036) | Heavy drafts (≈ $0.011) |
|---|---|---|---|---|
| 1 | $5 | $100 | ≈ 27,700 | ≈ 9,090 |
| 2 | $50 | $500 | ≈ 138,800 | ≈ 45,400 |
| 3 | $100 | $1,000 | ≈ 277,700 | ≈ 90,900 |
| 4 | $250 | $5,000 | ≈ 1,388,800 | ≈ 454,500 |
| 5 | $1,000 | $200,000 | ≈ 55.5 million | ≈ 18.1 million |

What Tier 1's $100 covers:

| Customers | Tier 1 fits about |
|---|---|
| Starter customers who use every draft at typical cost ($0.90 each) | 111 |
| Pro customers who use every draft at typical cost ($2.88 each) | 34 |
| Starter customers at the fair-use ceiling ($4.50 each) | 22 |
| Pro customers at the fair-use ceiling ($9.00 each) | 11 |

With the default global budgets, $10 a day is about 2,770 typical (or 900 heavy) drafts, and $90 a
month is about 25,000 typical (or 8,180 heavy) drafts.

Throughput at Tier 1: 500,000 tokens per minute is enough for about 36 typical or 11 heavy presses
per minute. The 500 requests-per-minute limit is not the bottleneck (about 83 presses at 6 calls
each). These are estimates; OpenAI's own counting rules apply.

On the server side, `ENGINE_MAX_LIVE` (default 6 on a 4 GB machine) limits how many customer engines
run at once.

When a new customer needs an engine and every slot is taken, the pool first stops the least recently
used **idle** engine and starts theirs in its place; the customer waits a moment longer for a cold
start and notices nothing else. Only when all six engines are actively serving requests is the
customer refused, with 503 `ai_unavailable` and `Retry-After: 10`, and the extension shows "Starling
could not finish this draft. Please try again." There is no queue for drafting. Captures
(snapshots, outcomes, feedback) never start an engine at all: they are spooled to disk and answered
202, then replayed when the engine next starts.

## Data handling

What each AI call sends to OpenAI:

- **Prompt text** built by the customer's engine (`llm.draft_messages`, API-native layout of
  2026-09-22; see [Prompt layout and caching](#prompt-layout-and-caching)):
  - the rules (guidance in English, not customer text), and "return JSON only" for a JSON call;
  - the writer block: the account's voice card (a model-written summary of its writing), its
    measured habits (numbers derived from its writing, not the writing), its standing preferences
    and guardrails, and a fixed sample of 20–40 of its own posts and replies, a reply shown with
    the post it answered. Sent **as written: entities are not redacted**. The customer's own
    protected posts are never included; background notes never are;
  - the task for the surface, and the customer's own composer text when they asked Starling to
    improve it, or their own direction when they gave one;
  - the 4–8 of the customer's own texts closest to the post at hand;
  - the facts Starling read for this draft, from at most three pages (the post's own linked pages,
    news-pool stories, web search results, stories from the account's topic research; see
    [Web search](#web-search)), labelled untrusted;
  - the conversation, labelled as untrusted: the author's display name and handle, the post (up
    to 4,000 characters), a quoted or parent post (1,500 each), up to six thread posts of 400,
    links in the post, image alt text or Starling's image description, poll options, and notes
    (the writer's own thread, a reply to the writer's own post, an earlier reply of theirs in the
    thread, earlier drafts for the same post, a fact check Starling ran, the language);
  - on a ✦ press, the moment: the post's age, its visible metrics, how fresh the newest replies
    are, the thread's state, engagement velocity, the fit to the writer's beat, how often the
    customer has replied to this author, and Starling's own timing read and action scores,
    labelled as a prior; then any retry note and the output format.
- **Voice card calls** (background): the account's own public posts and replies, the writing it
  added in Starling, its bio and its measured habits. Never its background notes or protected posts.

  Page and post addresses **are** sent, which is why the extension's store listing declares web
  history; see [EXTENSION-PUBLISHING.md](EXTENSION-PUBLISHING.md#6-privacy-practices-answers).
- **Web search queries**: one sentence of a **public** post being answered, sent to OpenAI's
  `web_search` tool with `store: false`, only when that sentence makes a checkable, time-sensitive
  claim nothing else covers (see [Web search](#web-search)). Never a protected post, never text from
  the customer's own writing.
- **Topic research queries**: the account's beat labels only (short topic phrases from the voice
  card, or from the niche read of its own writing, bio, pillars and background notes: a
  lowercase topic phrase, never a sentence, a capitalised name, a number, a handle or a link),
  sent to the same tool with `store: false`.
- **Images** from the post, as data URIs, sent to the vision lane at low detail.
- **`safety_identifier`**: the first 32 hex characters of a SHA-256 hash of the customer's internal
  account id. No email or name is sent.
- **`prompt_cache_key`**: a similar hash of the account id plus the lane name.

What Starling keeps: one `ai_calls` row per call, with token counts, cost, status and latency. It
never stores prompt or output text. Server logs never contain prompts or drafts.

What OpenAI says it does: API data is not used to train models by default, and abuse monitoring
logs may be kept for up to 30 days (https://developers.openai.com/api/docs/guides/your-data). OpenAI
is listed as an AI subprocessor through `AI_SUBPROCESSORS=OpenAI`; see
[CONFIGURATION.md](CONFIGURATION.md#business-and-legal-details).

## Live checks still pending

What has run against a real provider so far:

- **Anthropic.** The author's free public beta in autumn 2026 ran on the Anthropic path, with
  Claude Haiku 5.5 and web search off. This document records no quality, latency or cost figures
  from it.
- **OpenAI.** The OpenAI path and web search have run only against a fake provider: the stand-in
  in the test suites and the offline harness that the measured figures in this document come from.

Nothing below has been run against a real OpenAI account. Each check needs the operator's own key.

- The model is reachable from the production host and accepts `reasoning_effort: "none"`.
- Draft quality on real x.com posts, with a person signed in to X.
- Real latency: time to first text and total time per press.
- Real cost per press from `ai_calls` records, including cached-token share and whether output
  stays within 600 tokens per call.
- Heavy-case cost with `gpt-5.6-terra`, if you plan to offer it. The figure in the cost table is
  derived from the typical press, not measured.
- Web search on a live key: that the Responses endpoint accepts the request as sent (including
  `max_tool_calls` and `reasoning`), real latency against the 7-second budget, the real input
  tokens a search adds (the reserve assumes at most 12,000 at context size `low`), how often the
  cited sources survive the relevance check, and the share of presses that search.
- Voice: whether drafts read like the customer with the voice card and the fixed sample (a blind
  test against the customer's real posts), and whether the card itself is right (compare it with
  the account's writing).
- Prompt caching: the real `cached_input_tokens` share per account from the second press on
  (`ai_calls`), and the first-token time with and without a cached prefix.
- Topic research: whether the stories found are new, in the customer's niche and worth a post, and
  how quickly a new development in the beat reaches Create post.

## Prices re-checked on 2026-09-18

The published prices for `gpt-5.6-luna` and `gpt-5.6-terra` were checked again against
https://developers.openai.com/api/docs/pricing on 2026-09-18 and still match
`backend/server/config/ai_pricing.json`.

One qualification worth knowing: $0.20 / $0.02 / $1.20 per million tokens is the **standard,
short-context** rate. The same page lists long-context at $0.40 / $0.04 / $1.80, and different rates
for batch, flex and fast modes. Starling sends standard, short-context requests (at most 8,000 input
tokens per call), so the standard rate is the one that applies, but do not quote it as the model's
only price.

The usage tier table was also re-checked on 2026-09-18 against
https://developers.openai.com/api/docs/guides/rate-limits and is unchanged. Note that Tier 1 is
reached with **$5 paid**, and its **$100 monthly usage limit** is a different number: $100 *paid* is
what reaches Tier 3.
