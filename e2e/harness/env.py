"""The environment every process of one run gets.

Credentials here are fakes for two local servers, never anything that could reach a real
provider. Each is assembled from pieces at run time and is random per run, so no value that
looks like a provider key is ever written into a source file (`scripts/check_release.py`).
"""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from .paths import AI_PRICING_FILE, PLANS_FILE, WEB_DIR

# Chrome extension ids are 32 letters from a to p. Part 2 loads an unpacked extension, whose
# id Chrome derives from its key, so a browser run overrides this (`Stack(extension_id=...)`,
# `--extension-id`, or `E2E_EXTENSION_ID`).
E2E_EXTENSION_ID = "efakeidefakeidefakeidefakeidefak"
EXTENSION_ID_ENV = "E2E_EXTENSION_ID"
# Part 2 compiles the extension against one fixed origin, so a browser run pins the gateway
# port (`Stack(gateway_port=...)`, `--gateway-port`, or `E2E_GATEWAY_PORT`). API runs leave it
# unset and take a free port.
GATEWAY_PORT_ENV = "E2E_GATEWAY_PORT"
OWNER_EMAIL = "owner@starling.example"
SUPPORT_EMAIL = "support@starling.example"
CONSENT_VERSION = "2026-09-22.1"
EXTENSION_MIN_VERSION = "1.0.0"
EXTENSION_VERSION = "1.0.0"

# Small enough that a journey can exhaust one on purpose by restarting the services process
# with a lower value, large enough that ordinary journeys never trip it (a draft costs about
# $0.004 with the default model).
DAILY_BUDGET_USD = "0.50"
MONTHLY_BUDGET_USD = "5.00"


def b64key(size: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(size)).decode().rstrip("=")


def fake_ai_key() -> str:
    """An OpenAI-shaped key for the local fake. Assembled at run time on purpose."""
    return "sk" + "-" + "e2e" + "-" + secrets.token_hex(16)


def fake_stripe_key() -> str:
    return "sk" + "_test_" + "e2e" + secrets.token_hex(10)


def fake_webhook_secret() -> str:
    return "whsec" + "_" + "e2e" + secrets.token_hex(12)


# The stand-in Google's OAuth client (e2e/harness/fake_google.py). The id has Google's shape, so the
# gateway's configuration check accepts it; the secret is made per run.
GOOGLE_CLIENT_ID = "e2e-starling.apps.googleusercontent.com"


def fake_google_secret() -> str:
    return "e2e-google-" + secrets.token_hex(12)


def extension_id(override: str | None = None) -> str:
    """The extension id the gateway trusts for this run."""
    return (override or os.environ.get(EXTENSION_ID_ENV) or E2E_EXTENSION_ID).strip()


def gateway_port(override: int | None = None) -> int | None:
    """A pinned gateway port, or None to take a free one."""
    if override:
        return int(override)
    raw = os.environ.get(GATEWAY_PORT_ENV, "").strip()
    return int(raw) if raw.isdigit() else None


# A run that should exercise the world tools offline (e2e/eval) names a fixture file here: the
# services process then serves its pages and news stories instead of failing every fetch
# (services/world/fixtures.py; only while STARLING_WORLD_OFFLINE is set, refused in production).
WORLD_FIXTURES_ENV = "E2E_WORLD_FIXTURES"


def world_fixtures() -> dict[str, str]:
    path = os.environ.get(WORLD_FIXTURES_ENV, "").strip()
    return {"STARLING_WORLD_FIXTURES": path} if path else {}


def base_environment(
    *,
    data_dir: Path,
    public_url: str,
    secret_key: str,
    services_token_key: str,
    ai_base_url: str,
    ai_api_key: str,
    stripe_api_base: str,
    stripe_secret_key: str,
    stripe_webhook_secret: str,
    extension_ids: str | None = None,
) -> dict[str, str]:
    """Settings shared by the gateway, the services process and the operator commands."""
    return {
        "STARLING_ENV": "e2e",
        "STARLING_ISOLATION": "none",
        "STARLING_PUBLIC_URL": public_url,
        "STARLING_DATA_DIR": str(data_dir),
        "STARLING_WEB_DIR": str(WEB_DIR),
        "STARLING_SECRET_KEY": secret_key,
        "STARLING_SERVICES_TOKEN_KEY": services_token_key,
        # Production runs behind a TLS proxy, so the gateway is told to trust one. It lets the
        # journeys give each simulated customer its own network prefix through
        # `X-Forwarded-For`, exactly as real customers have one: without it every account in a
        # run shares 127.0.0.1 and the per-prefix sign-up limit would cap the suite at ten.
        "STARLING_TRUSTED_PROXIES": "127.0.0.1",
        "STARLING_EXTENSION_IDS": extension_id(extension_ids),
        "STARLING_OWNER_EMAILS": OWNER_EMAIL,
        "STARLING_SUPPORT_EMAIL": SUPPORT_EMAIL,
        "STARLING_CONSENT_VERSION": CONSENT_VERSION,
        "STARLING_EXTENSION_MIN_VERSION": EXTENSION_MIN_VERSION,
        "STARLING_WORLD_OFFLINE": "true",
        **world_fixtures(),
        "EMAIL_MODE": "outbox",
        "PLANS_FILE": str(PLANS_FILE),
        "AI_PRICING_FILE": str(AI_PRICING_FILE),
        "AI_BASE_URL": ai_base_url,
        "AI_API_KEY": ai_api_key,
        "AI_DAILY_BUDGET_USD": DAILY_BUDGET_USD,
        "AI_MONTHLY_BUDGET_USD": MONTHLY_BUDGET_USD,
        "AI_FREE_TRIP_PCT": "80",
        "AI_TIMEOUT_S": "30",
        "STRIPE_MODE": "test",
        "STRIPE_API_BASE": stripe_api_base,
        "STRIPE_SECRET_KEY": stripe_secret_key,
        "STRIPE_WEBHOOK_SECRET": stripe_webhook_secret,
        "STRIPE_BLOCK_PREPAID": "true",
        "ENGINE_MAX_LIVE": "6",
        "ENGINE_IDLE_SECONDS": "600",
        # Sign-up and sign-in do a password hash per call; the production cost would make the
        # journeys slow without testing anything the unit tests do not already cover.
        "ARGON2_TIME_COST": "1",
        "ARGON2_MEMORY_KIB": "8192",
        "ARGON2_PARALLELISM": "1",
        "LOG_LEVEL": "INFO",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }


# Inherited from the parent process so Python and the OS still work; nothing else leaks in.
INHERIT = (
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP", "TMPDIR",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "HOME", "LANG", "LC_ALL", "USERPROFILE",
)


def process_environment(extra: dict[str, str]) -> dict[str, str]:
    env = {name: os.environ[name] for name in INHERIT if name in os.environ}
    env.update({key: str(value) for key, value in extra.items()})
    return env
