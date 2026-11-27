"""Typed settings for every Starling server process.

All configuration comes from the environment (see `.env.example` and
docs/architecture/CONTRACTS.md section 12). `load_settings()` parses and normalises;
`validate()` reports problems for a given process role. In production any problem is
fatal at startup; in development and e2e the problems are logged instead, so a
developer can run the stack without real credentials.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parents[1]
DEFAULT_PLANS_FILE = SERVER_ROOT / "config" / "plans.toml"
DEFAULT_AI_PRICING_FILE = SERVER_ROOT / "config" / "ai_pricing.json"
DEFAULT_WEB_DIR = REPO_ROOT / "web"

ENVIRONMENTS = ("production", "development", "e2e")
ISOLATION_MODES = ("os", "none")
EMAIL_MODES = ("smtp", "outbox")
STRIPE_MODES = ("test", "live")
ROLES = ("gateway", "services", "supervisor", "cli")

# AI_PROVIDER picks the API the services process calls; engines see the same OpenAI-shaped proxy
# either way (services/anthropic_upstream.py translates for Anthropic). Per provider: the default
# model for every lane, the default AI_BASE_URL and the default AI_SUBPROCESSORS name.
AI_PROVIDERS = ("openai", "anthropic")
AI_PROVIDER_DEFAULTS = {
    "openai": {"model": "gpt-5.6-luna", "base_url": "https://api.openai.com/v1", "subprocessor": "OpenAI"},
    "anthropic": {"model": "claude-haiku-5-5", "base_url": "https://api.anthropic.com", "subprocessor": "Anthropic"},
}
# AI_REASONING_EFFORT values the Anthropic translation knows (none and minimal become low).
ANTHROPIC_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# claude-haiku-5-5 costs five times as much per token once a prompt is over 100,000 tokens, and
# config/ai_pricing.json holds only the lower price. The per-call input cap must stay well below
# that line (a token estimate of chars / 4 runs low on Claude's tokenizer).
ANTHROPIC_MAX_INPUT_TOKENS_PER_CALL = 60_000

MICROS_PER_USD = 1_000_000


class ConfigError(ValueError):
    """Raised when the configuration cannot be used."""


# --------------------------------------------------------------------------- plans


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    monthly_credits: int
    cost_ceiling_micros: int
    rate_per_minute: int
    rate_per_day: int
    public: bool
    stripe_lookup_key: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    interval: str | None = None
    order: int = 0

    @property
    def is_paid(self) -> bool:
        return self.stripe_lookup_key is not None


@dataclass(frozen=True)
class Plans:
    unit_name: str
    unit_label: str
    by_code: Mapping[str, Plan]

    @property
    def free(self) -> Plan:
        return self.by_code["free"]

    def get(self, code: str) -> Plan | None:
        return self.by_code.get(code)

    def public_plans(self) -> list[Plan]:
        return sorted((p for p in self.by_code.values() if p.public), key=lambda p: p.order)

    def paid_plans(self) -> list[Plan]:
        return sorted((p for p in self.by_code.values() if p.is_paid), key=lambda p: p.order)

    def by_lookup_key(self, lookup_key: str) -> Plan | None:
        for plan in self.by_code.values():
            if plan.stripe_lookup_key == lookup_key:
                return plan
        return None


def load_plans(path: Path) -> Plans:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"plans file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"plans file is not valid TOML: {path}: {exc}") from exc
    unit = data.get("unit", {})
    raw_plans = data.get("plans", {})
    if "free" not in raw_plans:
        raise ConfigError("plans file must define [plans.free]")
    plans: dict[str, Plan] = {}
    for order, (code, raw) in enumerate(raw_plans.items()):
        try:
            plan = Plan(
                code=code,
                name=str(raw["name"]),
                monthly_credits=int(raw["monthly_credits"]),
                cost_ceiling_micros=usd_to_micros(float(raw["cost_ceiling_usd"])),
                rate_per_minute=int(raw["rate_per_minute"]),
                rate_per_day=int(raw["rate_per_day"]),
                public=bool(raw.get("public", True)),
                stripe_lookup_key=raw.get("stripe_lookup_key") or None,
                amount_cents=int(raw["amount_cents"]) if "amount_cents" in raw else None,
                currency=str(raw["currency"]).lower() if "currency" in raw else None,
                interval=str(raw["interval"]) if "interval" in raw else None,
                order=order,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"plan '{code}' is incomplete or invalid: {exc}") from exc
        if plan.monthly_credits < 0 or plan.cost_ceiling_micros < 0:
            raise ConfigError(f"plan '{code}' has a negative allowance or ceiling")
        if plan.is_paid and (plan.amount_cents is None or not plan.currency or plan.interval != "month"):
            raise ConfigError(f"paid plan '{code}' needs amount_cents, currency and interval = \"month\"")
        if code == "free" and plan.is_paid:
            raise ConfigError("the free plan must not have a Stripe lookup key")
        plans[code] = plan
    _check_plans_make_sense(plans)
    return Plans(
        unit_name=str(unit.get("name", "draft")),
        unit_label=str(unit.get("label", "drafts")),
        by_code=plans,
    )


def _check_plans_make_sense(plans: Mapping[str, Plan]) -> None:
    """Refuse a plans file whose numbers would quietly break limits or billing.

    This is the one file a new owner is certain to edit, and every mistake below starts the
    server without a word and then fails every customer on the plan:

    * a rate of 0 refuses every request as `rate_limited`, for ever;
    * a fair-use ceiling of 0 beside a real allowance refuses every request as
      `fair_use_exceeded`, because spend (0) has already reached the ceiling (0);
    * a paid plan with no allowance, or no more than the free plan's, takes money for nothing;
    * two plans sharing a Stripe lookup key cannot be told apart when a payment arrives;
    * a dearer plan that allows less than a cheaper one turns an upgrade into a downgrade
      (`billing.mirror.plan_rank` orders plans by price, then allowance).
    """
    problems: list[str] = []
    free = plans["free"]
    for code, plan in plans.items():
        if plan.rate_per_minute < 1 or plan.rate_per_day < 1:
            problems.append(f"plan '{code}': rate_per_minute and rate_per_day must be at least 1")
        if plan.monthly_credits > 0 and plan.cost_ceiling_micros <= 0:
            problems.append(
                f"plan '{code}': cost_ceiling_usd must be above 0 when the plan has an allowance "
                "(a ceiling of 0 blocks every request as fair-use exceeded)"
            )
        if not plan.name.strip():
            problems.append(f"plan '{code}': name is empty")
        if plan.is_paid:
            if (plan.amount_cents or 0) < 1:
                problems.append(f"paid plan '{code}': amount_cents must be at least 1")
            if plan.monthly_credits <= free.monthly_credits:
                problems.append(
                    f"paid plan '{code}': monthly_credits ({plan.monthly_credits}) must be more than the free "
                    f"plan's ({free.monthly_credits})"
                )
            if not (plan.currency and len(plan.currency) == 3 and plan.currency.isalpha()):
                problems.append(f"paid plan '{code}': currency must be a three-letter code")
    paid = [plan for plan in plans.values() if plan.is_paid]
    keys = [plan.stripe_lookup_key for plan in paid]
    for key in sorted({key for key in keys if keys.count(key) > 1}):
        problems.append(f"stripe_lookup_key '{key}' is used by more than one plan")
    names = [plan.name.strip().lower() for plan in plans.values()]
    for name in sorted({name for name in names if name and names.count(name) > 1}):
        problems.append(f"plan name '{name}' is used by more than one plan")
    by_currency: dict[str, list[Plan]] = {}
    for plan in paid:
        by_currency.setdefault(plan.currency or "", []).append(plan)
    for group in by_currency.values():
        ordered = sorted(group, key=lambda plan: plan.amount_cents or 0)
        for cheaper, dearer in zip(ordered, ordered[1:]):
            if (dearer.amount_cents or 0) > (cheaper.amount_cents or 0) and dearer.monthly_credits < cheaper.monthly_credits:
                problems.append(
                    f"plan '{dearer.code}' costs more than '{cheaper.code}' but allows fewer "
                    f"({dearer.monthly_credits} against {cheaper.monthly_credits}): an upgrade would take drafts away"
                )
    if problems:
        raise ConfigError("plans file problems:\n- " + "\n- ".join(problems))


# --------------------------------------------------------------------------- AI pricing


@dataclass(frozen=True)
class ModelPrice:
    model: str
    effective_from: date
    input_micros_per_mtok: int
    cached_input_micros_per_mtok: int
    output_micros_per_mtok: int
    source: str

    def cost_micros(self, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> int:
        """Cost of one call. `input_tokens` includes cached tokens, as providers report it."""
        uncached = max(0, input_tokens - cached_input_tokens)
        total = (
            uncached * self.input_micros_per_mtok
            + cached_input_tokens * self.cached_input_micros_per_mtok
            + output_tokens * self.output_micros_per_mtok
        )
        return -(-total // 1_000_000)  # ceiling division: never under-count spend


@dataclass(frozen=True)
class AiPricing:
    prices: tuple[ModelPrice, ...]

    def price_for(self, model: str, at: date | None = None) -> ModelPrice | None:
        when = at or datetime.now(timezone.utc).date()
        candidates = [p for p in self.prices if p.model == model and p.effective_from <= when]
        return max(candidates, key=lambda p: p.effective_from) if candidates else None


def load_ai_pricing(path: Path) -> AiPricing:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"AI pricing file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"AI pricing file is not valid JSON: {path}: {exc}") from exc
    prices = []
    for raw in data.get("models", []):
        try:
            prices.append(
                ModelPrice(
                    model=str(raw["model"]),
                    effective_from=date.fromisoformat(str(raw["effective_from"])),
                    input_micros_per_mtok=usd_to_micros(float(raw["input_per_mtok"])),
                    cached_input_micros_per_mtok=usd_to_micros(float(raw["cached_input_per_mtok"])),
                    output_micros_per_mtok=usd_to_micros(float(raw["output_per_mtok"])),
                    source=str(raw.get("source", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"AI pricing entry is invalid: {raw!r}: {exc}") from exc
    return AiPricing(prices=tuple(prices))


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class LegalDetails:
    company_name: str
    registered_address: str
    contact_email: str
    privacy_contact: str
    jurisdiction: str
    governing_law_venue: str
    effective_date: str
    hosting_provider: str
    email_provider: str
    ai_subprocessors: str
    retention_periods: str
    refund_policy: str

    def placeholders(self) -> list[str]:
        """Names of fields that still hold `[[PLACEHOLDER]]` markers."""
        return [name for name, value in self.__dict__.items() if value.startswith("[[")]


@dataclass(frozen=True)
class Settings:
    env: str
    public_url: str
    data_dir: Path
    web_dir: Path
    secret_key: bytes
    secret_key_ephemeral: bool
    secret_key_previous: bytes
    isolation: str
    trusted_proxies: tuple[str, ...]
    extension_ids: tuple[str, ...]
    owner_emails: tuple[str, ...]
    support_email: str
    extension_min_version: str
    consent_version: str
    extension_install_url: str
    legal: LegalDetails

    email_mode: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_starttls: bool
    smtp_from: str

    ai_provider: str
    ai_base_url: str
    ai_api_key: str
    ai_api_key_secondary: str
    ai_model_quality: str
    ai_model_fast: str
    ai_model_vision: str
    ai_reasoning_effort: str
    ai_pricing_file: Path
    ai_credit_multiplier: int
    ai_max_calls_per_request: int
    ai_max_input_tokens_per_call: int
    ai_max_output_tokens: int
    ai_timeout_s: float
    ai_daily_budget_micros: int
    ai_monthly_budget_micros: int
    ai_free_trip_pct: int

    web_search_enabled: bool
    web_search_model: str
    web_search_context_size: str
    web_search_per_account_day: int
    web_search_background_per_account_day: int
    web_search_timeout_s: float

    plans_file: Path
    past_due_grace_days: int
    signup_global_per_hour: int
    max_devices_per_account: int
    disposable_domains_file: Path | None

    stripe_mode: str
    stripe_secret_key: str
    stripe_account_id: str
    stripe_webhook_secret: str
    stripe_webhook_secret_previous: str
    stripe_api_base: str
    stripe_api_version: str
    stripe_portal_configuration_id: str
    stripe_portal_upgrade_configuration_id: str
    stripe_automatic_tax: bool
    stripe_tax_code: str
    stripe_block_prepaid: bool
    stripe_allow_promo_codes: bool
    stripe_allow_test_mode: bool

    engine_max_live: int
    engine_idle_seconds: int
    engine_memory_soft_mb: int
    engine_memory_hard_mb: int
    engine_uid_base: int
    world_offline: bool
    world_fixtures: str
    e2e_payment_pages_origin: str

    # Sign in with Google (auth/google.py). Off unless both are set.
    google_client_id: str
    google_client_secret: str
    # The e2e harness's stand-in for Google's endpoints (loopback only; refused in production).
    e2e_oidc_base: str

    argon2_time_cost: int
    argon2_memory_kib: int
    argon2_parallelism: int
    argon2_max_concurrent: int
    log_level: str
    backup_dir: Path
    backup_passphrase: str
    alert_email: str

    services_url: str
    services_listen: str
    services_token_key: bytes
    services_token_key_ephemeral: bool

    plans: Plans = field(compare=False)
    ai_pricing: AiPricing = field(compare=False)

    # ---- derived paths
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def google_sign_in_enabled(self) -> bool:
        """"Continue with Google" is offered only when the OAuth client is fully configured."""
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def payments_rehearsal(self) -> bool:
        """Production, deliberately on Stripe's sandbox: every page says so and nothing is indexed."""
        return self.is_production and self.stripe_mode == "test" and self.stripe_allow_test_mode

    @property
    def public_url_is_loopback(self) -> bool:
        """The site's own address is this machine: a development or harness run, never a deployment."""
        host = (urlsplit(self.public_url).hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    @property
    def dev_tools_enabled(self) -> bool:
        """Development-only routes (`/_dev/*`). Three independent conditions, because one was not enough.

        `STARLING_ENV` defaults to development, so a deployment that simply FORGOT the variable
        used to serve `/_dev/outbox`, which returns email bodies with live password-reset links,
        to anybody who asked. A real deployment has a public address and sends real email, so
        it fails the other two conditions however its environment is spelt.
        """
        return (not self.is_production) and self.public_url_is_loopback and self.email_mode == "outbox"

    @property
    def control_db_path(self) -> Path:
        return self.data_dir / "control" / "control.db"

    @property
    def tenants_dir(self) -> Path:
        return self.data_dir / "tenants"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def run_dir(self) -> Path:
        return Path("/run/starling") if self.isolation == "os" else self.data_dir / "run"

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        return "__Host-starling_session" if self.cookie_secure else "starling_session"


# --------------------------------------------------------------------------- parsing helpers


def usd_to_micros(value: float) -> int:
    return int(round(value * MICROS_PER_USD))


def _str(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(name, default).strip()


def _operator(env: Mapping[str, str], suffix: str) -> str:
    """One of the operator's business details, `OPERATOR_<suffix>`.

    These settings were named `BUYER_<suffix>` before the project was opened, and a deployment
    configured then still starts: the old name is read when the new one is not set. Unset, the value
    is its `[[OPERATOR_<suffix>]]` marker, which the legal pages show and production refuses.
    """
    for name in (f"OPERATOR_{suffix}", f"BUYER_{suffix}"):
        value = env.get(name, "").strip()
        if value:
            return value
    return f"[[OPERATOR_{suffix}]]"


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int | None = None) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _list(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = env.get(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _choice(env: Mapping[str, str], name: str, choices: tuple[str, ...], default: str) -> str:
    value = _str(env, name, default).lower()
    if value not in choices:
        raise ConfigError(f"{name} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _secret_key(env: Mapping[str, str], environment: str) -> tuple[bytes, bool]:
    raw = _str(env, "STARLING_SECRET_KEY")
    if not raw:
        if environment == "production":
            return b"", False
        return secrets.token_bytes(32), True
    padded = raw + "=" * (-len(raw) % 4)
    try:
        key = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError("STARLING_SECRET_KEY must be base64url encoded") from exc
    if len(key) < 32:
        raise ConfigError("STARLING_SECRET_KEY must decode to at least 32 bytes")
    return key, False


def _previous_secret_key(env: Mapping[str, str]) -> bytes:
    """Optional previous gateway secret, accepted for verification and decryption during rotation."""
    raw = _str(env, "STARLING_SECRET_KEY_PREVIOUS")
    if not raw:
        return b""
    padded = raw + "=" * (-len(raw) % 4)
    try:
        key = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError("STARLING_SECRET_KEY_PREVIOUS must be base64url encoded") from exc
    if len(key) < 32:
        raise ConfigError("STARLING_SECRET_KEY_PREVIOUS must decode to at least 32 bytes")
    return key


def _services_token_key(env: Mapping[str, str], environment: str, secret_key: bytes, secret_ephemeral: bool) -> tuple[bytes, bool]:
    """Key shared by the gateway and services processes for signed services tokens.

    Production requires an explicit `STARLING_SERVICES_TOKEN_KEY` (the services process must
    not hold the gateway's session secret). Development and e2e derive one from the gateway
    secret so a single `.env` works for both processes.
    """
    raw = _str(env, "STARLING_SERVICES_TOKEN_KEY")
    if raw:
        padded = raw + "=" * (-len(raw) % 4)
        try:
            key = base64.urlsafe_b64decode(padded)
        except (binascii.Error, ValueError) as exc:
            raise ConfigError("STARLING_SERVICES_TOKEN_KEY must be base64url encoded") from exc
        if len(key) < 32:
            raise ConfigError("STARLING_SERVICES_TOKEN_KEY must decode to at least 32 bytes")
        return key, False
    if environment == "production":
        return b"", False
    if secret_key and not secret_ephemeral:
        return hmac.new(secret_key, b"starling-services-token-v1", hashlib.sha256).digest(), False
    return secrets.token_bytes(32), True


def _install_url(env: Mapping[str, str], environment: str) -> str:
    """The Chrome Web Store listing. In production it can be worked out from the published id.

    "Add to Chrome" is the one step nobody can skip, so a deployment that forgot the variable still
    gets a working button: the store serves every listing at /detail/<extension id>.
    """
    configured = _str(env, "STARLING_EXTENSION_INSTALL_URL")
    if configured or environment != "production":
        return configured
    ids = _list(env, "STARLING_EXTENSION_IDS")
    return f"https://chromewebstore.google.com/detail/{ids[0]}" if ids else ""


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = dict(os.environ if environ is None else environ)
    environment = _choice(env, "STARLING_ENV", ENVIRONMENTS, "development")
    secret_key, ephemeral = _secret_key(env, environment)
    isolation = _choice(env, "STARLING_ISOLATION", ISOLATION_MODES, "os" if environment == "production" else "none")
    services_key, services_key_ephemeral = _services_token_key(env, environment, secret_key, ephemeral)
    default_services_address = "unix:/run/starling/services.sock" if isolation == "os" else "tcp:127.0.0.1:8766"
    data_dir = Path(_str(env, "STARLING_DATA_DIR", "/data" if environment == "production" else str(REPO_ROOT / "data")))
    plans_file = Path(_str(env, "PLANS_FILE", str(DEFAULT_PLANS_FILE)))
    ai_pricing_file = Path(_str(env, "AI_PRICING_FILE", str(DEFAULT_AI_PRICING_FILE)))
    disposable = _str(env, "DISPOSABLE_DOMAINS_FILE")
    ai_provider = _choice(env, "AI_PROVIDER", AI_PROVIDERS, "openai")
    provider_defaults = AI_PROVIDER_DEFAULTS[ai_provider]
    ai_model_default = provider_defaults["model"]
    ai_model_fast = _str(env, "AI_MODEL_FAST", ai_model_default)

    legal = LegalDetails(
        company_name=_operator(env, "COMPANY_NAME"),
        registered_address=_operator(env, "REGISTERED_ADDRESS"),
        contact_email=_operator(env, "CONTACT_EMAIL"),
        privacy_contact=_operator(env, "PRIVACY_CONTACT"),
        jurisdiction=_operator(env, "JURISDICTION"),
        governing_law_venue=_operator(env, "GOVERNING_LAW_VENUE"),
        effective_date=_operator(env, "EFFECTIVE_DATE"),
        hosting_provider=_str(env, "HOSTING_PROVIDER", "[[HOSTING_PROVIDER]]"),
        email_provider=_str(env, "EMAIL_PROVIDER", "[[EMAIL_PROVIDER]]"),
        ai_subprocessors=_str(env, "AI_SUBPROCESSORS", provider_defaults["subprocessor"]),
        retention_periods=_str(env, "RETENTION_PERIODS", "[[RETENTION_PERIODS]]"),
        refund_policy=_str(env, "REFUND_POLICY", "[[REFUND_POLICY]]"),
    )

    return Settings(
        env=environment,
        public_url=_str(env, "STARLING_PUBLIC_URL", "http://127.0.0.1:8765").rstrip("/"),
        data_dir=data_dir,
        web_dir=Path(_str(env, "STARLING_WEB_DIR", str(DEFAULT_WEB_DIR))),
        secret_key=secret_key,
        secret_key_ephemeral=ephemeral,
        secret_key_previous=_previous_secret_key(env),
        isolation=isolation,
        trusted_proxies=_list(env, "STARLING_TRUSTED_PROXIES"),
        extension_ids=_list(env, "STARLING_EXTENSION_IDS"),
        owner_emails=tuple(e.lower() for e in _list(env, "STARLING_OWNER_EMAILS")),
        support_email=_str(env, "STARLING_SUPPORT_EMAIL", "[[OPERATOR_CONTACT_EMAIL]]"),
        extension_min_version=_str(env, "STARLING_EXTENSION_MIN_VERSION", "1.0.0"),
        consent_version=_str(env, "STARLING_CONSENT_VERSION", "2026-09-22.1"),
        extension_install_url=_install_url(env, environment),
        legal=legal,
        email_mode=_choice(env, "EMAIL_MODE", EMAIL_MODES, "smtp" if environment == "production" else "outbox"),
        smtp_host=_str(env, "SMTP_HOST"),
        smtp_port=_int(env, "SMTP_PORT", 587, minimum=1),
        smtp_username=_str(env, "SMTP_USERNAME"),
        smtp_password=env.get("SMTP_PASSWORD", ""),
        smtp_starttls=_bool(env, "SMTP_STARTTLS", True),
        smtp_from=_str(env, "SMTP_FROM"),
        ai_provider=ai_provider,
        ai_base_url=_str(env, "AI_BASE_URL", provider_defaults["base_url"]).rstrip("/"),
        ai_api_key=env.get("AI_API_KEY", "").strip(),
        ai_api_key_secondary=env.get("AI_API_KEY_SECONDARY", "").strip(),
        ai_model_quality=_str(env, "AI_MODEL_QUALITY", ai_model_default),
        ai_model_fast=ai_model_fast,
        ai_model_vision=_str(env, "AI_MODEL_VISION", ai_model_default),
        ai_reasoning_effort=_str(env, "AI_REASONING_EFFORT", "none"),
        ai_pricing_file=ai_pricing_file,
        ai_credit_multiplier=_int(env, "AI_CREDIT_MULTIPLIER", 1, minimum=1),
        ai_max_calls_per_request=_int(env, "AI_MAX_CALLS_PER_REQUEST", 12, minimum=1),
        ai_max_input_tokens_per_call=_int(env, "AI_MAX_INPUT_TOKENS_PER_CALL", 8000, minimum=256),
        ai_max_output_tokens=_int(env, "AI_MAX_OUTPUT_TOKENS", 600, minimum=16),
        # Each upstream call. The engine gives a draft call 20 s and a helper 8 s; 25 s leaves the
        # proxy room for one retry inside that and never outlives a press nobody is waiting for.
        ai_timeout_s=_float(env, "AI_TIMEOUT_S", 25.0),
        # Defaults stay under OpenAI's usage tier 1 monthly limit ($100); raise both after the
        # provider account moves to a higher tier (docs/AI-PROVIDER.md).
        ai_daily_budget_micros=usd_to_micros(_float(env, "AI_DAILY_BUDGET_USD", 10.0)),
        ai_monthly_budget_micros=usd_to_micros(_float(env, "AI_MONTHLY_BUDGET_USD", 90.0)),
        ai_free_trip_pct=_int(env, "AI_FREE_TRIP_PCT", 80, minimum=1),
        # Real web search: OpenAI's `web_search` tool on the Responses endpoint, with the same key.
        # About a cent a search, so the engine asks only when a post carries a checkable,
        # time-sensitive claim, at most once a press (docs/AI-PROVIDER.md). It exists only on the
        # OpenAI path, so it is off by default when AI_PROVIDER is anthropic.
        web_search_enabled=_bool(env, "WEB_SEARCH", ai_provider == "openai"),
        web_search_model=_str(env, "WEB_SEARCH_MODEL", ai_model_fast),
        web_search_context_size=_choice(env, "WEB_SEARCH_CONTEXT_SIZE", ("low", "medium", "high"), "low"),
        web_search_per_account_day=_int(env, "WEB_SEARCH_PER_ACCOUNT_DAY", 20, minimum=0),
        # Background topic research (paid plans only; services/world/search.py paces it to the period).
        web_search_background_per_account_day=_int(env, "WEB_SEARCH_BACKGROUND_PER_ACCOUNT_DAY", 8, minimum=0),
        web_search_timeout_s=_float(env, "WEB_SEARCH_TIMEOUT_S", 8.0),
        plans_file=plans_file,
        past_due_grace_days=_int(env, "PAST_DUE_GRACE_DAYS", 7, minimum=0),
        # Ceilings no single customer should ever meet; 0 switches either one off.
        signup_global_per_hour=_int(env, "SIGNUP_GLOBAL_PER_HOUR", 500, minimum=0),
        max_devices_per_account=_int(env, "MAX_DEVICES_PER_ACCOUNT", 20, minimum=0),
        disposable_domains_file=Path(disposable) if disposable else None,
        stripe_mode=_choice(env, "STRIPE_MODE", STRIPE_MODES, "test"),
        stripe_secret_key=env.get("STRIPE_SECRET_KEY", "").strip(),
        stripe_account_id=_str(env, "STRIPE_ACCOUNT_ID"),
        stripe_webhook_secret=env.get("STRIPE_WEBHOOK_SECRET", "").strip(),
        stripe_webhook_secret_previous=env.get("STRIPE_WEBHOOK_SECRET_PREVIOUS", "").strip(),
        stripe_api_base=_str(env, "STRIPE_API_BASE", "https://api.stripe.com").rstrip("/"),
        stripe_api_version=_str(env, "STRIPE_API_VERSION", "2026-08-26.dahlia"),
        stripe_portal_configuration_id=_str(env, "STRIPE_PORTAL_CONFIGURATION_ID"),
        stripe_portal_upgrade_configuration_id=_str(env, "STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID"),
        stripe_automatic_tax=_bool(env, "STRIPE_AUTOMATIC_TAX", False),
        stripe_tax_code=_str(env, "STRIPE_TAX_CODE"),
        stripe_block_prepaid=_bool(env, "STRIPE_BLOCK_PREPAID", True),
        stripe_allow_promo_codes=_bool(env, "STRIPE_ALLOW_PROMO_CODES", False),
        stripe_allow_test_mode=_bool(env, "STRIPE_ALLOW_TEST_MODE", False),
        engine_max_live=_int(env, "ENGINE_MAX_LIVE", 6, minimum=1),
        # 30 minutes: long enough that someone reading X between drafts does not pay a cold start
        # (2.5-3 s) on every press. Idle engines are still the first evicted when slots run out.
        engine_idle_seconds=_int(env, "ENGINE_IDLE_SECONDS", 1800, minimum=30),
        engine_memory_soft_mb=_int(env, "ENGINE_MEMORY_SOFT_MB", 512, minimum=128),
        engine_memory_hard_mb=_int(env, "ENGINE_MEMORY_HARD_MB", 768, minimum=128),
        engine_uid_base=_int(env, "ENGINE_UID_BASE", 200000, minimum=100000),
        world_offline=_bool(env, "STARLING_WORLD_OFFLINE", False),
        # e2e only: public pages and news stories served from a file while STARLING_WORLD_OFFLINE is set.
        world_fixtures=_str(env, "STARLING_WORLD_FIXTURES"),
        e2e_payment_pages_origin=_str(env, "STARLING_E2E_PAYMENT_PAGES_ORIGIN").rstrip("/"),
        google_client_id=_str(env, "GOOGLE_CLIENT_ID"),
        google_client_secret=env.get("GOOGLE_CLIENT_SECRET", "").strip(),
        e2e_oidc_base=_str(env, "STARLING_E2E_OIDC_BASE").rstrip("/"),
        argon2_time_cost=_int(env, "ARGON2_TIME_COST", 3, minimum=1),
        argon2_memory_kib=_int(env, "ARGON2_MEMORY_KIB", 65536, minimum=8192),
        argon2_parallelism=_int(env, "ARGON2_PARALLELISM", 4, minimum=1),
        argon2_max_concurrent=_int(env, "ARGON2_MAX_CONCURRENT", 4, minimum=1),
        log_level=_str(env, "LOG_LEVEL", "INFO").upper(),
        backup_dir=Path(_str(env, "BACKUP_DIR", str(data_dir / "backups"))),
        backup_passphrase=env.get("BACKUP_PASSPHRASE", ""),
        alert_email=_str(env, "ALERT_EMAIL"),
        services_url=_str(
            env,
            "STARLING_SERVICES_URL",
            default_services_address if isolation == "os" else "http://127.0.0.1:8766",
        ),
        services_listen=_str(env, "STARLING_SERVICES_LISTEN", default_services_address),
        services_token_key=services_key,
        services_token_key_ephemeral=services_key_ephemeral,
        plans=load_plans(plans_file),
        ai_pricing=load_ai_pricing(ai_pricing_file),
    )


def validate(settings: Settings, role: str) -> list[str]:
    """Return every problem that makes `settings` unusable for `role`.

    Callers in production must refuse to start when the list is not empty
    (`ensure_valid`). Development and e2e tolerate missing credentials.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    problems: list[str] = []
    prod = settings.is_production

    if prod:
        if not settings.public_url.startswith("https://"):
            problems.append("STARLING_PUBLIC_URL must use https in production")
        if settings.isolation != "os":
            problems.append("STARLING_ISOLATION must be 'os' in production")
        if not settings.secret_key and role in ("gateway", "cli"):
            problems.append("STARLING_SECRET_KEY is required in production")
        if not settings.services_token_key and role in ("gateway", "services", "supervisor"):
            problems.append("STARLING_SERVICES_TOKEN_KEY is required in production")
        if role == "supervisor" and len(settings.backup_passphrase) < 16:
            problems.append("BACKUP_PASSPHRASE (at least 16 characters) is required in production for encrypted backups")
        # Test endpoints and switches used by the e2e harness must never reach production.
        if role in ("gateway", "cli") and settings.stripe_api_base != "https://api.stripe.com":
            problems.append("STRIPE_API_BASE must be https://api.stripe.com in production")
        if role == "services" and not settings.ai_base_url.startswith("https://"):
            problems.append("AI_BASE_URL must use https in production")
        if settings.world_offline:
            problems.append("STARLING_WORLD_OFFLINE is only for development and e2e")
        if settings.world_fixtures:
            problems.append("STARLING_WORLD_FIXTURES is only for development and e2e")
        if settings.e2e_payment_pages_origin:
            problems.append("STARLING_E2E_PAYMENT_PAGES_ORIGIN is only for development and e2e")
        if settings.e2e_oidc_base:
            problems.append("STARLING_E2E_OIDC_BASE is only for development and e2e")

    if role == "gateway":
        if prod:
            if settings.email_mode != "smtp":
                problems.append("EMAIL_MODE must be 'smtp' in production")
            if not settings.smtp_host or not settings.smtp_from:
                problems.append("SMTP_HOST and SMTP_FROM are required in production")
            if not settings.extension_ids:
                problems.append("STARLING_EXTENSION_IDS must list the published extension id")
            if not settings.owner_emails:
                problems.append("STARLING_OWNER_EMAILS must list at least one owner email")
            if not settings.support_email or settings.support_email.startswith("[["):
                problems.append("STARLING_SUPPORT_EMAIL must be a real support address in production")
            if settings.legal.placeholders():
                problems.append(
                    "business and legal details still contain placeholders: "
                    + ", ".join(settings.legal.placeholders())
                )
        if settings.extension_install_url and not settings.extension_install_url.startswith("https://"):
            problems.append("STARLING_EXTENSION_INSTALL_URL must be an https link to the store listing")
        problems.extend(_google_problems(settings))
        # A free-only launch (a plans file with no paid plan) runs without Stripe.
        problems.extend(_stripe_problems(settings, required=prod and bool(settings.plans.paid_plans())))

    if role == "services":
        if prod and not settings.ai_api_key:
            problems.append("AI_API_KEY is required in production")
        models = {settings.ai_model_quality, settings.ai_model_fast, settings.ai_model_vision}
        if settings.web_search_enabled:
            models.add(settings.web_search_model)
        for model in sorted(models):
            if settings.ai_pricing.price_for(model) is None:
                problems.append(f"no price configured for AI model {model!r} in {settings.ai_pricing_file}")
        if settings.web_search_timeout_s <= 0:
            problems.append("WEB_SEARCH_TIMEOUT_S must be above 0")
        if settings.ai_provider == "anthropic":
            problems.extend(_anthropic_problems(settings))

    if settings.engine_memory_hard_mb < settings.engine_memory_soft_mb:
        problems.append("ENGINE_MEMORY_HARD_MB must be at least ENGINE_MEMORY_SOFT_MB")
    if not prod and not settings.public_url_is_loopback:
        # Not fatal (a developer may test from a phone on the same network), but never silent:
        # outside production none of the checks above run, and customers are not isolated by the OS.
        problems.append(
            f"STARLING_ENV is {settings.env!r} but STARLING_PUBLIC_URL is not this machine: the production "
            "checks are OFF (OS isolation, real email, Stripe verification). A deployment must set STARLING_ENV=production"
        )
    return problems


def _anthropic_problems(settings: Settings) -> list[str]:
    """What AI_PROVIDER=anthropic cannot do with the rest of the AI settings (docs/AI-PROVIDER.md)."""
    problems: list[str] = []
    if settings.web_search_enabled:
        problems.append(
            "WEB_SEARCH must be false when AI_PROVIDER is anthropic: web search uses OpenAI's web_search tool "
            "(Responses API), which the Anthropic path does not provide"
        )
    if (urlsplit(settings.ai_base_url).hostname or "").lower().endswith("openai.com"):
        problems.append("AI_BASE_URL points at OpenAI but AI_PROVIDER is anthropic (unset it to use https://api.anthropic.com)")
    lanes = {
        "AI_MODEL_QUALITY": settings.ai_model_quality,
        "AI_MODEL_FAST": settings.ai_model_fast,
        "AI_MODEL_VISION": settings.ai_model_vision,
    }
    for name, model in lanes.items():
        if not model.startswith("claude-"):
            problems.append(f"{name} is {model!r}, which is not a Claude model, but AI_PROVIDER is anthropic")
    if settings.ai_reasoning_effort and settings.ai_reasoning_effort.lower() not in ANTHROPIC_EFFORTS:
        problems.append(
            f"AI_REASONING_EFFORT must be one of {', '.join(ANTHROPIC_EFFORTS)} when AI_PROVIDER is anthropic"
        )
    if settings.ai_max_input_tokens_per_call > ANTHROPIC_MAX_INPUT_TOKENS_PER_CALL:
        problems.append(
            f"AI_MAX_INPUT_TOKENS_PER_CALL must be at most {ANTHROPIC_MAX_INPUT_TOKENS_PER_CALL} when AI_PROVIDER is "
            "anthropic: prompts over 100,000 tokens cost five times the price in AI_PRICING_FILE"
        )
    return problems


def _google_problems(settings: Settings) -> list[str]:
    """Sign in with Google is optional, but half a configuration is a mistake, not a choice."""
    problems: list[str] = []
    if bool(settings.google_client_id) != bool(settings.google_client_secret):
        problems.append("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set together (or neither, to turn Google sign-in off)")
    if settings.google_client_id and not settings.google_client_id.endswith(".apps.googleusercontent.com"):
        problems.append("GOOGLE_CLIENT_ID should end in .apps.googleusercontent.com (copy the OAuth client ID, not the project ID)")
    if settings.e2e_oidc_base and not settings.is_production:
        host = (urlsplit(settings.e2e_oidc_base).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            problems.append("STARLING_E2E_OIDC_BASE must be a loopback address")
    return problems


def _stripe_problems(settings: Settings, required: bool) -> list[str]:
    problems: list[str] = []
    key = settings.stripe_secret_key
    if not key:
        if required:
            problems.append("STRIPE_SECRET_KEY is required in production")
        return problems
    live_key = key.startswith(("sk_live_", "rk_live_"))
    test_key = key.startswith(("sk_test_", "rk_test_"))
    if not (live_key or test_key):
        problems.append("STRIPE_SECRET_KEY does not look like a Stripe secret or restricted key")
    if settings.stripe_mode == "live" and not live_key:
        problems.append("STRIPE_MODE is 'live' but STRIPE_SECRET_KEY is not a live key")
    if settings.stripe_mode == "test" and not test_key:
        problems.append("STRIPE_MODE is 'test' but STRIPE_SECRET_KEY is not a test key")
    if settings.is_production and settings.stripe_mode != "live" and not settings.stripe_allow_test_mode:
        problems.append(
            "STRIPE_MODE must be 'live' in production (for a pre-launch rehearsal with Stripe's sandbox, "
            "also set STRIPE_ALLOW_TEST_MODE=true; the site then says on every page that payments are not real)"
        )
    if not settings.stripe_webhook_secret.startswith("whsec_"):
        problems.append("STRIPE_WEBHOOK_SECRET must be set (whsec_...) when Stripe is configured")
    return problems


def ensure_valid(settings: Settings, role: str) -> list[str]:
    """Raise in production when there are problems; return them otherwise."""
    problems = validate(settings, role)
    if problems and settings.is_production:
        raise ConfigError("configuration problems:\n- " + "\n- ".join(problems))
    return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
