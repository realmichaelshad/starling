# News pool shape: `GET /v1/news/headlines`

Owner of this file: engine workstream. The services workstream builds the world service side
against it. It refines CONTRACTS.md §4.2 (the route and its purpose are defined there); if the two
ever disagree, CONTRACTS.md wins and this file is corrected.

The engine no longer contacts Hacker News, RSS publishers, OpenRouter or any other feed itself.
Everything it knows about "what happened today" comes from this one response.

## Request

```
GET {STARLING_SERVICES_URL}/v1/news/headlines?limit=150
Authorization: Bearer <services_token>
X-Starling-Request-Id: <reservation id>        (while serving a request)
X-Starling-Bucket: background                  (otherwise)
```

- `limit`: optional integer, 1 to 150, default 150. The engine always sends `150`.
- No other parameters. The engine never sends themes, handles, post text or anything derived from a
  customer's account. Personalisation happens inside the engine, after the response arrives.

## Response `200 application/json`

```json
{
  "generated_at": 1757800000,
  "items": [
    {
      "title": "Example Labs releases an open-weight coding model",
      "url": "https://example.com/blog/open-weight-coding-model",
      "source": "Example Labs Blog",
      "summary": "The model is available under an Apache 2.0 licence and scores 71% on the lab's own agentic coding benchmark. Weights and an inference guide are published alongside the post.",
      "published_at": 1757796400,
      "points": 0,
      "lane": null
    }
  ]
}
```

### Envelope

| Field | Type | Required | Meaning |
|---|---|---|---|
| `generated_at` | integer | yes | Unix seconds (UTC) when the shared pool was last refreshed |
| `items` | array of objects | yes | At most `limit` stories; may be empty |

### Item

