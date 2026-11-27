"""Drive the whole product for each persona, as the extension does, and record what came back.

The real gateway, services process and one engine per customer, started by the e2e harness. With
`RealAI` the services process calls the real provider with the evaluator's key, capped by the
product's own daily and monthly AI budgets; without it, the stand-in answers (free, and its text
means nothing: it only proves the tool works).

A stand-in run also serves the world tools from `world_fixtures.json` (linked pages and news
stories, STARLING_WORLD_FIXTURES), so reading a post's link and the news pool really run; a real
run reads the real world. `plans="real"` keeps the product's own plans file (the real Free caps)
instead of the relaxed EVAL_PLANS. Every press's `expects` is checked (`check_tools`).
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from e2e.harness import env as harness_env
from e2e.harness.stack import RealAI, Stack
from e2e.tests.client import Control, Device, Website, new_state, pkce_pair, redirect_query, redirect_uri_for

from .personas import PERSONAS, Persona, Press

WORLD_FIXTURES = Path(__file__).resolve().with_name("world_fixtures.json")
SEARCH_LANE = "starling-search"

# Generous limits for the run's own plans file: the evaluation measures drafts, not rate limits.
# The spend cap is the product's global AI budget (RealAI.budget_usd), not these ceilings.
EVAL_PLANS = """[unit]
name = "draft"
label = "drafts"

[plans.free]
name = "Free"
monthly_credits = 1000
cost_ceiling_usd = 5.00
rate_per_minute = 30
rate_per_day = 1000
public = true

[plans.starter]
name = "Starter"
monthly_credits = 2000
stripe_lookup_key = "starling_starter_monthly_usd"
amount_cents = 900
currency = "usd"
interval = "month"
cost_ceiling_usd = 10.00
rate_per_minute = 30
rate_per_day = 1000
public = true

[plans.pro]
name = "Pro"
monthly_credits = 4000
stripe_lookup_key = "starling_pro_monthly_usd"
amount_cents = 1900
currency = "usd"
interval = "month"
cost_ceiling_usd = 20.00
rate_per_minute = 30
rate_per_day = 1000
public = true
"""


@dataclass
class Outcome:
    persona: str
    press: str
    kind: str
    good: str
    post: dict[str, Any] | None
    draft_text: str
    message: str
    status: int = 0
    ok: bool = False
    action: str = ""
    verdict_why: str = ""
    draft: str = ""
    answer: str = ""
    rationale: str = ""
    notes: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    first_text_seconds: float | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    cost_micros: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    # Stand-in runs only: every call the provider received during this press, as its control log
    # reports it (the prompt and the answer, once the log carries them).
    provider_calls: list[dict[str, Any]] = field(default_factory=list)
    # The world tools this press should have used, and whether it did (`check_tools`).
    expects: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Run:
    started_at: str
    provider: str                  # "real" or "stand-in"
    models: dict[str, str]
    budget_usd: str
    plans: str = "eval"            # "eval" (relaxed limits) or "real" (the product's plans file)
    world: str = ""                # where the world tools read from: "fixtures" or "real"
    outcomes: list[Outcome] = field(default_factory=list)
    background: list[dict[str, Any]] = field(default_factory=list)   # ai_calls not tied to a press
    total_cost_micros: int = 0
    samples: dict[str, list[str]] = field(default_factory=dict)
    about: dict[str, str] = field(default_factory=dict)
    # What the engine concluded about each account, read from its own database after the run:
    # {"voice_card": {...} | None, "topics": [...], "tables": [...]} per persona.
    reads: dict[str, dict[str, Any]] = field(default_factory=dict)


# --------------------------------------------------------------------------- accounts


def _connect(site: Website, extension_id: str, gateway_url: str) -> Device:
    """The extension's sign-in, as `e2e/tests/conftest.connect_device` does it."""
    verifier, challenge = pkce_pair()
    state = new_state()
    params = {
        "client_id": extension_id, "redirect_uri": redirect_uri_for(extension_id), "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state, "device_label": "Evaluation Chrome",
    }
    query = redirect_query(site.approve_connect(params))
    exchange = site.http.post("/api/v1/devices/token", json={
        "grant_type": "authorization_code", "code": query["code"], "code_verifier": verifier,
        "redirect_uri": params["redirect_uri"], "client_id": extension_id, "device_label": "Evaluation Chrome",
        "extension_version": harness_env.EXTENSION_VERSION,
    })
    if exchange.status_code != 200:
        raise RuntimeError(f"the extension sign-in failed: {exchange.status_code} {exchange.text[:200]}")
    token = exchange.json()
    return Device(gateway_url, token["token"], token["device_id"], harness_env.EXTENSION_VERSION)


