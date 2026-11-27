"""Drive the running stack the way a browser and the extension do.

Three views of the same account, which every journey compares:

* `Website` - the real pages, posted to as a browser would (CSRF token from the form, `Origin`
  header, 303 redirects), with the numbers parsed out of the rendered HTML;
* `Device` - `/api/v1` with a device token, exactly as the extension calls it;
* `Control` - the control database, read only, plus the two fakes' control planes.

Nothing here knows anything the extension or a browser could not know, except the control
database, which is read to prove the other two agree with it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]*)"')
HIDDEN_RE = re.compile(r'name="([A-Za-z_][A-Za-z0-9_]*)"\s+value="([^"]*)"')
USAGE_RE = re.compile(r'data-usage="([a-z_]+)"[^>]*>([^<]*)<')
USAGE_VALUE_RE = re.compile(r'data-usage="([a-z_]+)"\s+data-value="(\d+)"')
PLAN_RE = re.compile(r'data-plan="([a-z_]*)"')
NOTICE_RE = re.compile(r'data-usage-notice="([a-z-]+)"')
DEFAULT_PASSWORD = "a long enough passphrase"
SAME_ORIGIN = "\x00same-origin"  # sentinel: send the gateway's own origin


def pkce_pair() -> tuple[str, str]:
    """A verifier and its S256 challenge, exactly as `extension/shared/auth.ts` builds them."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return verifier, challenge


def new_state() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def redirect_uri_for(extension_id: str) -> str:
    return f"https://{extension_id}.chromiumapp.org/connect"


