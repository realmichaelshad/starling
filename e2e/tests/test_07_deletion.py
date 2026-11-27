"""Journey 7: taking everything away (local, mocked providers).

The customer exports their data, then deletes the account. Deletion has to finish four things:
cancel the subscription at Stripe, remove the engine's data folder, anonymise the account row
and leave a tombstone, and sign every device out. Each one is checked where it really happened
- the fake Stripe's own state, the file system, the control database and the API.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from e2e.tests import billing_flow
from e2e.tests.client import Control, stream_body

DRAFT_COMPOSER = {
    "action": "post",
    "draft_text": "I finally wrote down why we keep the old migration files around.",
    "intent": "enhance",
}
MATERIAL = "I write short posts about build systems and the tools I actually use every week."


@pytest.fixture
def doomed(ready_account, harness: Control):
    """A customer with a subscription, an engine folder with data in it, and a device."""
    account = ready_account("deletion")
    if not account.extras.get("prepared"):
        billing_flow.subscribe(account, harness, "starter")
        added = account.device.post("/own-material", {"kind": "voice", "text": MATERIAL})
        assert added.status_code == 200, added.text[:300]
        drafted = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
        assert drafted.final is not None, drafted.events
        account.extras["prepared"] = True
    return account


def tenant_folder(stack: dict, harness: Control, account) -> Path:
    seq = harness.scalar("SELECT tenant_seq FROM accounts WHERE id = ?", (account.id,))
    return Path(stack["data_dir"]) / "tenants" / str(seq)


def test_the_export_carries_both_halves_of_the_customers_data(doomed, harness: Control):
    account = doomed
    response = account.site.post_form("/account/export", {})
    assert response.status_code == 200, response.text[:300]
    assert "application/json" in response.headers.get("content-type", "")
    assert "attachment" in response.headers.get("content-disposition", "")
    body = json.loads(response.text)
    assert isinstance(body, dict) and body, "the export is empty"
    flat = json.dumps(body)
    assert account.email in flat, "the export does not mention the account it belongs to"
    assert MATERIAL in flat, "the engine's own material is missing from the export"


def test_the_export_carries_nothing_the_server_keeps_secret(doomed, harness: Control):
    """The download is a file a customer will store and forward. It must not carry a password
    hash, a session id, a device token digest or a TOTP secret."""
    account = doomed
    response = account.site.post_form("/account/export", {})
    assert response.status_code == 200, response.text[:300]
    body = response.text

    secret_values = []
    for sql in (
        "SELECT password_hash AS v FROM accounts WHERE id = ?",
        "SELECT totp_secret_enc AS v FROM accounts WHERE id = ?",
        "SELECT id_hash AS v FROM web_sessions WHERE account_id = ?",
        "SELECT csrf_secret AS v FROM web_sessions WHERE account_id = ?",
        "SELECT token_hash AS v FROM devices WHERE account_id = ?",
        "SELECT token_hash AS v FROM email_tokens WHERE account_id = ?",
    ):
        secret_values.extend(row["v"] for row in harness.rows(sql, (account.id,)) if row["v"])
    assert secret_values, "the test needs something to look for"
    for value in secret_values:
        assert value not in body, "the export leaked a stored secret"
    for field in ("password_hash", "token_hash", "id_hash", "csrf_secret", "totp_secret_enc"):
        assert field not in body, field


def test_deleting_the_account_cancels_the_subscription_and_removes_everything(
    doomed, harness: Control, stack: dict
):
    account = doomed
    customer = billing_flow.customer_of(harness, account)
    subscription = billing_flow.subscription_of(harness, account)["subscription_id"]
    folder = tenant_folder(stack, harness, account)
    assert folder.is_dir() and any(folder.iterdir()), "the engine wrote nothing to delete"
    token_still_works = account.device.get("/session")
    assert token_still_works.status_code == 200  # negative control before the deletion

    response = account.site.post_form(
        "/account/delete", {"delete_password": account.password, "delete_confirmation": "DELETE"}
    )
    assert response.status_code == 303, response.text[:300]
    assert response.headers["location"] == "/goodbye"  # the page that says what happened

    # The device token is refused at once.
    after = account.device.get("/session")
    assert after.status_code == 401 and after.json()["error"]["code"] == "signed_out"

    # The account row is anonymised and a tombstone is left behind.
    row = harness.wait_for(
        lambda: _deleted_row(harness, account.id), 60.0, "the account row to be anonymised"
    )
    assert row["status"] == "deleted"
    assert account.email.lower() not in (row["email_norm"] or "")
    assert row["email"].startswith("deleted-") and row["email"].endswith("@invalid")
    assert harness.rows("SELECT id FROM devices WHERE account_id = ?", (account.id,)) == []
    assert harness.rows("SELECT id_hash FROM web_sessions WHERE account_id = ?", (account.id,)) == []
    assert harness.one("SELECT account_hash FROM tombstones LIMIT 1") is not None

    # The subscription is cancelled at Stripe, not just forgotten locally.
    state = harness.stripe_get(f"/state?customer={customer}")
    cancelled = [s for s in state["subscriptions"] if s["id"] == subscription]
    assert cancelled and cancelled[0]["status"] in ("canceled", "incomplete_expired"), cancelled

    # The engine's data folder is gone.
    harness.wait_for(lambda: not folder.exists(), 60.0, f"{folder} to be removed")
    assert harness.one("SELECT account_id FROM engines WHERE account_id = ?", (account.id,)) is None


def _deleted_row(harness: Control, account_id: str) -> dict | None:
    row = harness.one("SELECT status, email, email_norm FROM accounts WHERE id = ?", (account_id,))
    return row if row is not None and row["status"] == "deleted" else None


def test_signing_in_again_with_the_deleted_address_is_refused(doomed, harness: Control, site):
    account = doomed
    response = site.login(account.email)
    assert response.status_code != 303, "a deleted account must not sign in"
    assert site.get("/account").status_code in (302, 303)


def test_negative_control_the_deletion_checks_can_fail(ready_account, harness: Control, stack: dict):
    """A live account fails every check the deleted one passes."""
    account = ready_account("drafting")
    assert account.device.get("/session").status_code == 200
    row = harness.one("SELECT status FROM accounts WHERE id = ?", (account.id,))
    assert row["status"] != "deleted"
    with pytest.raises(AssertionError):
        assert _deleted_row(harness, account.id) is not None
