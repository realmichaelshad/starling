"""plans.toml is the one file a new owner is certain to edit: the server must refuse the edits that
would start without a word and then fail every customer on the plan."""
from __future__ import annotations

from pathlib import Path

import pytest

from starling_server.config import DEFAULT_PLANS_FILE, ConfigError, load_plans

GOOD = """
[unit]
name = "draft"
label = "drafts"

[plans.free]
name = "Free"
monthly_credits = 25
cost_ceiling_usd = 0.30
rate_per_minute = 3
rate_per_day = 10

[plans.starter]
name = "Starter"
monthly_credits = 250
stripe_lookup_key = "starter_monthly"
amount_cents = 900
currency = "usd"
interval = "month"
cost_ceiling_usd = 4.50
rate_per_minute = 6
rate_per_day = 60

[plans.pro]
name = "Pro"
monthly_credits = 800
stripe_lookup_key = "pro_monthly"
amount_cents = 1900
currency = "usd"
interval = "month"
cost_ceiling_usd = 9.00
rate_per_minute = 10
rate_per_day = 150
"""


def load(tmp_path: Path, text: str):
    path = tmp_path / "plans.toml"
    path.write_text(text, encoding="utf-8")
    return load_plans(path)


def test_the_shipped_plans_file_and_the_reference_text_both_load(tmp_path):
    assert {"free", "starter", "pro"} <= set(load_plans(DEFAULT_PLANS_FILE).by_code)
    assert load(tmp_path, GOOD).get("pro").monthly_credits == 800


@pytest.mark.parametrize(
    ("find", "replace", "expected"),
    [
        # Every request on the plan would be refused as rate_limited, for ever.
        ("rate_per_minute = 6", "rate_per_minute = 0", "plan 'starter': rate_per_minute and rate_per_day must be at least 1"),
        ("rate_per_day = 10", "rate_per_day = -5", "plan 'free': rate_per_minute and rate_per_day must be at least 1"),
        # Spend (0) has already reached a ceiling of 0: every request refused as fair_use_exceeded.
        ("cost_ceiling_usd = 4.50", "cost_ceiling_usd = 0", "plan 'starter': cost_ceiling_usd must be above 0"),
        # Money for nothing.
        ("monthly_credits = 250", "monthly_credits = 25", "paid plan 'starter': monthly_credits (25) must be more than the free plan's (25)"),
        ("amount_cents = 900", "amount_cents = 0", "paid plan 'starter': amount_cents must be at least 1"),
        ("currency = \"usd\"\ninterval = \"month\"\ncost_ceiling_usd = 4.50", "currency = \"dollars\"\ninterval = \"month\"\ncost_ceiling_usd = 4.50",
         "paid plan 'starter': currency must be a three-letter code"),
        # A payment could not be matched to one plan.
        ("stripe_lookup_key = \"pro_monthly\"", "stripe_lookup_key = \"starter_monthly\"", "stripe_lookup_key 'starter_monthly' is used by more than one plan"),
        ("name = \"Pro\"", "name = \"Starter\"", "plan name 'starter' is used by more than one plan"),
        # An upgrade that takes drafts away.
        ("monthly_credits = 800", "monthly_credits = 100", "plan 'pro' costs more than 'starter' but allows fewer"),
    ],
)
def test_an_edit_that_would_break_every_customer_on_the_plan_is_refused(tmp_path, find, replace, expected):
    assert GOOD.count(find) == 1, find
    with pytest.raises(ConfigError) as refused:
        load(tmp_path, GOOD.replace(find, replace))
    assert expected in str(refused.value)


def test_every_problem_is_reported_at_once(tmp_path):
    broken = GOOD.replace("rate_per_minute = 6", "rate_per_minute = 0").replace("amount_cents = 1900", "amount_cents = 0")
    with pytest.raises(ConfigError) as refused:
        load(tmp_path, broken)
    message = str(refused.value)
    assert "plan 'starter': rate_per_minute" in message and "paid plan 'pro': amount_cents" in message


def test_a_free_plan_with_no_drafts_at_all_is_a_legitimate_choice(tmp_path):
    """An owner may switch free drafting off: no allowance, and then no ceiling is needed either."""
    no_free_drafts = GOOD.replace("monthly_credits = 25\ncost_ceiling_usd = 0.30", "monthly_credits = 0\ncost_ceiling_usd = 0")
    assert load(tmp_path, no_free_drafts).free.monthly_credits == 0