def csrf_token(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "the page carries no CSRF token"
    return match.group(1)


def form_fields(html: str, action: str) -> dict[str, str]:
    """The hidden fields of the first form posting to `action`."""
    start = html.find(f'action="{action}"')
    assert start >= 0, f"no form posts to {action}"
    end = html.find("</form>", start)
    return {name: value for name, value in HIDDEN_RE.findall(html[start:end])}


def number(text: str) -> int:
    return int(text.replace(",", "").strip())


# --------------------------------------------------------------------------- the website


class Website:
    """One browser: its own cookie jar, its own CSRF tokens, its own network.

    `client_ip` is sent as `X-Forwarded-For`; the harness lists 127.0.0.1 in
    `STARLING_TRUSTED_PROXIES`, so the gateway treats it as the visitor's address. Giving each
    simulated customer its own prefix is what real customers have, and it keeps the per-prefix
    sign-up limit from capping how many accounts a run may create.
    """

    def __init__(self, base_url: str, client_ip: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_ip = client_ip
        headers = {"X-Forwarded-For": client_ip} if client_ip else {}
        self.http = httpx.Client(
            base_url=self.base_url, timeout=60.0, follow_redirects=False, trust_env=False, headers=headers
        )

    def close(self) -> None:
        self.http.close()

    # ---- raw

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(path, **kwargs)

    # Pages that always carry a form, signed in or out. The first one that renders wins; a
    # signed-out visitor also picks up the pre-session CSRF cookie from it.
    CSRF_PAGES = ("/account/security", "/signup", "/login")

    def token_from(self, path: str | None = None) -> str:
        for candidate in ((path,) if path else self.CSRF_PAGES):
            response = self.get(candidate)
            if response.status_code == 200:
                match = CSRF_RE.search(response.text)
                if match:
                    return match.group(1)
        raise AssertionError(f"no page carried a CSRF token (tried {path or self.CSRF_PAGES})")

    def post_form(
        self,
        path: str,
        data: dict[str, str],
        *,
        token: str | None = None,
        origin: str | None = SAME_ORIGIN,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """A browser form POST. `origin=None` sends no Origin header (and no Sec-Fetch-Site),
        `token=""` sends an empty CSRF field: both are refusals the journeys check."""
        payload = dict(data)
        payload["csrf_token"] = self.token_from() if token is None else token
        merged = dict(headers or {})
        if origin is SAME_ORIGIN:
            merged["Origin"] = self.base_url
        elif origin is not None:
            merged["Origin"] = origin
        return self.http.post(path, data=payload, headers=merged)

    # ---- account flows

    def signup(self, email: str, password: str = DEFAULT_PASSWORD) -> httpx.Response:
        return self.post_form("/signup", {"email": email, "password": password})

    def login(self, email: str, password: str = DEFAULT_PASSWORD) -> httpx.Response:
        return self.post_form("/login", {"email": email, "password": password})

    def logout(self) -> httpx.Response:
        return self.post_form("/logout", {})

    def outbox(self, to: str) -> list[dict[str, Any]]:
        response = self.get("/_dev/outbox", params={"to": to})
        assert response.status_code == 200, response.text
        return response.json()["messages"]

    def link_token(self, to: str, path: str) -> str:
        pattern = re.compile(re.escape(self.base_url + path) + r"\?token=([A-Za-z0-9_-]+)")
        for message in reversed(self.outbox(to)):
            found = pattern.search(message.get("body") or "")
            if found:
                return found.group(1)
        raise AssertionError(f"no {path} link was sent to {to}")

    def verify_email(self, email: str) -> httpx.Response:
        return self.post_form("/verify-email", {"token": self.link_token(email, "/verify-email")})

    # ---- what the pages say

    def account_usage(self) -> dict[str, Any]:
        """The numbers on `/account`, read from the rendered HTML."""
        page = self.get("/account")
        assert page.status_code == 200, page.text
        html = page.text
        values = {name: number(text) for name, text in USAGE_RE.findall(html) if text.strip().replace(",", "").isdigit()}
        for name, raw in USAGE_VALUE_RE.findall(html):
            values[name] = int(raw)
        plans = PLAN_RE.findall(html)
        return {
            "usage": values,
            "plan_code": plans[0] if plans else None,
            "notices": NOTICE_RE.findall(html),
            "html": html,
        }

    def billing_page(self) -> dict[str, Any]:
        page = self.get("/account/billing")
        assert page.status_code == 200, page.text
        plans = PLAN_RE.findall(page.text)
        return {"plan_code": plans[0] if plans else None, "html": page.text}

    def billing_status(self) -> dict[str, Any]:
        response = self.get("/billing/status", headers={"Accept": "application/json"})
        assert response.status_code == 200, response.text
        return response.json()["state"]

    def devices(self) -> list[dict[str, str]]:
        page = self.get("/account/devices")
        assert page.status_code == 200, page.text
        found = re.findall(r'action="/account/devices/([^"/]+)/revoke"', page.text)
        return [{"id": device_id} for device_id in found]

    # ---- extension connect (CONTRACTS 9)

    def connect_page(self, params: dict[str, str]) -> httpx.Response:
        return self.get("/connect", params=params)

    def approve_connect(self, params: dict[str, str]) -> httpx.Response:
        """Approve and return the 303 to `https://<id>.chromiumapp.org/connect`, never followed."""
        page = self.connect_page(params)
        assert page.status_code == 200, page.text[:400]
        fields = form_fields(page.text, "/connect/approve")
        return self.post_form("/connect/approve", fields, token=csrf_token(page.text))


def redirect_query(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 303, f"expected a redirect, got {response.status_code}"
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return {key: values[0] for key, values in query.items()}


# --------------------------------------------------------------------------- the extension


@dataclass
class StreamResult:
    events: list[dict[str, Any]]
    status: int
    headers: dict[str, str]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("event") == name]

    @property
    def final(self) -> dict[str, Any] | None:
        finals = self.of("final")
        return finals[-1] if finals else None

    @property
    def error(self) -> dict[str, Any] | None:
        errors = self.of("error")
        return errors[-1]["error"] if errors else None

    @property
    def usage(self) -> dict[str, Any] | None:
        blocks = self.of("usage")
        return blocks[-1]["usage"] if blocks else None

    @property
    def text(self) -> str:
        return "".join(event.get("text", "") for event in self.of("token"))


class Device:
    """The extension: a device token on `/api/v1`, never a cookie."""

    def __init__(self, base_url: str, token: str, device_id: str, version: str = "1.0.0") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.device_id = device_id
        self.version = version
        self.http = httpx.Client(base_url=self.base_url, timeout=300.0, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self.http.close()

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        base = {
            "Authorization": f"Bearer {self.token}",
            "X-Starling-Extension": self.version,
            "X-Starling-Time-Zone": "UTC",
        }
        base.update(extra or {})
        return base

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(f"/api/v1{path}", headers=self.headers(kwargs.pop("headers", None)), **kwargs)

    def post(self, path: str, body: Any = None, *, key: str | None = None,
             headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        merged = dict(headers or {})
        merged["Idempotency-Key"] = key or str(uuid.uuid4())
        return self.http.post(f"/api/v1{path}", json=body, headers=self.headers(merged), **kwargs)

    def delete(self, path: str, *, key: str | None = None) -> httpx.Response:
        merged = {"Idempotency-Key": key or str(uuid.uuid4())}
        return self.http.request("DELETE", f"/api/v1{path}", headers=self.headers(merged))

    def session(self) -> dict[str, Any]:
        response = self.get("/session")
        assert response.status_code == 200, response.text
        return response.json()

    def consent(self, *, passive_learning: bool, version: str) -> httpx.Response:
        return self.post("/consent", {"version": version, "passive_learning": passive_learning})

    def stream(self, body: dict[str, Any], *, key: str | None = None,
               stop_after_tokens: int | None = None, headers: dict[str, str] | None = None) -> StreamResult:
        """`POST /api/v1/assist/stream`, parsed into NDJSON events.

        `stop_after_tokens` closes the connection after that many `token` events, which is how
        the journeys drive a client disconnect before and after exposure.
        """
        merged = dict(headers or {})
        merged["Idempotency-Key"] = key or str(uuid.uuid4())
        events: list[dict[str, Any]] = []
        tokens = 0
        with self.http.stream(
            "POST", "/api/v1/assist/stream", json=body, headers=self.headers(merged)
        ) as response:
            status, head = response.status_code, dict(response.headers)
            if status != 200:
                response.read()
                try:
                    events.append({"event": "error", **response.json()})
                except ValueError:
                    events.append({"event": "error", "error": {"code": "unreadable", "message": response.text[:200]}})
                return StreamResult(events, status, head)
            for line in response.iter_lines():
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line))
                if events[-1].get("event") == "token":
                    tokens += 1
                if stop_after_tokens is not None and tokens >= stop_after_tokens:
                    # `stop_after_tokens=0` drops the connection as soon as the `accepted`
                    # line arrives, which is the disconnect-before-exposure case.
                    response.close()
                    break
        return StreamResult(events, status, head)


# --------------------------------------------------------------------------- the control plane


class Control:
    """The control database (read only) and the two fakes' control endpoints."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.db_path = Path(state["control_db"])
        self.http = httpx.Client(timeout=60.0, trust_env=False)

    def close(self) -> None:
        self.http.close()

    # ---- control database

    def _connect(self, writable: bool = False) -> sqlite3.Connection:
        uri = f"file:{self.db_path.as_posix()}" + ("" if writable else "?mode=ro")
        conn = sqlite3.connect(uri, uri=True, timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        return conn

    def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        found = self.rows(sql, params)
        return found[0] if found else None

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        found = self.one(sql, params)
        return None if found is None else next(iter(found.values()))

    def write(self, sql: str, params: tuple = ()) -> None:
        """Only for state an operator sets outside the API (the owner pause, an admin debit)."""
        conn = self._connect(writable=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql, params)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def account_id(self, email: str) -> str:
        row = self.one("SELECT id FROM accounts WHERE email_norm = ?", (email.lower(),))
        assert row is not None, f"no account for {email}"
        return str(row["id"])

    ZERO_COUNTERS = {
        "committed": 0, "reserved": 0, "granted": 0, "cost_micros": 0,
        "background_micros": 0, "released_with_spend": 0,
    }

    def counters(self, account_id: str) -> dict[str, Any]:
        """The current period's `usage_counters` row, or zeros before the first one exists."""
        row = self.one(
            "SELECT c.* FROM usage_counters c JOIN entitlement_periods p ON p.id = c.period_id "
            "WHERE c.account_id = ? ORDER BY p.period_start DESC LIMIT 1",
            (account_id,),
        )
        return row or {"account_id": account_id, "period_id": None, **self.ZERO_COUNTERS}

    def ai_calls(self, account_id: str | None = None, reservation_id: str | None = None) -> list[dict[str, Any]]:
        if reservation_id is not None:
            return self.rows("SELECT * FROM ai_calls WHERE reservation_id = ? ORDER BY started_at", (reservation_id,))
        return self.rows("SELECT * FROM ai_calls WHERE account_id = ? ORDER BY started_at", (account_id,))

    def reservations(self, account_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM usage_reservations WHERE account_id = ? ORDER BY created_at", (account_id,))

    def request_errors(self, account_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM request_errors WHERE account_id = ? ORDER BY last_at", (account_id,))

    def set_owner_pause(self, value: Any) -> None:
        """The value the owner dashboard writes for `service_state['ai_paused']`."""
        payload = json.dumps(value, separators=(",", ":"))
        self.write(
            "INSERT INTO service_state (key, value_json, updated_at) VALUES ('ai_paused', ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (payload, int(time.time())),
        )

    def clear_owner_pause(self) -> None:
        self.write("DELETE FROM service_state WHERE key = 'ai_paused'", ())

    def debit_credits(self, account_id: str, amount: int, reason: str = "e2e limit journey") -> dict[str, Any]:
        """The admin debit of `usage.grants`, run in this process against the same database."""
        return self._grants("debit_credits", account_id, amount, reason)

    def grant_credits(self, account_id: str, amount: int, reason: str = "e2e limit journey") -> dict[str, Any]:
        return self._grants("grant_credits", account_id, amount, reason)

    def _grants(self, name: str, account_id: str, amount: int, reason: str) -> dict[str, Any]:
        from e2e.harness.paths import add_server_to_path

        add_server_to_path()
        from starling_server.config import load_settings
        from starling_server.db import Database
        from starling_server.usage import grants

        settings = load_settings(self._settings_env())
        db = Database(self.db_path)
        try:
            return getattr(grants, name)(db, settings, account_id, amount, "e2e-harness", reason)
        finally:
            db.close()

    def set_period_cost(self, account_id: str, period_id: str, cost_micros: int) -> None:
        """Set the period's recorded AI spend, the column the services process owns.

        A journey cannot reach the fair-use ceiling ($0.30, about 300 drafts) by drafting, so it
        writes the spend a heavy month would have recorded and puts it back afterwards.
        """
        self.write(
            "INSERT INTO usage_counters (account_id, period_id, cost_micros) VALUES (?, ?, ?) "
            "ON CONFLICT (account_id, period_id) DO UPDATE SET cost_micros = excluded.cost_micros",
            (account_id, period_id, int(cost_micros)),
        )

    def global_budget(self, scope: str) -> dict[str, Any]:
        """Today's (or this month's) `budget_counters` row, with zeros when there is none yet."""
        window = time.strftime("%Y-%m" if scope == "global_month" else "%Y-%m-%d", time.gmtime())
        row = self.one(
            "SELECT * FROM budget_counters WHERE scope = ? AND window_key = ?", (scope, window)
        )
        return row or {"scope": scope, "window_key": window, "cost_micros": 0, "limit_micros": 0, "tripped_at": None}

    def _settings_env(self) -> dict[str, str]:
        from e2e.harness import env as harness_env

        return {
            "STARLING_ENV": "e2e",
            "STARLING_ISOLATION": "none",
            "STARLING_DATA_DIR": self.state["data_dir"],
            "STARLING_PUBLIC_URL": self.state["gateway_url"],
            "PLANS_FILE": str(harness_env.PLANS_FILE),
            "AI_PRICING_FILE": str(harness_env.AI_PRICING_FILE),
        }

    # ---- the fakes

    def openai(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.state["fake_openai_control"] + path
        response = self.http.post(url, json=payload or {}) if payload is not None else self.http.get(url)
        assert response.status_code == 200, response.text
        return response.json()

    def openai_get(self, path: str) -> dict[str, Any]:
        response = self.http.get(self.state["fake_openai_control"] + path)
        assert response.status_code == 200, response.text
        return response.json()

    def ai_requests(self) -> dict[str, Any]:
        return self.openai_get("/requests")

    def ai_reset(self) -> None:
        self.openai("/reset", {})

    def ai_fail(self, kind: str, **options: Any) -> None:
        self.openai("/fail", {"kind": kind, **options})

    def stripe(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.state["fake_stripe_control"] + path
        response = self.http.post(url, json=payload or {}) if payload is not None else self.http.get(url)
        assert response.status_code == 200, response.text
        return response.json()

    def stripe_get(self, path: str) -> dict[str, Any]:
        response = self.http.get(self.state["fake_stripe_control"] + path)
        assert response.status_code == 200, response.text
        return response.json()

    # ---- waiting

    def wait_for(self, predicate, timeout_s: float = 30.0, what: str = "a condition", interval: float = 0.2) -> Any:
        give_up = time.monotonic() + timeout_s
        last: Any = None
        while time.monotonic() < give_up:
            last = predicate()
            if last:
                return last
            time.sleep(interval)
        raise AssertionError(f"timed out after {timeout_s:g}s waiting for {what} (last value: {last!r})")

    def wait_for_events_processed(self, timeout_s: float = 30.0) -> None:
        self.wait_for(
            lambda: not self.rows("SELECT event_id FROM billing_events WHERE status IN ('received', 'processing')"),
            timeout_s,
            "every billing event to be processed",
        )


# --------------------------------------------------------------------------- an account


@dataclass
class Account:
    email: str
    password: str
    id: str
    site: Website
    device: Device | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        self.site.close()
        if self.device is not None:
            self.device.close()


def stream_body(mode: str = "chat", message: str = "What should I post about today?", **extra: Any) -> dict[str, Any]:
    """A `BrowserAssistRequest` for the stream route (extension/shared/types.ts)."""
    body: dict[str, Any] = {"mode": mode}
    if mode == "chat":
        body["message"] = message
    body.update(extra)
    return body


def assist_body(mode: str = "context", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"mode": mode}
    body.update(extra)
    return body
