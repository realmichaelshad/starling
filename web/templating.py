"""Jinja2 environment for the Starling website templates.

The gateway (CONTRACTS section 10) and `scripts/check_site.py` must build the environment with
`create_environment()`, so a template that renders in the checks renders the same way in
production. The templates use the filters, tests and globals below; a plain Jinja2 environment
does not have them and fails to compile the templates.

Gateway wiring (FastAPI / Starlette >= 0.28), replacing `Jinja2Templates(directory=...)`:

    import importlib.util
    spec = importlib.util.spec_from_file_location("starling_web_templating", settings.web_dir / "templating.py")
    templating = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(templating)
    app.state.templates = Jinja2Templates(env=templating.create_environment(settings.web_dir))

Filters
    date(ts)            unix seconds -> "October 14, 2026" (UTC)
    datetime(ts)        unix seconds -> "Oct 14, 2026, 14:05 UTC"
    iso(ts)             unix seconds -> "2026-10-14T14:05:00Z" (for <time datetime>)
    money(cents, cur)   900, "usd" -> "$9"; 950 -> "$9.50"
    usd_micros(m)       1_234_567 -> "$1.23"; small non-zero amounts keep 4 decimals
    number(n)           12345 -> "12,345"
    percent(x)          12.345 -> "12.3%" (x is already 0..100)
    placeholders(text)  escapes text and wraps [[KEY]] markers in <mark class="placeholder">
    short_id(value)     first 8 characters (for UUIDs in admin tables)

Tests
    placeholder         `value is placeholder` when the whole value is a [[KEY]] marker

Globals
    static(path)        "/static/<path>?v=<content hash>"; raises if the file is missing
    usage_percent(used, allowance) integer 0..100 for usage meters
    legal_values(site.legal) dict keyed by marker name (OPERATOR_COMPANY_NAME, ...) built from the
                        gateway's LegalDetails fields (company_name, ...); a field the gateway does
                        not provide yet becomes its [[MARKER]] so the page shows what is missing
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined, select_autoescape
from markupsafe import Markup, escape

WEB_DIR = Path(__file__).resolve().parent
PLACEHOLDER_RE = re.compile(r"\[\[[A-Z0-9_]+\]\]")

# Marker name -> field name in the gateway's LegalDetails (config.py). The last two have no
# setting yet; until they do, the legal pages show their markers.
LEGAL_FIELDS = {
    "OPERATOR_COMPANY_NAME": "company_name",
    "OPERATOR_REGISTERED_ADDRESS": "registered_address",
    "OPERATOR_CONTACT_EMAIL": "contact_email",
    "OPERATOR_PRIVACY_CONTACT": "privacy_contact",
    "OPERATOR_JURISDICTION": "jurisdiction",
    "OPERATOR_GOVERNING_LAW_VENUE": "governing_law_venue",
    "OPERATOR_EFFECTIVE_DATE": "effective_date",
    "HOSTING_PROVIDER": "hosting_provider",
    "EMAIL_PROVIDER": "email_provider",
    "AI_SUBPROCESSORS": "ai_subprocessors",
    "RETENTION_PERIODS": "retention_periods",
    "REFUND_POLICY": "refund_policy",
}

_CURRENCY_SYMBOLS = {"usd": "$", "eur": "€", "gbp": "£", "cad": "CA$", "aud": "A$"}


def _utc(ts: int | float) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def format_date(ts: int | float) -> str:
    d = _utc(ts)
    return f"{d:%B} {d.day}, {d.year}"


def format_datetime(ts: int | float) -> str:
    d = _utc(ts)
    return f"{d:%b} {d.day}, {d.year}, {d:%H:%M} UTC"


def format_iso(ts: int | float) -> str:
    return _utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_money(cents: int, currency: str | None = "usd") -> str:
    cents = int(cents or 0)
    code = (currency or "usd").lower()
    amount = f"{cents // 100:,}" if cents % 100 == 0 else f"{cents / 100:,.2f}"
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{symbol}{amount}" if symbol else f"{amount} {code.upper()}"


def format_usd_micros(micros: int) -> str:
    dollars = int(micros) / 1_000_000
    if 0 < abs(dollars) < 0.01:
        return f"${dollars:.4f}"
    return f"${dollars:,.2f}"


def format_number(value: int | float) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def format_percent(value: int | float, digits: int = 1) -> str:
    return f"{float(value):.{digits}f}%"


def mark_placeholders(value: Any) -> Markup:
    text = str(escape(value))
    return Markup(PLACEHOLDER_RE.sub(lambda m: f'<mark class="placeholder">{m.group(0)}</mark>', text))


def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and PLACEHOLDER_RE.fullmatch(value.strip()) is not None


def short_id(value: Any) -> str:
    return str(value)[:8]


def usage_percent(used: int, allowance: int) -> int:
    if allowance <= 0:
        return 100 if used > 0 else 0
    return max(0, min(100, round(used * 100 / allowance)))


def legal_values(legal: Mapping[str, Any] | None) -> dict[str, str]:
    source = dict(legal or {})
    values = {}
    for marker, field in LEGAL_FIELDS.items():
        value = source.get(field, source.get(marker))
        values[marker] = value if isinstance(value, str) and value.strip() else f"[[{marker}]]"
    return values


def find_placeholders(html: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(html)))


def create_environment(web_dir: Path | str = WEB_DIR, *, strict: bool = True) -> Environment:
    """Build the website environment. `strict=True` makes a missing context key an error."""
    root = Path(web_dir)
    static_dir = root / "static"

    @lru_cache(maxsize=None)
    def static(path: str) -> str:
        target = (static_dir / path).resolve()
        if static_dir.resolve() not in target.parents or not target.is_file():
            raise FileNotFoundError(f"static asset not found: {path}")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:10]
        return f"/static/{path}?v={digest}"

    env = Environment(
        loader=FileSystemLoader(str(root / "templates")),
        autoescape=select_autoescape(["html", "xml"], default_for_string=True),
        undefined=StrictUndefined if strict else Undefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        date=format_date,
        datetime=format_datetime,
        iso=format_iso,
        money=format_money,
        usd_micros=format_usd_micros,
        number=format_number,
        percent=format_percent,
        placeholders=mark_placeholders,
        short_id=short_id,
    )
    env.tests["placeholder"] = is_placeholder
    env.globals.update(static=static, usage_percent=usage_percent, legal_values=legal_values)
    return env