def _account(gateway_url: str, extension_id: str, control: Control, persona: Persona, index: int) -> tuple[str, Device, Website]:
    site = Website(gateway_url, client_ip=f"2001:db8:e7a1:{index:x}::1")
    email = f"eval-{persona.key}-{secrets.token_hex(3)}@starling.example"
    for step, response in (("sign-up", site.signup(email)), ("confirm", site.verify_email(email)), ("sign-in", site.login(email))):
        if response.status_code != 303:
            raise RuntimeError(f"{step} failed for {persona.key}: {response.status_code}")
    device = _connect(site, extension_id, gateway_url)
    consent = device.consent(passive_learning=False, version=harness_env.CONSENT_VERSION)
    if consent.status_code not in (200, 201):
        raise RuntimeError(f"consent failed for {persona.key}: {consent.status_code} {consent.text[:200]}")
    if persona.background:
        device.post("/own-material", {"kind": "background", "text": persona.background})
    for sample in persona.samples:
        added = device.post("/own-material", {"kind": "voice", "text": sample})
        if added.status_code != 200:
            raise RuntimeError(f"a writing sample was refused for {persona.key}: {added.status_code} {added.text[:200]}")
    device.session()  # the extension asks for its session when X opens; the engine may start then
    return control.account_id(email), device, site


# --------------------------------------------------------------------------- presses


def _body(press: Press) -> tuple[str, dict[str, Any]]:
    """(route, body) for one press: `/assist` for ✦, `/assist/stream` for the composer and chat."""
    if press.kind == "star":
        return "/assist", {"mode": "context", "context": press.post.context()}
    if press.kind == "chat":
        return "/assist/stream", {"mode": "chat", "message": press.message}
    action = "reply" if press.kind == "create_reply" else "post"
    intent = "enhance" if press.kind == "enhance" else "create"
    body: dict[str, Any] = {"mode": "enhance_composer",
                            "composer": {"action": action, "draft_text": press.draft_text, "intent": intent}}
    if press.post is not None:
        body["context"] = press.post.context()
    return "/assist/stream", body


def _stream(device: Device, body: dict[str, Any], key: str) -> tuple[int, dict[str, Any], float | None, str]:
    """POST /assist/stream: (status, final response, seconds to the first text, error)."""
    started = time.monotonic()
    first: float | None = None
    final: dict[str, Any] = {}
    error = ""
    headers = device.headers({"Idempotency-Key": key})
    with device.http.stream("POST", "/api/v1/assist/stream", json=body, headers=headers) as response:
        if response.status_code != 200:
            response.read()
            return response.status_code, {}, None, response.text[:300]
        for line in response.iter_lines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            kind = event.get("event")
            if kind == "token" and first is None and str(event.get("text") or "").strip():
                first = time.monotonic() - started
            elif kind == "final":
                final = event.get("response") or {}
            elif kind == "error":
                error = json.dumps(event.get("error") or event)[:300]
    return 200, final, first, error


def _read(outcome: Outcome, response: dict[str, Any]) -> None:
    decision = response.get("decision") or {}
    outcome.ok = bool(response.get("ok"))
    outcome.action = str(decision.get("action") or "")
    outcome.verdict_why = str(decision.get("why") or decision.get("rationale") or "")[:400]
    outcome.draft = str(decision.get("draft") or "")
    outcome.answer = str(response.get("answer") or "")
    outcome.rationale = str(decision.get("rationale") or "")
    for key in ("voice_note", "search_note", "review_note"):
        if decision.get(key):
            outcome.notes.append(f"{key}: {decision[key]}")
    if decision.get("web_checked") is False:
        outcome.notes.append("web_checked: false")
    sources = decision.get("sources") or ((response.get("research") or {}).get("sources") if isinstance(response.get("research"), dict) else None) or []
    outcome.sources = [{"url": str(s.get("url") or ""), "title": str(s.get("title") or "")} for s in sources if isinstance(s, dict)]
    if not outcome.ok and response.get("error"):
        outcome.error = json.dumps(response.get("error"))[:300]


def _metering(outcome: Outcome, control: Control, account_id: str, key: str) -> str | None:
    reservation = control.one(
        "SELECT id FROM usage_reservations WHERE account_id = ? AND idempotency_key = ?", (account_id, key))
    if reservation is None:
        return None
    calls = control.ai_calls(reservation_id=reservation["id"])
    outcome.calls = [{name: call.get(name) for name in ("lane", "model", "status", "error_code", "input_tokens",
                                                         "cached_input_tokens", "output_tokens", "cost_micros",
                                                         "started_at", "finished_at")} for call in calls]
    outcome.cost_micros = sum(int(call.get("cost_micros") or 0) for call in calls)
    outcome.input_tokens = sum(int(call.get("input_tokens") or 0) for call in calls)
    outcome.cached_input_tokens = sum(int(call.get("cached_input_tokens") or 0) for call in calls)
    outcome.output_tokens = sum(int(call.get("output_tokens") or 0) for call in calls)
    return str(reservation["id"])


