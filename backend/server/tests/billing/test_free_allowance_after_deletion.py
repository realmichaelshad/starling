"""A deleted address gets no second free allowance (CONTRACTS 7.1 and 11)."""
from __future__ import annotations

from starling_server.account import deletion
from starling_server.auth import accounts
from starling_server.billing.entitlements import current_entitlement

NOW = 1_800_000_000


def _sign_up(database, email: str):
    return database.write(
        lambda conn: accounts.insert_account(conn, email=email, password_hash="!", ip_prefix=None, now=NOW)
    )


def test_reusing_a_deleted_address_starts_at_zero(database, settings):
    free = settings.plans.free
    first = _sign_up(database, "reuse@example.com")
    assert current_entitlement(database, settings, first.id, NOW).allowance == free.monthly_credits

    database.write(lambda conn: deletion.anonymize(conn, settings, first.id, NOW))
    second = _sign_up(database, "reuse@example.com")
    again = current_entitlement(database, settings, second.id, NOW)
    assert again.allowance == 0 and again.cost_ceiling_micros == 0
    assert again.plan_code == free.code and again.status == "free"

    # Negative control: another address still gets the full free allowance.
    other = _sign_up(database, "someone-else@example.com")
    assert current_entitlement(database, settings, other.id, NOW).allowance == free.monthly_credits


def test_the_tag_and_dot_variants_of_a_deleted_address_are_covered(database, settings):
    first = _sign_up(database, "person@gmail.com")
    database.write(lambda conn: deletion.anonymize(conn, settings, first.id, NOW))
    variant = _sign_up(database, "p.e.r.s.o.n+later@googlemail.com")
    assert current_entitlement(database, settings, variant.id, NOW).allowance == 0
