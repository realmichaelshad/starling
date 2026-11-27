"""Admin credit grants and debits (usage.grants)."""
from __future__ import annotations

import pytest

from starling_server.usage import grants, service
from starling_server.usage.service import Reservation, UsageError

from .conftest import NOW, body_hash, counters, ledger, make_account, new_key, record_ai_call


def reserve(db, s, account, now=NOW):
    return service.reserve(db, s, account, new_key(), body_hash(), "/api/v1/assist", "context", now)


def test_grant_raises_remaining_and_is_ledgered(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    period = fake_entitlements.period_id(account)
    fake_entitlements.set(account, allowance=1)
    spent = reserve(db, s, account)
    service.activate(db, spent.id, NOW)
    record_ai_call(db, spent.id)
    assert service.finish(db, s, spent.id, True, "{}", NOW)["remaining"] == 0
    with pytest.raises(UsageError) as caught:
        reserve(db, s, account)
    assert caught.value.code == "limit_reached"

    result = grants.grant_credits(db, s, account, 5, "admin-1", "Support goodwill", NOW + 10)
    assert (result["kind"], result["amount"], result["granted"], result["period_id"]) == ("admin_grant", 5, 5, period)
    assert (result["usage"]["allowance"], result["usage"]["used"], result["usage"]["remaining"]) == (1, 1, 5)
    [row] = ledger(db, account, "admin_grant")
    assert (row["id"], row["amount"], row["actor"], row["reason"], row["reservation_id"]) == (
        result["ledger_id"], 5, "admin-1", "Support goodwill", None)
    assert isinstance(reserve(db, s, account, NOW + 11), Reservation)
    assert service.account_state(db, s, account, NOW + 12)["usage"]["remaining"] == 4
    assert service.verify_counters(db) == []


def test_debit_lowers_remaining(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    grants.grant_credits(db, s, account, 10, "admin-1", "Promotion", NOW)
    result = grants.debit_credits(db, s, account, 4, "admin-1", "Correction", NOW + 1)
    assert (result["kind"], result["granted"], result["usage"]["remaining"]) == ("admin_debit", 6, 256)
    assert [(row["kind"], row["amount"]) for row in ledger(db, account)] == [("admin_grant", 10), ("admin_debit", 4)]
    assert counters(db, account, fake_entitlements.period_id(account))["granted"] == 6
    assert service.verify_counters(db) == []


@pytest.mark.parametrize("amount", [0, -1, 10_001, True, 2.5, "5", None])
def test_amount_is_bounded(database, roomy_settings, fake_entitlements, amount):
    db, s = database, roomy_settings
    account = make_account(db, s)
    for change in (grants.grant_credits, grants.debit_credits):
        with pytest.raises(UsageError) as caught:
            change(db, s, account, amount, "admin-1", "Reason", NOW)
        assert (caught.value.http_status, caught.value.code) == (400, "bad_request")
    assert ledger(db, account) == []


def test_limits_and_required_fields(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    account = make_account(db, s)
    assert grants.grant_credits(db, s, account, 10_000, "admin-1", "Largest single grant", NOW)["granted"] == 10_000
    assert grants.debit_credits(db, s, account, 1, "admin-1", "Smallest debit", NOW)["granted"] == 9_999
    for actor, reason in (("admin-1", ""), ("admin-1", "   "), ("", "Reason"), ("admin-1", "x" * 501)):
        with pytest.raises(UsageError) as caught:
            grants.grant_credits(db, s, account, 1, actor, reason, NOW)
        assert caught.value.http_status == 400
    with pytest.raises(UsageError) as caught:
        grants.grant_credits(db, s, "00000000-0000-4000-8000-000000000000", 1, "admin-1", "Reason", NOW)
    assert caught.value.http_status == 404
    assert len(ledger(db, account)) == 2