def _provider_log(control: Control, control_url: str, *, reset: bool) -> list[dict[str, Any]]:
    """The stand-in's call log (`/_control/requests`); reset before a press, read after it."""
    if not control_url:
        return []
    try:
        if reset:
            control.http.post(f"{control_url}/requests/reset")
            return []
        answer = control.http.get(f"{control_url}/requests").json()
    except Exception:  # noqa: BLE001  the log is a view, never a reason to fail a press
        return []
    calls = answer.get("calls") if isinstance(answer, dict) else answer  # {"totals": {...}, "calls": [...]}
    return [call for call in (calls or []) if isinstance(call, dict)]


def _engine_read(data_dir: Path, tenant_seq: int) -> dict[str, Any]:
    """What the customer's engine concluded about them: its newest voice card and its topic candidates.

    Read straight from the engine's own database after the run (the engines are stopped by then). The
    tables are the engine's (voice_cards, topic_candidates); when a table is missing the read says so.
    """
    path = data_dir / "tenants" / str(tenant_seq) / "starling.db"
    read: dict[str, Any] = {"voice_card": None, "topics": [], "tables": []}
    if not path.exists():
        return read
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        read["tables"] = sorted(tables)
        if "voice_cards" in tables:
            row = connection.execute("SELECT * FROM voice_cards ORDER BY rowid DESC LIMIT 1").fetchone()
            if row is not None:
                read["voice_card"] = {key: row[key] for key in row.keys()}
        if "topic_candidates" in tables:
            rows = connection.execute("SELECT * FROM topic_candidates ORDER BY rowid DESC LIMIT 8").fetchall()
            read["topics"] = [{key: row[key] for key in row.keys()} for row in rows]
    finally:
        connection.close()
    return read


def check_tools(outcome: Outcome) -> list[dict[str, Any]]:
    """Did the press use the world tools it should have? One {"expect", "ok", "why"} per expectation.

    web_check: a web search ran for this press and the card links sources. link: the card cites a
    page the post itself links. news: the created post cites the story it is about.
    """
    checks: list[dict[str, Any]] = []
    cited = [str(source.get("url") or "") for source in outcome.sources]
    searches = [call for call in outcome.calls if call.get("lane") == SEARCH_LANE]
    notes = " ".join(outcome.notes)
    for expect in outcome.expects:
        if expect == "web_check":
            if not searches:
                ok, why = False, "no web search ran for this press"
            elif not any(call.get("status") == "ok" for call in searches):
                ok, why = False, "the web search failed (" + ", ".join(
                    str(call.get("error_code") or call.get("status")) for call in searches) + ")"
            elif not cited:
                ok, why = False, "a web search ran but the card shows no sources"
            else:
                ok, why = True, f"searched; {len(cited)} source(s) on the card"
        elif expect == "link":
            links = [str(url) for url in ((outcome.post or {}).get("links") or [])]
            hit = [url for url in cited if url in links]
            ok = bool(hit)
            why = "the card cites the linked page" if ok else (
                "the card cites other pages, not the linked one" if cited else "the linked page is not cited")
        elif expect == "news":
            ok = bool(cited)
            why = (f"the card cites {len(cited)} story page(s)" if ok else
                   "no story cited" + (f" ({notes[:160]})" if notes else ""))
        else:
            ok, why = False, f"unknown expectation {expect!r}"
        if outcome.error and not (outcome.draft or outcome.answer):
            ok, why = False, "the press failed"
        checks.append({"expect": expect, "ok": ok, "why": why})
    return checks


def press_once(device: Device, control: Control, account_id: str, persona: Persona, press: Press,
               provider_control: str = "") -> Outcome:
    outcome = Outcome(persona=persona.key, press=press.id, kind=press.kind, good=press.good,
                      post=press.post.context() if press.post else None, draft_text=press.draft_text,
                      message=press.message, expects=list(press.expects))
    route, body = _body(press)
    key = str(uuid.uuid4())
    _provider_log(control, provider_control, reset=True)
    started = time.monotonic()
    try:
        if route == "/assist":
            response = device.post("/assist", body, key=key)
            outcome.status = response.status_code
            payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if response.status_code == 200:
                _read(outcome, payload)
            else:
                outcome.error = json.dumps(payload.get("error") or payload)[:300] if payload else response.text[:300]
        else:
            status, final, first, error = _stream(device, body, key)
            outcome.status = status
            outcome.first_text_seconds = round(first, 2) if first is not None else None
            if final:
                _read(outcome, final)
            outcome.error = outcome.error or error
    except Exception as exc:  # noqa: BLE001  one failed press is a result, never a crashed run
        outcome.error = f"{type(exc).__name__}: {exc}"[:300]
    outcome.seconds = round(time.monotonic() - started, 2)
    _metering(outcome, control, account_id, key)
    outcome.provider_calls = _provider_log(control, provider_control, reset=False)
    outcome.checks = check_tools(outcome)
    return outcome


