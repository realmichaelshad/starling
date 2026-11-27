"""A local stand-in for the OpenAI Chat Completions API, served over real HTTP.

It answers exactly the subset `starling_server/services/upstream.py` sends (CONTRACTS 4.1):
`POST /v1/chat/completions`, streaming (SSE, ending with a usage chunk and `data: [DONE]`)
and non-streaming, and `POST /v1/responses` with the `web_search` tool (W4: one
`web_search_call` item, then a message whose `output_text` carries `url_citation`
annotations; its calls are logged with outcome "search"). Answers are deterministic: the
same prompt always produces the same text and the same token counts, so a test can multiply
them by `config/ai_pricing.json` (plus the tool's per-call fee for a search) and check
`ai_calls.cost_micros`.

The text is shaped for the caller. The engine asks the model for several different things and
parses each answer differently, so a single "Fake completion text." would make every draft
fail its own gates. Each rule below matches the instruction the engine actually sends and
answers in the shape that instruction asks for (`e2e/README.md` lists them). A `json_schema`
response_format is answered with exactly one object carrying the schema's own properties.

Like a real provider, the stand-in honours the output cap: an answer longer than
`max_completion_tokens` (or `max_tokens`) is cut at the cap and ends with
`finish_reason: "length"`, so a draft object that does not fit its budget fails here the way
it would fail live.

Prompt caching is modelled like OpenAI's automatic caching: per `prompt_cache_key`, a request
whose messages share an exact prefix of at least 1,024 tokens with an earlier request reports
that prefix (from 1,024, in 128-token steps) as `prompt_tokens_details.cached_tokens`.

SCRIPTED ANSWERS (`E2E_AI_SCRIPT`). When the environment variable names a JSON file holding a
list of rules, every call is checked against them first, in order:

    [{"match": ["substring", "..."],          # all must appear in the call's full prompt text
      "endpoint": "chat" | "responses",       # optional: only that endpoint
      "answer": "text" | {...}}]              # the assistant content (an object is sent as JSON)

The full prompt text is the messages in order, each as "<role>" on its own line and then its
content (for `/v1/responses`: the instructions as the system message, the input as the user
message). For `/v1/responses` an object answer may be `{"text": str, "sources": [{"url",
"title"}]}`: each source is cited by one sentence of the text (a web search answer). The first
matching rule wins; with no match (or no file) the built-in rules below answer. The file is
re-read when it changes.

Control plane (never part of the OpenAI API, always under `/_control`):

| Route | Purpose |
|---|---|
| `GET /_control/health` | readiness |
| `GET /_control/requests` | one entry per upstream call (with its full `messages`, `response_text`, `endpoint` and `scripted`), plus totals |
| `POST /_control/requests/reset` | forget the call log |
| `POST /_control/fail` | queue a failure: `invalid_key`, `rate_limited`, `server_error`, `quota`, `slow`, `cut_stream` |
| `POST /_control/reset` | forget failures and the call log |
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

CHARS_PER_TOKEN = 4
SENTINEL = "@@META@@"  # engine llm.STREAM_SENTINEL: draft text, then metadata JSON
MAX_STREAM_CHUNK = 24


# --------------------------------------------------------------------------- deterministic text


def _seed(text: str) -> random.Random:
    return random.Random(int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16))


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]{3,}")
_COMMON = frozenset(
    """about above after again against because before being below between both cannot could
    does doing down during eachffew from further have having here hers herself himself into
    itself just more most only other ours ourselves over same should some such than that their
    theirs them themselves then there these they this those through under until very were what
    when where which while with would your yours yourself yourselves write writer draft post
    posts text json only return line sentence write written reply replies""".split()
)


def salient_words(text: str, limit: int = 6) -> list[str]:
    """The most distinctive words of a prompt, so an answer is visibly about its own input."""
    counts: dict[str, int] = {}
    for match in _WORD_RE.finditer(text):
        word = match.group(0).lower()
        if word in _COMMON or word.isdigit():
            continue
        counts[word] = counts.get(word, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ordered[:limit]]


def _subject(prompt: str) -> str:
    words = salient_words(prompt, 3)
    return " ".join(words[:2]) if words else "the release"


def first_sentence_of(block: str) -> str:
    for piece in re.split(r"(?<=[.!?])\s+", block.strip()):
        cleaned = " ".join(piece.split())
        if len(cleaned.split()) >= 5:
            return cleaned
    return " ".join(block.split())[:180]


def _section(prompt: str, *names: str) -> str:
    """The text under the first of `names` that appears in the prompt (an evidence block)."""
    for name in names:
        index = prompt.find(name)
        if index < 0:
            continue
        rest = prompt[index + len(name):]
        stop = rest.find("\n\n")
        return rest[:stop] if stop > 0 else rest[:1200]
    return ""


def user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _flatten(message.get("content"))
    return ""


def system_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(_flatten(m.get("content")) for m in messages if m.get("role") == "system")


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


# ---- the answers themselves


def _draft_text(prompt: str, rng: random.Random) -> str:
    subject = _subject(prompt)
    openers = [
        f"Everyone keeps arguing about {subject}, and almost nobody has shipped with it yet.",
        f"Spent the afternoon on {subject}. The interesting part is not the part people quote.",
        f"{subject.capitalize()} is the kind of thing that looks small until you run it for a week.",
    ]
    middles = [
        "The first version was wrong in a useful way, so the second one took an hour.",
        "It took one real workload before the trade-off showed up.",
        "The docs answer the easy half. The other half is a Tuesday afternoon.",
    ]
    closers = [
        "Worth an hour if you already have the problem.",
        "Not a revolution. Just less work than last month.",
        "Try it on something you already understand.",
    ]
    return " ".join([rng.choice(openers), rng.choice(middles), rng.choice(closers)])


def _chat_answer(prompt: str, rng: random.Random) -> str:
    subject = _subject(prompt)
    lines = [
        f"Post about {subject} today.",
        "Replies before posts: pick three accounts you actually read and answer their newest post.",
        "Keep it to one idea and one concrete number.",
    ]
    rng.shuffle(lines)
    return " ".join(lines)


def _candidates_json(prompt: str, rng: random.Random) -> str:
    """The legacy `json_object` draft shape, with ONE candidate like a real model asked for one."""
    local = _seed(f"{prompt}#0")
    candidate = {
        "text": _draft_text(prompt + "0", local),
        "rationale": f"Leads with the concrete part of {_subject(prompt)} instead of the claim.",
    }
    del rng
    return json.dumps({"candidates": [candidate]}, ensure_ascii=False)


def _schema_of(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    """The JSON schema a `json_schema` response_format asks for, or None."""
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return None
    spec = response_format.get("json_schema")
    schema = spec.get("schema") if isinstance(spec, dict) else None
    return schema if isinstance(schema, dict) else None


def _schema_answer(schema: dict[str, Any], prompt: str, rng: random.Random) -> str:
    """One object that fills exactly the properties the schema names, in their order.

    A strict schema is how the engine asks for ONE draft (`llm.draft_response_format`);
    answering with anything else would be a model ignoring its instructions, which the
    engine has its own tests for. Unknown string fields get a short sentence, enums their
    first value, numbers 0.8, booleans false.
    """
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    answer: dict[str, Any] = {}
    text = _draft_text(prompt, rng)
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("type")
        if isinstance(spec.get("enum"), list) and spec["enum"]:
            # The decision schema's verdict is ["act", "wait"]: the stand-in always acts.
            answer[name] = spec["enum"][0]
        elif kind in ("number", "integer"):
            answer[name] = 0.8
        elif kind == "array":
            # A list of short strings (the voice card's beats, stances, words): the prompt's own
            # most distinctive words, so a card is visibly about the writing it was given.
            words = salient_words(prompt, 9) or ["writing", "posts", "replies"]
            answer[name] = [" ".join(words[index:index + 2]) for index in range(0, min(len(words), 6), 2)]
        elif kind == "boolean":
            answer[name] = False
        elif name == "text":
            answer[name] = text
        elif name == "point":
            # The point is the draft's own first sentence, as a model that plans
            # and then writes would give it (the engine checks the text serves it).
            answer[name] = first_sentence_of(text)
        elif name == "why":
            answer[name] = f"There is a concrete point to add about {_subject(prompt)}."
        elif name == "rationale":
            answer[name] = f"Leads with the concrete part of {_subject(prompt)} instead of the claim."
        else:
            answer[name] = _one_sentence(prompt, rng)
    return json.dumps(answer, ensure_ascii=False)


def _stream_draft(prompt: str, rng: random.Random) -> str:
    meta = {"rationale": f"Keeps the writer's point about {_subject(prompt)} and cuts the throat-clearing."}
    text = _draft_text(prompt, rng)
    # A reply is asked for its point on a first line (engine llm._point_line).
    point = f"POINT: {first_sentence_of(text)}\n" if "line starting POINT:" in prompt else ""
    return f"{point}{text}\n{SENTINEL}\n{json.dumps(meta, ensure_ascii=False)}"


def _one_sentence(prompt: str, rng: random.Random) -> str:
    words = salient_words(prompt, 4) or ["the", "release"]
    shapes = [
        "{a} changes what {b} costs in practice, and that is the whole point.",
        "The draft argues that {a} matters more than the usual {b} advice.",
        "{a} is worth a look once {b} is already a problem you have.",
    ]
    filler = words[1] if len(words) > 1 else words[0]
    return rng.choice(shapes).format(a=words[0], b=filler).capitalize()


def _digest(prompt: str, rng: random.Random) -> str:
    del rng
    return f"{_subject(prompt).capitalize()} shipped a change this week [1]. Early users report a smaller build [2]."


def answer_for(messages: list[dict[str, Any]], response_format: dict[str, Any] | None) -> str:
    """The deterministic answer for one call, chosen by the instruction the engine sent."""
    system = system_text(messages)
    prompt = user_text(messages)
    rng = _seed(system + "\x00" + prompt)
    wants_json = bool(response_format) and str(response_format.get("type", "")).startswith("json")

    if "You verify quotes" in system:
        evidence = _section(prompt, "Evidence:", "EVIDENCE:", "evidence:")
        quote = first_sentence_of(evidence) if evidence.strip() else "NONE"
        if "Claims:" in prompt:
            # Several claims in one call (engine support_check S6): one numbered line each.
            count = len(re.findall(r'^\d+\. "', _section(prompt, "Claims:"), flags=re.MULTILINE)) or 1
            return "\n".join(f"{index}: {quote}" for index in range(1, count + 1))
        return quote
    if "Reply with OK or BAD" in system:
        return "OK"
    if "Return only the single sentence" in system:
        return _one_sentence(prompt, rng)
    if "short news digest" in system:
        return _digest(prompt, rng)
    if "You are Starling" in system:
        return _chat_answer(prompt, rng)
    if SENTINEL in prompt or "Output format: first write ONLY the finished draft" in prompt:
        return _stream_draft(prompt, rng)
    schema = _schema_of(response_format)
    if schema is not None:
        return _schema_answer(schema, prompt, rng)
    if wants_json or "Return valid JSON only" in system:
        if "candidates" in prompt or "draft" in prompt.lower():
            return _candidates_json(prompt, rng)
        return json.dumps({"ok": True, "items": [], "note": _one_sentence(prompt, rng)}, ensure_ascii=False)
    return _draft_text(prompt, rng)


def token_estimate(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


# ---- prompt caching (OpenAI's automatic caching, modelled)

CACHE_MIN_TOKENS = 1_024
CACHE_STEP_TOKENS = 128
CACHE_HISTORY = 64


def serialise_messages(messages: list[dict[str, Any]]) -> str:
    """The messages in order, the way a prefix is compared: role, then content."""
    return "".join(f"<{m.get('role')}>\n{_flatten(m.get('content'))}\n" for m in messages
                   if isinstance(m, dict))


def _common_prefix(a: str, b: str) -> int:
    low, high = 0, min(len(a), len(b))
    while low < high:
        middle = (low + high + 1) // 2
        if a[:middle] == b[:middle]:
            low = middle
        else:
            high = middle - 1
    return low


def cached_tokens_for(history: list[str], serialised: str) -> int:
    """Tokens a real provider would serve from cache for this prompt, given earlier ones."""
    shared = max((_common_prefix(serialised, earlier) for earlier in history), default=0)
    tokens = shared // CHARS_PER_TOKEN
    if tokens < CACHE_MIN_TOKENS or token_estimate(serialised) < CACHE_MIN_TOKENS:
        return 0
    return CACHE_MIN_TOKENS + ((tokens - CACHE_MIN_TOKENS) // CACHE_STEP_TOKENS) * CACHE_STEP_TOKENS


# ---- scripted answers (E2E_AI_SCRIPT)

_SCRIPT_CACHE: dict[str, Any] = {"path": None, "mtime": None, "rules": []}


def script_rules() -> list[dict[str, Any]]:
    """The rules in the file `E2E_AI_SCRIPT` names, re-read when the file changes."""
    import os
    path = os.environ.get("E2E_AI_SCRIPT", "").strip()
    if not path:
        return []
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if _SCRIPT_CACHE["path"] != path or _SCRIPT_CACHE["mtime"] != mtime:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = []
        rules = ([rule for rule in data if isinstance(rule, dict) and isinstance(rule.get("match"), list)]
                 if isinstance(data, list) else [])
        _SCRIPT_CACHE.update(path=path, mtime=mtime, rules=rules)
    return list(_SCRIPT_CACHE["rules"])


def scripted_answer(prompt_text: str, endpoint: str) -> Any | None:
    """The answer of the first rule whose every `match` substring is in the prompt, or None."""
    for rule in script_rules():
        wanted = rule.get("endpoint")
        if wanted and wanted != endpoint:
            continue
        if all(str(item) in prompt_text for item in rule["match"]):
            return rule.get("answer")
    return None


def as_content(answer: Any) -> str:
    return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


# ---- web search (the Responses endpoint's `web_search` tool, W4)

# What one search adds as input tokens (the pages it read), at search_context_size "low".
SEARCH_CONTENT_TOKENS = 1_500


def search_claim(body: dict[str, Any]) -> str:
    """The claim a web search was asked to check (services/world/search.py sends it as `input`)."""
    value = body.get("input")
    if isinstance(value, list):
        value = " ".join(_flatten(item.get("content")) for item in value if isinstance(item, dict))
    text = " ".join(str(value or "").split())
    return text.split(":", 1)[1].strip() if text.lower().startswith("claim to check") else text


def web_search_output(claim: str, *, topics: bool = False,
                      scripted: Any = None) -> tuple[list[dict[str, Any]], str]:
    """A web search answer in the Responses shape, the same every time for the same claim.

    One `web_search_call` item (its query is the claim's salient words, its sources the two
    pages below), then one `message` whose `output_text` says what each page reports, every
    sentence followed by an inline citation, `([host](url))`, whose `url_citation` annotation
    spans exactly that markup, as the real tool does.
    """
    words = salient_words(claim, 4) or ["claim", "check"]
    slug = "-".join(words[:3])
    if isinstance(scripted, dict):
        # A scripted search answer (E2E_AI_SCRIPT): its text, one sentence citing each source.
        sources = [item for item in scripted.get("sources") or [] if isinstance(item, dict) and item.get("url")]
        sentences = [piece for piece in re.split(r"(?<=[.!?])\s+", str(scripted.get("text") or "").strip())
                     if piece] or [claim]
        if not sources:
            text = " ".join(sentences)
            return ([{"type": "message", "id": "msg_e2e", "status": "completed", "role": "assistant",
                      "content": [{"type": "output_text", "text": text, "annotations": []}]}], text)
        pages = [(sentences[min(index, len(sentences) - 1)], str(source["url"]),
                  str(source.get("title") or source["url"])) for index, source in enumerate(sources)]
    elif topics:
        # "What is new in this beat" (background topic research): two developments, two outlets.
        pages = [
            (f"Example News reports a new {' '.join(words[:2])} release this week, with the figures behind it.",
             f"https://news.example.com/{slug}-release", f"{' '.join(words[:2]).title()} release - Example News"),
            (f"Example Daily covers a debate about {' '.join(words[1:3]) or words[0]} that started this week.",
             f"https://daily.example.com/{slug}-debate", f"{' '.join(words[1:3]).title()} debate - Example Daily"),
        ]
    else:
        pages = [
            (f"Example News reports that {claim.rstrip('.!?')}.",
             f"https://news.example.com/{slug}", f"{' '.join(words[:3]).title()} - Example News"),
            (f"The {' '.join(words[:2])} announcement gives the date and the figures behind it.",
             f"https://example.com/{slug}/announcement", f"{' '.join(words[:2]).title()} announcement"),
        ]
    text = ""
    annotations: list[dict[str, Any]] = []
    for sentence, url, title in pages:
        text += ("" if not text else " ") + sentence + " "
        marker = f"([{url.split('//', 1)[1].split('/', 1)[0]}]({url}))"
        annotations.append({"type": "url_citation", "start_index": len(text), "end_index": len(text) + len(marker),
                            "url": url, "title": title})
        text += marker
    output = [
        {"type": "web_search_call", "id": "ws_e2e", "status": "completed",
         "action": {"type": "search", "query": " ".join(words), "sources": [{"type": "url", "url": url}
                                                                              for _, url, _ in pages]}},
        {"type": "message", "id": "msg_e2e", "status": "completed", "role": "assistant",
         "content": [{"type": "output_text", "text": text, "annotations": annotations}]},
    ]
    return output, text


def output_limit(body: dict[str, Any]) -> int | None:
    """The output cap the request carries (`max_completion_tokens`, else `max_tokens`)."""
    for name in ("max_completion_tokens", "max_tokens"):
        value = body.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def cut_to_limit(answer: str, limit: int | None) -> tuple[str, str]:
    """A real provider stops at the cap: the text is cut and `finish_reason` says "length".

    The stand-in used to ignore the cap, so no journey could ever see an answer cut
    mid-JSON, which is exactly what a live model does to a draft object that does not fit.
    """
    if limit is None or token_estimate(answer) <= limit:
        return answer, "stop"
    return answer[: limit * CHARS_PER_TOKEN], "length"


def chunk_text(text: str, rng: random.Random) -> list[str]:
    """Split into stable, word-aligned pieces so a stream looks like a stream."""
    pieces: list[str] = []
    current = ""
    for word in re.split(r"(\s+)", text):
        current += word
        if len(current) >= rng.randint(8, MAX_STREAM_CHUNK) and not word.isspace():
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces or [text]


# --------------------------------------------------------------------------- failure injection


FAILURE_KINDS = ("invalid_key", "rate_limited", "server_error", "quota", "slow", "cut_stream", "empty_usage")


@dataclass
class Failure:
    kind: str
    times: int = 1
    delay_s: float = 0.0
    after_chunks: int = 2
    retry_after_s: int | None = None

    def consume(self) -> None:
        self.times -= 1


@dataclass
class Call:
    n: int
    at: float
    model: str
    stream: bool
    outcome: str
    input_chars: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    safety_identifier: str | None
    prompt_cache_key: str | None
    max_completion_tokens: int | None
    reasoning_effort: str | None
    response_format: str | None
    key_fingerprint: str
    answer_chars: int
    fields: list[str] = field(default_factory=list)
    # The whole request and answer, so an evaluation report can show exactly what the model
    # received for each press: `messages` for chat; for responses, the instructions as a
    # system message and the input as a user message.
    messages: list[dict[str, Any]] = field(default_factory=list)
    response_text: str = ""
    endpoint: str = "chat"
    scripted: bool = False


class FakeOpenAI:
    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key
        self.calls: list[Call] = []
        self.failures: list[Failure] = []
        self._lock = asyncio.Lock()
        self._seq = 0
        # Earlier prompts per prompt_cache_key, for the cached-token model.
        self.prefixes: dict[str, list[str]] = {}

    # ---- control

    def reset(self) -> None:
        self.calls.clear()
        self.failures.clear()
        self.prefixes.clear()

    def cached_tokens(self, key: Any, messages: list[dict[str, Any]]) -> int:
        serialised = serialise_messages(messages)
        history = self.prefixes.setdefault(str(key or ""), [])
        cached = cached_tokens_for(history, serialised)
        history.append(serialised)
        if len(history) > CACHE_HISTORY:
            self.prefixes[str(key or "")] = history[-CACHE_HISTORY:]
        return cached

    def queue(self, failure: Failure) -> None:
        self.failures.append(failure)

    def take_failure(self) -> Failure | None:
        for failure in list(self.failures):
            if failure.times <= 0:
                self.failures.remove(failure)
                continue
            failure.consume()
            if failure.times <= 0:
                self.failures.remove(failure)
            return failure
        return None

    def totals(self) -> dict[str, Any]:
        done = [call for call in self.calls if call.outcome in ("json", "stream")]
        return {
            "calls": len(self.calls),
            "completed": len(done),
            "streamed": sum(1 for call in done if call.stream),
            "prompt_tokens": sum(call.prompt_tokens for call in done),
            "cached_tokens": sum(call.cached_tokens for call in done),
            "completion_tokens": sum(call.completion_tokens for call in done),
            "searches": sum(1 for call in self.calls if call.outcome == "search"),
            "by_outcome": {kind: sum(1 for call in self.calls if call.outcome == kind) for kind in
                           sorted({call.outcome for call in self.calls})},
            "identities": sorted({call.safety_identifier for call in self.calls if call.safety_identifier}),
        }


def _error(status: int, code: str, message: str, kind: str, headers: dict[str, str] | None = None) -> JSONResponse:
    body = {"error": {"message": message, "type": kind, "param": None, "code": code}}
    return JSONResponse(body, status_code=status, headers=headers or {})


def _fingerprint(header: str) -> str:
    token = header[len("Bearer "):] if header.startswith("Bearer ") else header
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8] if token else ""


def create_app(api_key: str) -> FastAPI:
    fake = FakeOpenAI(api_key=api_key)
    app = FastAPI(title="fake OpenAI", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.fake = fake

    @app.get("/_control/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "calls": len(fake.calls)}

    @app.get("/_control/requests")
    async def requests() -> dict[str, Any]:
        return {"totals": fake.totals(), "calls": [call.__dict__ for call in fake.calls]}

    @app.post("/_control/requests/reset")
    async def reset_requests() -> dict[str, Any]:
        fake.calls.clear()
        return {"ok": True}

    @app.post("/_control/reset")
    async def reset() -> dict[str, Any]:
        fake.reset()
        return {"ok": True}

    @app.get("/_control/failures")
    async def failures() -> dict[str, Any]:
        return {"failures": [failure.__dict__ for failure in fake.failures]}

    @app.post("/_control/fail")
    async def fail(payload: dict[str, Any]) -> JSONResponse:
        kind = str(payload.get("kind", ""))
        if kind not in FAILURE_KINDS:
            return JSONResponse({"error": f"kind must be one of {', '.join(FAILURE_KINDS)}"}, status_code=400)
        failure = Failure(
            kind=kind,
            times=max(1, int(payload.get("times", 1))),
            delay_s=float(payload.get("delay_s", 0.0)),
            after_chunks=max(0, int(payload.get("after_chunks", 2))),
            retry_after_s=payload.get("retry_after_s"),
        )
        fake.queue(failure)
        return JSONResponse({"ok": True, "queued": failure.__dict__})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "invalid_request_error", "the body is not JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return _error(400, "invalid_request_error", "the body is not an object", "invalid_request_error")
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        stream = bool(body.get("stream"))
        authorization = request.headers.get("authorization", "")
        async with fake._lock:
            fake._seq += 1
            index = fake._seq
            failure = fake.take_failure()

        def record(outcome: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                   cached: int = 0, answer: str = "", scripted: bool = False) -> Call:
            call = Call(
                n=index,
                at=round(time.time(), 3),
                model=str(body.get("model", "")),
                stream=stream,
                outcome=outcome,
                input_chars=sum(len(_flatten(m.get("content"))) for m in messages),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached,
                safety_identifier=body.get("safety_identifier"),
                prompt_cache_key=body.get("prompt_cache_key"),
                max_completion_tokens=body.get("max_completion_tokens"),
                reasoning_effort=body.get("reasoning_effort"),
                response_format=(body.get("response_format") or {}).get("type") if isinstance(body.get("response_format"), dict) else None,
                key_fingerprint=_fingerprint(authorization),
                answer_chars=len(answer),
                fields=sorted(body),
                messages=[{"role": m.get("role"), "content": _flatten(m.get("content"))}
                          for m in messages if isinstance(m, dict)],
                response_text=answer,
                endpoint="chat",
                scripted=scripted,
            )
            fake.calls.append(call)
            return call

        if authorization != f"Bearer {fake.api_key}" or (failure is not None and failure.kind == "invalid_key"):
            record("invalid_key")
            return _error(401, "invalid_api_key", "Incorrect API key provided.", "invalid_request_error")
        if failure is not None and failure.kind == "rate_limited":
            record("rate_limited")
            headers = {"retry-after": str(failure.retry_after_s or 1)}
            return _error(429, "rate_limit_exceeded", "Rate limit reached for requests.", "requests", headers)
        if failure is not None and failure.kind == "quota":
            record("quota")
            return _error(429, "insufficient_quota", "You exceeded your current quota.", "insufficient_quota")
        if failure is not None and failure.kind == "server_error":
            record("server_error")
            return _error(500, None, "The server had an error while processing your request.", "server_error")

        response_format = body.get("response_format") if isinstance(body.get("response_format"), dict) else None
        script = scripted_answer(serialise_messages(messages), "chat")
        chosen = as_content(script) if script is not None else answer_for(messages, response_format)
        answer, finish_reason = cut_to_limit(chosen, output_limit(body))
        prompt_tokens = sum(token_estimate(_flatten(m.get("content")) or " ") for m in messages)
        completion_tokens = token_estimate(answer)
        cached = fake.cached_tokens(body.get("prompt_cache_key"), messages)
        with_usage = not (failure is not None and failure.kind == "empty_usage")
        delay = failure.delay_s if failure is not None and failure.kind == "slow" else 0.0
        cut_after = failure.after_chunks if failure is not None and failure.kind == "cut_stream" else None

        if not stream:
            if delay:
                await asyncio.sleep(delay)
            record("json", prompt_tokens, completion_tokens, cached, answer, script is not None)
            payload: dict[str, Any] = {
                "id": f"chatcmpl-e2e-{index:06d}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": finish_reason}],
            }
            if with_usage:
                payload["usage"] = _usage(prompt_tokens, completion_tokens, cached)
            return JSONResponse(payload)

        call = record("stream", prompt_tokens, completion_tokens, cached, answer, script is not None)
        pieces = chunk_text(answer, _seed(answer))

        async def body_stream() -> Iterable[bytes]:
            if delay:
                await asyncio.sleep(delay)
            for position, piece in enumerate(pieces):
                if cut_after is not None and position >= cut_after:
                    call.outcome = "cut_stream"
                    raise RuntimeError("fake OpenAI cut the stream")
                yield _sse(
                    {
                        "id": f"chatcmpl-e2e-{index:06d}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": body.get("model"),
                        "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                    }
                )
                await asyncio.sleep(0)
            yield _sse(
                {
                    "id": f"chatcmpl-e2e-{index:06d}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": body.get("model"),
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                }
            )
            if with_usage:
                yield _sse(
                    {
                        "id": f"chatcmpl-e2e-{index:06d}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": body.get("model"),
                        "choices": [],
                        "usage": _usage(prompt_tokens, completion_tokens, cached),
                    }
                )
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            body_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/responses")
    async def responses(request: Request) -> Any:
        """The Responses endpoint, answering a `web_search` tool request (W4) like the real one."""
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "invalid_request_error", "the body is not JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return _error(400, "invalid_request_error", "the body is not an object", "invalid_request_error")
        authorization = request.headers.get("authorization", "")
        claim = search_claim(body)
        instructions = str(body.get("instructions") or "")
        async with fake._lock:
            fake._seq += 1
            index = fake._seq
            failure = fake.take_failure()

        raw_input = body.get("input")
        if isinstance(raw_input, list):
            raw_input = " ".join(_flatten(item.get("content")) for item in raw_input if isinstance(item, dict))
        call_messages = [{"role": "system", "content": instructions}, {"role": "user", "content": str(raw_input or "")}]

        def record(outcome: str, prompt_tokens: int = 0, completion_tokens: int = 0, answer: str = "",
                   scripted: bool = False) -> Call:
            reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
            call = Call(
                n=index, at=round(time.time(), 3), model=str(body.get("model", "")), stream=False,
                outcome=outcome, input_chars=len(instructions) + len(claim), prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, cached_tokens=0,
                safety_identifier=body.get("safety_identifier"), prompt_cache_key=None,
                max_completion_tokens=body.get("max_output_tokens"), reasoning_effort=reasoning.get("effort"),
                response_format=None, key_fingerprint=_fingerprint(authorization), answer_chars=len(answer),
                fields=sorted(body), messages=call_messages, response_text=answer, endpoint="responses",
                scripted=scripted,
            )
            fake.calls.append(call)
            return call

        if authorization != f"Bearer {fake.api_key}" or (failure is not None and failure.kind == "invalid_key"):
            record("invalid_key")
            return _error(401, "invalid_api_key", "Incorrect API key provided.", "invalid_request_error")
        if failure is not None and failure.kind == "rate_limited":
            record("rate_limited")
            return _error(429, "rate_limit_exceeded", "Rate limit reached for requests.", "requests",
                          {"retry-after": str(failure.retry_after_s or 1)})
        if failure is not None and failure.kind == "quota":
            record("quota")
            return _error(429, "insufficient_quota", "You exceeded your current quota.", "insufficient_quota")
        if failure is not None and failure.kind == "server_error":
            record("server_error")
            return _error(500, None, "The server had an error while processing your request.", "server_error")
        tools = body.get("tools") if isinstance(body.get("tools"), list) else []
        if not any(isinstance(tool, dict) and tool.get("type") == "web_search" for tool in tools) or not claim:
            record("bad_request")
            return _error(400, "invalid_request_error", "this stand-in answers web_search requests only",
                          "invalid_request_error")
        if failure is not None and failure.kind == "slow":
            await asyncio.sleep(failure.delay_s)
        script = scripted_answer(serialise_messages(call_messages), "responses")
        if script is not None and not isinstance(script, dict):
            script = {"text": as_content(script), "sources": []}
        topics = "new developments" in instructions.lower()
        output, text = web_search_output(claim, topics=topics, scripted=script)
        prompt_tokens = token_estimate(instructions + claim) + SEARCH_CONTENT_TOKENS
        completion_tokens = token_estimate(text)
        record("search", prompt_tokens, completion_tokens, text, script is not None)
        payload: dict[str, Any] = {
            "id": f"resp_e2e_{index:06d}", "object": "response", "created_at": int(time.time()),
            "status": "completed", "model": body.get("model"), "output": output,
        }
        if not (failure is not None and failure.kind == "empty_usage"):
            payload["usage"] = {
                "input_tokens": prompt_tokens, "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": completion_tokens, "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": prompt_tokens + completion_tokens,
            }
        return JSONResponse(payload)

    return app


def _usage(prompt_tokens: int, completion_tokens: int, cached: int = 0) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def _sse(event: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
