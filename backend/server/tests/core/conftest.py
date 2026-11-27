"""Server core fixtures.

- `env` is overridden so `STARLING_WEB_DIR` points at minimal templates written per test. Each
  template prints the common context and dumps every key of `web.render.REQUIRED_CONTEXT` as JSON,
  so route tests never depend on the website workstream's real templates.
- `fake_clock` replaces `security.clock.now` so expiry tests move time instead of sleeping.
- `fake_state` replaces `usage.service.account_state`; `fake_pool` stands in for the engine pool.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from starling_server.security import clock
from starling_server.usage import service as usage_service
from starling_server.web.render import REQUIRED_CONTEXT, required_templates

from ..conftest import PUBLIC_URL, base_env

START_TIME = 1_789_000_000


def template_source(name: str) -> str:
    keys = REQUIRED_CONTEXT.get(name, ())
    extras = "\n".join(f'<pre id="ctx-{key}">{{{{ {key} | tojson }}}}</pre>' for key in keys)
    return (
        "<!doctype html>\n"
        f'<title>{name}</title>\n<p id="template">{name}</p>\n'
        '<p id="user">{% if user %}{{ user.email_masked }} admin={{ user.is_admin }}{% else %}anonymous{% endif %}</p>\n'
        '<p id="path">{{ request_path }}</p>\n'
        '<ul id="flash">{% for item in flash %}<li class="{{ item.level }}">{{ item.text }}</li>{% endfor %}</ul>\n'
        '<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"></form>\n'
        '<p id="site">{{ site.name }} {{ site.public_url }} {{ site.env }}</p>\n'
        f"{extras}\n"
    )


# The app factory builds its Jinja environment from `<STARLING_WEB_DIR>/templating.py`
# (`create_environment(web_dir)`). This stand-in has the same interface and strictness
# (StrictUndefined), without depending on the website workstream's filters.
TEMPLATING_STANDIN = '''
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape


def create_environment(web_dir, *, strict=True):
    return Environment(
        loader=FileSystemLoader(str(Path(web_dir) / "templates")),
        autoescape=select_autoescape(["html", "xml"], default_for_string=True),
        undefined=StrictUndefined,
    )
'''


def write_templates(root: Path, extra: tuple[str, ...] = ("docs/getting-started.html",)) -> Path:
    templates = root / "templates"
    for name in [*required_templates(), *extra]:
        path = templates / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template_source(name), encoding="utf-8")
    (root / "templating.py").write_text(TEMPLATING_STANDIN, encoding="utf-8")
    return root


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    return write_templates(tmp_path / "web")


@pytest.fixture
def env(tmp_path: Path, web_dir: Path) -> dict[str, str]:
    values = base_env(tmp_path / "data")
    values["STARLING_WEB_DIR"] = str(web_dir)
    return values


class FakeClock:
    def __init__(self, start: int) -> None:
        self.value = start

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock(START_TIME)
    monkeypatch.setattr(clock, "now", fake)
    return fake


@pytest.fixture
def fake_state(monkeypatch: pytest.MonkeyPatch):
    """A §6.2-shaped state built from the account row (hermetic: no billing or usage tables)."""
    calls: list[str] = []

    def account_state(db, settings, account_id, now=None):
        calls.append(account_id)
        with db.read() as conn:
            row = conn.execute("SELECT email, email_verified_at, status FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise usage_service.UsageError("not_found", 404, "Unknown account.")
        return {
            "account": {"id": account_id, "email_masked": "fake", "email_verified": row["email_verified_at"] is not None, "status": row["status"]},
            "plan": {"code": "free", "name": "Free", "status": "free", "interval": None, "period_start": 0, "period_end": 0,
                     "cancel_at": None, "scheduled_change": None, "payment_problem": False, "payment_issue": None},
            "usage": {"unit": "draft", "unit_label": "drafts", "allowance": 25, "used": 0, "reserved": 0, "remaining": 25,
                      "resets_at": 0, "near_limit": False, "fair_use_blocked": False},
            "consent": {"required_version": settings.consent_version, "accepted_version": None, "passive_learning": False},
            "service": {"status": "ok", "message": None},
            "extension": {"min_version": settings.extension_min_version},
        }

    monkeypatch.setattr(usage_service, "account_state", account_state)
    return calls


class FakePool:
    def __init__(self) -> None:
        self.requests: list[tuple[str, int, str, str]] = []
        self.deleted: list[tuple[str, int]] = []
        self.export_body = b'{"profile": {"samples": []}}'
        self.fail_delete = False

    async def request(self, account_id, tenant_seq, method, path, body, headers, timeout_s):
        self.requests.append((account_id, tenant_seq, method, path))
        return 200, self.export_body

    async def delete_data(self, account_id, tenant_seq):
        if self.fail_delete:
            raise RuntimeError("engine unavailable")
        self.deleted.append((account_id, tenant_seq))

    async def start(self, **kwargs):
        return None

    async def close(self):
        return None


def _swap_pool(app, replacement):
    """Install `replacement` after the lifespan started; put the real pool back before `client`
    shuts the app down, so the engines lifecycle closes the pool it created."""
    real = getattr(app.state, "engine_pool", None)
    app.state.engine_pool = replacement
    try:
        yield replacement
    finally:
        app.state.engine_pool = real


@pytest.fixture
def fake_pool(app, client):
    yield from _swap_pool(app, FakePool())


@pytest.fixture
def no_pool(app, client):
    """The gateway without an engine pool (for example before the engines lifecycle is enabled)."""
    yield from _swap_pool(app, None)


@pytest.fixture
def second_client(app, client):
    """Another browser on the same app (own cookie jar; the lifespan belongs to `client`)."""
    return TestClient(app, base_url=PUBLIC_URL)