# --------------------------------------------------------------------------- the run


def run(personas: tuple[Persona, ...] = PERSONAS, *, real: RealAI | None, out_dir: Path,
        topic_wait_s: float = 0.0, say: Callable[[str], None] = print, golden_script: Path | None = None,
        plans: str = "eval", world_fixtures: Path | None = WORLD_FIXTURES) -> Run:
    out_dir = out_dir.resolve()  # the services process runs from another folder and reads plans.toml by path
    out_dir.mkdir(parents=True, exist_ok=True)
    if plans not in ("eval", "real"):
        raise ValueError("plans is 'eval' or 'real'")
    plans_file: Path | None = None
    if plans == "eval":
        plans_file = out_dir / "plans.toml"
        plans_file.write_text(EVAL_PLANS, encoding="utf-8")
    fake_env = {"E2E_AI_SCRIPT": str(golden_script)} if golden_script and not real else {}
    stack = Stack(state_file=out_dir / "state.json", real_ai=real, plans_file=plans_file, fake_ai_env=fake_env)
    provider = "real" if real else ("stand-in, golden answers" if fake_env else "stand-in")
    use_fixtures = bool(world_fixtures) and real is None
    record = Run(started_at=datetime.now(timezone.utc).isoformat(), provider=provider,
                 models={}, budget_usd=real.budget_usd if real else harness_env.DAILY_BUDGET_USD,
                 plans=plans, world="fixtures" if use_fixtures else ("real" if real else "offline"))
    # The world fixture reaches the services process through the harness env (stand-in runs only).
    previous = os.environ.get(harness_env.WORLD_FIXTURES_ENV)
    if use_fixtures:
        os.environ[harness_env.WORLD_FIXTURES_ENV] = str(Path(world_fixtures).resolve())
    try:
        state = stack.up()
    finally:
        if previous is None:
            os.environ.pop(harness_env.WORLD_FIXTURES_ENV, None)
        else:
            os.environ[harness_env.WORLD_FIXTURES_ENV] = previous
    control = Control(asdict(state))
    record.models = dict(state.settings.get("ai_models") or {})
    press_reservations: set[str] = set()
    provider_control = "" if real else str(state.fake_openai_control or "")
    tenants: dict[str, int] = {}
    try:
        for index, persona in enumerate(personas, start=1):
            say(f"[eval] {persona.name}: signing up and adding {len(persona.samples)} writing samples")
            account_id, device, site = _account(state.gateway_url, state.extension_id, control, persona, index)
            seq = control.scalar("SELECT tenant_seq FROM accounts WHERE id = ?", (account_id,))
            if seq is not None:
                tenants[persona.key] = int(seq)
            record.samples[persona.key] = list(persona.samples)
            record.about[persona.key] = f"{persona.name}: {persona.about}"
            try:
                now_first = [p for p in persona.presses if p.kind not in ("create_post", "chat")]
                later = [p for p in persona.presses if p.kind in ("create_post", "chat")]
                for press in now_first:
                    outcome = press_once(device, control, account_id, persona, press, provider_control)
                    record.outcomes.append(outcome)
                    say(f"[eval]   {press.id}: {outcome.status} {outcome.action or '-'} in {outcome.seconds:.1f} s")
                if later and topic_wait_s > 0:
                    say(f"[eval]   waiting {topic_wait_s:.0f} s for background topic research")
                    time.sleep(topic_wait_s)
                for press in later:
                    outcome = press_once(device, control, account_id, persona, press, provider_control)
                    record.outcomes.append(outcome)
                    say(f"[eval]   {press.id}: {outcome.status} {outcome.action or '-'} in {outcome.seconds:.1f} s")
            finally:
                device.close()
                site.close()
        reservations = {row["id"] for row in control.rows("SELECT id FROM usage_reservations")}
        press_reservations |= reservations
        every_call = control.rows("SELECT * FROM ai_calls ORDER BY id")
        record.total_cost_micros = sum(int(call.get("cost_micros") or 0) for call in every_call)
        record.background = [
            {name: call.get(name) for name in ("account_id", "bucket", "lane", "model", "status", "input_tokens",
                                               "cached_input_tokens", "output_tokens", "cost_micros")}
            for call in every_call if call.get("reservation_id") not in press_reservations
        ]
    finally:
        control.close()
        stack.down(keep_run_dir=True)
    for key, seq in tenants.items():
        try:
            record.reads[key] = _engine_read(Path(state.data_dir), seq)
        except sqlite3.Error as exc:
            record.reads[key] = {"voice_card": None, "topics": [], "tables": [], "error": str(exc)[:200]}
    return record