| Field | Type | Required | Rules |
|---|---|---|---|
| `title` | string | yes | Plain text: HTML tags stripped, entities unescaped, whitespace collapsed. 1 to 300 characters. Items with an empty title are dropped by the engine. |
| `url` | string | yes | Absolute `http` or `https` URL of the story itself (for a discussion-only story, the discussion URL). Items without a URL are dropped. |
| `source` | string | yes | Display name of the authored feed, 1 to 80 characters, stable across refreshes (for example `Hacker News`, `OpenRouter`, `Example Labs Blog`). Used for the "Published 3 hours ago on {source}" line and for round-robin interleaving. |
| `summary` | string | yes, may be `""` | Plain text, at most 700 characters. The feed's own description or the submission text. Same cleaning as `title`. |
| `published_at` | integer | yes | Unix seconds, UTC, whole seconds (no milliseconds, no ISO strings). **`0` means unknown**: the feed sent no parseable date. |
| `points` | integer | no (default `0`) | Community score where the source has one (Hacker News points); otherwise `0`. |
| `lane` | string or null | no (default `null`) | Set only when the source itself establishes the lane from provenance. Today one value is meaningful: `"frontier models"` for model listings (for example OpenRouter's new models). |
| `cluster_size` | integer | no (default `1`) | How many independent sources carry the same story: items are grouped when their titles overlap (word Jaccard 0.5 or more) or share two names. At least 1. |
| `first_seen_at` | integer | no (default `0`) | Unix seconds when the pool first saw this URL; `0` when it has no memory of it. |
| `points_per_hour` | number | no (default `0`) | How fast the story's Hacker News points rose since the previous refresh; `0` where the source has no points. |
| `breaking` | boolean | no (default `false`) | Two or more sources within two hours, points rising at 60 an hour or more, or a new listing (under six hours old) from a lab the product watches. The engine ranks breaking stories right after "has a source". |

### `published_at` encoding, precisely

- RSS `<pubDate>` (RFC 822) and Atom `<published>` / `<updated>` (ISO 8601) are converted to an
  integer count of seconds since 1970-01-01T00:00:00Z. A date without a zone is taken as UTC.
- A missing or unparseable date is sent as `0`, never as `null`, a guess, or the fetch time.
- Engine tolerance (defensive, not an invitation): `null`, a missing field, a negative number, a
  non-finite number or a string are all read as `0`. A float is truncated to whole seconds.
- The engine never makes a time claim ("published today", "3 hours ago") for `0`.

### Ordering and de-duplication

The engine re-applies both rules on arrival, so they are recommendations rather than preconditions:

1. De-duplicate on the case-insensitive first 70 characters of `title`, keeping the first seen.
2. Interleave round-robin by `source` (sources in ascending name order), so one prolific source
   cannot fill the pool. This is the order the engine used when it fetched feeds itself.

The engine also re-applies its own summary cleaning (date furniture such as `Jun 29, 2026 ·`,
share/subscribe widget sentences, and first-person submitter sentences are removed). The service may
do the same; the operation is idempotent.

## Failure behaviour (engine side)

| Situation | Engine behaviour |
|---|---|
| Non-200 status, error body per CONTRACTS conventions | Serves its last cached pool (kept in the customer database for 15 minutes, stale copy kept indefinitely) and records a `news_refresh_empty` safety event. Never fails a draft. |
| Timeout | Same as above. The engine waits at most 8 seconds, shortened to the request's remaining time budget. |
| Malformed JSON, missing `items`, `items` not an array | Same as above. |
| Individual malformed item | That item is dropped; the rest are used. |

## Caching

- Services reads Hacker News and OpenRouter every 2.5 minutes and the RSS and Atom feeds every 5
  minutes, with conditional requests where the source supports them (CONTRACTS §4.2). A fast tick
  re-assembles the pool from fresh Hacker News and OpenRouter results and the feeds it did not
  re-read.
- The engine caches the response for 2.5 minutes per customer and will not re-request within 60
  seconds even when a draft asks for a forced refresh. Its post candidates last 5 minutes and are
  rebuilt whenever the pool changes, so they are never older than the pool.
- `STARLING_RESEARCH=off` disables the request entirely (the cached pool, if any, is still served).

---

## Other world-service responses the engine reads

CONTRACTS §4.2 defines these routes. Listed here are exactly the fields the engine uses, so the
services side knows what must be present. Same headers as above on every call.

### `POST /v1/fetch` `{"url": "<absolute http(s) URL>"}`

| Field | Type | Used for |
|---|---|---|
| `text` | string | Page text. Plain text with block boundaries as newlines. Fewer than 100 characters is treated as unreadable. The engine keeps at most 6,000 characters, cut at a line boundary. |
| `title` | string or null | Card and evidence titles. |
| `final_url` | string | Not read today. |
| `published_at` | integer | Not read today (same encoding as above if sent). |

Any non-200 response means "page unreadable"; the draft continues without it.

### `POST /v1/image` `{"url": "<absolute https URL>"}`

| Field | Type | Used for |
|---|---|---|
| `data_url` | string | Must start with `data:image/`. The engine re-checks the bytes and accepts only PNG, JPEG, WebP or GIF of at most 5 MB, then sends it to `starling-vision` as an `image_url` part with `detail: "low"`. |
| `mime` | string | Not read (the engine sniffs the bytes). |

### `POST /v1/summary` `{"query_id": "<authored id>"}`

The engine sends only these authored ids (from `backend/engine/app/research.py`,
`AUTHORED_QUERIES`); the text next to each is what the summary should cover:

| `query_id` | Topic |
|---|---|
| `ai-model-releases` | AI model release |
| `coding-agents` | coding agent |
| `llm-pricing` | LLM pricing |
| `ai-developer-tools` | developer tools AI |
| `open-weight-models` | open source model |

Response fields the engine reads (CONTRACTS does not define them yet; this is the engine's
expectation, to be confirmed by the services workstream):

| Field | Type | Rules |
|---|---|---|
| `answer` | string | At most five short sentences summarising recent, cited sources, with `[1]`-style citations. `summary` is accepted as an alias. Empty means "unavailable". |
| `sources` | array | Up to 6 objects `{"title": string, "url": string, "published_at": integer (0 or absent = unknown), "snippet": string}`. |
| `generated_at` | integer | Not read today. |

The engine caches a summary per customer for 45 minutes. An unknown id should return 400
`bad_request`; the engine treats any non-200 as "no web signal".
