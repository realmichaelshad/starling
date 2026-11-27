"""`stripe-setup` and `stripe-verify` (CONTRACTS sections 7.2 and 8).

Setup is idempotent: prices are found by lookup key (created only when no active price has
the key, with `transfer_lookup_key` so an archived holder gives the key up), one product
holds every paid price, and the two portal configurations are found by
`metadata.starling_setup` + `metadata.starling_role`:

* `manage` (STRIPE_PORTAL_CONFIGURATION_ID): plan switching off, cancel at period end on,
  payment method update, invoice history, address and tax id on, email off.
* `upgrade` (STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID): the same plus subscription updates
  limited to the paid prices with `proration_behavior=always_invoice`. It is only used for
  `subscription_update_confirm` deep links, because Stripe requires the target price to be
  listed in the configuration's `features.subscription_update.products`.

Existing prices are never changed. Verify compares the Stripe prices with plans.toml
(amount, currency, interval), the key prefix with STRIPE_MODE, the account id (skipped
with a warning when a restricted key may not read the account) and the portal
configurations.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..config import Settings
from ..db import Database
from . import mirror
from .stripe_client import StripeClient, StripePermissionError, object_id

SETUP_VERSION = "1"
ROLE_MANAGE = "manage"
ROLE_UPGRADE = "upgrade"
PRODUCT_MARKER = "starling"


@dataclass
class SetupReport:
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class VerifyReport:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    account_id: str | None = None
    prices: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def key_mode_problems(settings: Settings) -> list[str]:
    key = settings.stripe_secret_key
    if settings.stripe_mode == "live" and not key.startswith(("sk_live_", "rk_live_")):
        return ["STRIPE_MODE is 'live' but STRIPE_SECRET_KEY is not a live key"]
    if settings.stripe_mode == "test" and not key.startswith(("sk_test_", "rk_test_")):
        return ["STRIPE_MODE is 'test' but STRIPE_SECRET_KEY is not a test key"]
    return []


def _account_problems(settings: Settings, account: Mapping[str, Any]) -> list[str]:
    if settings.stripe_account_id and account.get("id") != settings.stripe_account_id:
        return [f"STRIPE_ACCOUNT_ID is {settings.stripe_account_id} but the key belongs to account {account.get('id')}"]
    return []


def _retrieve_account(client: StripeClient) -> tuple[dict[str, Any] | None, str | None]:
    """`GET /v1/account`, or (None, warning) when a restricted key may not read the account."""
    try:
        return client.retrieve_account(), None
    except StripePermissionError:
        return None, "the Stripe key may not read the account (restricted key); the STRIPE_ACCOUNT_ID check was skipped"


def _active_prices(settings: Settings, client: StripeClient) -> dict[str, dict[str, Any]]:
    keys = [plan.stripe_lookup_key for plan in settings.plans.paid_plans()]
    return {price["lookup_key"]: price for price in client.list_prices(lookup_keys=keys, active=True) if price.get("lookup_key")}


def _metadata(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    return obj.get("metadata") or {}


def portal_params(settings: Settings, role: str, prices: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    public = settings.public_url
    params: dict[str, Any] = {
        "name": "Starling billing" if role == ROLE_MANAGE else "Starling upgrades",
        "default_return_url": f"{public}/account/billing",
        "features": {
            "customer_update": {"enabled": True, "allowed_updates": ["address", "tax_id"]},
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end", "proration_behavior": "none"},
            "subscription_update": {"enabled": False},
        },
        "metadata": {"starling_setup": SETUP_VERSION, "starling_role": role},
    }
    if public.startswith("https://"):
        params["business_profile"] = {"privacy_policy_url": f"{public}/privacy", "terms_of_service_url": f"{public}/terms"}
    if role == ROLE_UPGRADE:
        by_product: dict[str, list[str]] = {}
        for price in prices.values():
            by_product.setdefault(object_id(price.get("product")) or "", []).append(price["id"])
        params["features"]["subscription_update"] = {
            "enabled": True,
            "default_allowed_updates": ["price"],
            "proration_behavior": "always_invoice",
            "products": [{"product": product, "prices": ids} for product, ids in by_product.items()],
            "schedule_at_period_end": {"conditions": [{"type": "decreasing_item_amount"}]},
        }
    return params


def portal_problems(config: Mapping[str, Any], role: str, price_ids: Mapping[str, str]) -> list[str]:
    label = f"portal configuration {config.get('id')}"
    features = config.get("features") or {}
    problems: list[str] = []
    if not (features.get("payment_method_update") or {}).get("enabled"):
        problems.append(f"{label}: payment method update must be on")
    if not (features.get("invoice_history") or {}).get("enabled"):
        problems.append(f"{label}: invoice history must be on")
    cancel = features.get("subscription_cancel") or {}
    if not cancel.get("enabled") or cancel.get("mode") != "at_period_end":
        problems.append(f"{label}: cancellation must be on and take effect at period end")
    customer = features.get("customer_update") or {}
    allowed = set(customer.get("allowed_updates") or [])
    if not customer.get("enabled") or not {"address", "tax_id"} <= allowed or "email" in allowed:
        problems.append(f"{label}: address and tax id updates must be on and email updates off")
    update = features.get("subscription_update") or {}
    if role == ROLE_MANAGE and update.get("enabled"):
        problems.append(f"{label}: plan switching must be off")
    if role == ROLE_UPGRADE:
        if not update.get("enabled") or update.get("proration_behavior") != "always_invoice":
            problems.append(f"{label}: subscription updates must be on with proration always_invoice")
        offered = {price for product in update.get("products") or [] for price in product.get("prices") or []}
        missing = sorted(code for code, price_id in price_ids.items() if price_id not in offered)
        if missing:
            problems.append(f"{label}: does not offer the current price of {', '.join(missing)}")
    return problems


def verify(db: Database, settings: Settings, client: StripeClient) -> VerifyReport:
    report = VerifyReport(problems=key_mode_problems(settings))
    account, warning = _retrieve_account(client)
    if warning:
        report.warnings.append(warning)
    else:
        report.account_id = account.get("id")
        report.problems.extend(_account_problems(settings, account))
        if settings.is_production and not settings.stripe_account_id:
            report.problems.append("STRIPE_ACCOUNT_ID must be set in production")
    prices = _active_prices(settings, client)
    matched: list[tuple[Mapping[str, Any], str]] = []
    for plan in settings.plans.paid_plans():
        price = prices.get(plan.stripe_lookup_key)
        if price is None:
            report.problems.append(f"{plan.code}: no active Stripe price has lookup key {plan.stripe_lookup_key!r} (run stripe-setup)")
            continue
        issues = mirror.price_problems(price, plan)
        if bool(price.get("livemode")) != mirror.livemode(settings):
            issues.append(f"{plan.code}: price {price['id']} belongs to the other Stripe mode")
        report.problems.extend(issues)
        if not issues:
            matched.append((price, plan.code))
            report.prices[plan.code] = price["id"]
    configs = {config["id"]: config for config in client.list_portal_configurations()}
    for name, config_id, role in (
        ("STRIPE_PORTAL_CONFIGURATION_ID", settings.stripe_portal_configuration_id, ROLE_MANAGE),
        ("STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID", settings.stripe_portal_upgrade_configuration_id, ROLE_UPGRADE),
    ):
        if not config_id:
            if settings.is_production:
                report.problems.append(f"{name} must be set in production (run stripe-setup)")
            continue
        config = configs.get(config_id)
        if config is None or not config.get("active"):
            report.problems.append(f"{name} {config_id} is not an active portal configuration")
        else:
            report.problems.extend(portal_problems(config, role, report.prices))
    if matched:
        db.write(lambda conn: [mirror.upsert_price(conn, price, code) for price, code in matched])
    return report


def run_setup(db: Database, settings: Settings, client: StripeClient) -> SetupReport:
    report = SetupReport(problems=key_mode_problems(settings))
    account, warning = _retrieve_account(client)
    if warning:
        report.warnings.append(warning)
    else:
        report.problems.extend(_account_problems(settings, account))
        report.env["STRIPE_ACCOUNT_ID"] = account["id"]
    if report.problems:
        return report
    existing = _active_prices(settings, client)
    product_id = next((object_id(price.get("product")) for price in existing.values() if price.get("product")), None)
    plan_prices: dict[str, Mapping[str, Any]] = {}
    for plan in settings.plans.paid_plans():
        price = existing.get(plan.stripe_lookup_key)
        if price is not None:
            issues = mirror.price_problems(price, plan)
            if issues:
                report.problems.extend(issues)
                report.problems.append(
                    f"{plan.code}: price {price['id']} was left unchanged; archive it in Stripe and run stripe-setup again"
                )
                continue
            report.reused.append(f"price {price['id']} ({plan.code})")
        else:
            if product_id is None:
                product_id = _ensure_product(settings, client, report)
            params: dict[str, Any] = {
                "currency": plan.currency,
                "unit_amount": plan.amount_cents,
                "recurring": {"interval": plan.interval, "interval_count": 1},
                "product": product_id,
                "lookup_key": plan.stripe_lookup_key,
                "transfer_lookup_key": True,
                "nickname": f"Starling {plan.name}",
                "metadata": {"starling_setup": SETUP_VERSION, "starling_plan": plan.code},
            }
            if settings.stripe_automatic_tax:
                params["tax_behavior"] = "exclusive"
            price = client.create_price(params, idempotency_key=f"starling-setup-{uuid.uuid4()}")
            report.created.append(f"price {price['id']} ({plan.code})")
        plan_prices[plan.code] = price
    if plan_prices:
        db.write(lambda conn: [mirror.upsert_price(conn, price, code) for code, price in plan_prices.items()])

    ours = [config for config in client.list_portal_configurations(active=True) if _metadata(config).get("starling_setup")]
    manage = next((c for c in ours if _metadata(c).get("starling_role") == ROLE_MANAGE), None)
    if manage is None:
        manage = client.create_portal_configuration(portal_params(settings, ROLE_MANAGE, {}))
        report.created.append(f"portal configuration {manage['id']} (management)")
    else:
        report.reused.append(f"portal configuration {manage['id']} (management)")
        report.problems.extend(portal_problems(manage, ROLE_MANAGE, {}))
    report.env["STRIPE_PORTAL_CONFIGURATION_ID"] = manage["id"]

    if len(plan_prices) != len(settings.plans.paid_plans()):
        report.problems.append("the upgrade portal configuration needs every paid plan price; fix the prices first")
        return report
    price_ids = {code: price["id"] for code, price in plan_prices.items()}
    upgrade = next(
        (c for c in ours if _metadata(c).get("starling_role") == ROLE_UPGRADE and not portal_problems(c, ROLE_UPGRADE, price_ids)),
        None,
    )
    if upgrade is None:
        upgrade = client.create_portal_configuration(portal_params(settings, ROLE_UPGRADE, plan_prices))
        report.created.append(f"portal configuration {upgrade['id']} (upgrades)")
    else:
        report.reused.append(f"portal configuration {upgrade['id']} (upgrades)")
    report.env["STRIPE_PORTAL_UPGRADE_CONFIGURATION_ID"] = upgrade["id"]
    return report


def _ensure_product(settings: Settings, client: StripeClient, report: SetupReport) -> str:
    for product in client.list_products(active=True):
        metadata = _metadata(product)
        if metadata.get("starling_product") == PRODUCT_MARKER and metadata.get("starling_setup"):
            report.reused.append(f"product {product['id']}")
            return product["id"]
    params: dict[str, Any] = {
        "name": "Starling",
        "metadata": {"starling_setup": SETUP_VERSION, "starling_product": PRODUCT_MARKER},
    }
    if settings.stripe_tax_code:
        params["tax_code"] = settings.stripe_tax_code
    product = client.create_product(params, idempotency_key=f"starling-setup-{uuid.uuid4()}")
    report.created.append(f"product {product['id']}")
    return product["id"]
