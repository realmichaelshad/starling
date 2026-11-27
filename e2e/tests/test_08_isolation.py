"""Journey 8: two customers at once never see each other (local, mocked providers).

Each customer gets a process of their own and a folder of their own, and the material one of
them types into settings is invisible to the other. The two drafts are pressed at the same
time, from two threads, because a shared engine would be easiest to miss when the presses are
sequential.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from e2e.tests.client import Control, stream_body

ALICE_MATERIAL = "ALICE-CANARY: I write about compiler warnings and the ones worth turning into errors."
BOB_MATERIAL = "BOB-CANARY: I write about warehouse logistics and the software that keeps pallets moving."
ALICE_DRAFT = {"action": "post", "draft_text": "Turning one warning into an error found three real bugs.",
               "intent": "enhance"}
BOB_DRAFT = {"action": "post", "draft_text": "We re-slotted the pick faces and shaved an hour off every shift.",
             "intent": "enhance"}


@pytest.fixture
def two_customers(ready_account, harness: Control):
    alice = ready_account("tenant-a")
    bob = ready_account("tenant-b")
    for account, material in ((alice, ALICE_MATERIAL), (bob, BOB_MATERIAL)):
        if not account.extras.get("material"):
            added = account.device.post("/own-material", {"kind": "voice", "text": material})
            assert added.status_code == 200, added.text[:300]
            account.extras["material"] = material
    return alice, bob


def engine_row(harness: Control, account) -> dict:
    return harness.wait_for(
        lambda: harness.one("SELECT * FROM engines WHERE account_id = ? AND state = 'running'", (account.id,)),
        60.0,
        f"an engine for {account.email}",
    )


def test_two_customers_drafting_at_the_same_time_get_their_own_engine(
    two_customers, harness: Control, stack: dict
):
    alice, bob = two_customers
    results: dict[str, object] = {}
    lock = threading.Lock()

    def draft(name: str, account, composer: dict) -> None:
        outcome = account.device.stream(stream_body("enhance_composer", composer=composer))
        with lock:
            results[name] = outcome

    workers = [
        threading.Thread(target=draft, args=("alice", alice, ALICE_DRAFT), daemon=True),
        threading.Thread(target=draft, args=("bob", bob, BOB_DRAFT), daemon=True),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=300)

    for name in ("alice", "bob"):
        assert results[name].final is not None, results[name].events

    first, second = engine_row(harness, alice), engine_row(harness, bob)
    assert first["pid"] != second["pid"], "the two customers shared one engine process"
    assert first["tenant_seq"] != second["tenant_seq"]
    assert first["listen"] != second["listen"]

    tenants = Path(stack["data_dir"]) / "tenants"
    alice_dir, bob_dir = tenants / str(first["tenant_seq"]), tenants / str(second["tenant_seq"])
    assert alice_dir.is_dir() and bob_dir.is_dir() and alice_dir != bob_dir
    assert _contains(alice_dir, "ALICE-CANARY") and not _contains(alice_dir, "BOB-CANARY")
    assert _contains(bob_dir, "BOB-CANARY") and not _contains(bob_dir, "ALICE-CANARY")


def _contains(folder: Path, needle: str) -> bool:
    """Whether any file under `folder` holds `needle` (the customer's own canary)."""
    marker = needle.encode("utf-8")
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        try:
            if marker in path.read_bytes():
                return True
        except OSError:
            continue
    return False


def test_neither_customer_can_see_the_others_material(two_customers):
    alice, bob = two_customers
    for account, mine, theirs in ((alice, ALICE_MATERIAL, BOB_MATERIAL), (bob, BOB_MATERIAL, ALICE_MATERIAL)):
        response = account.device.get("/own-material")
        assert response.status_code == 200, response.text[:300]
        body = response.text
        assert mine[:24] in body, f"{account.email} lost its own material"
        assert theirs[:24] not in body, f"{account.email} can see the other customer's material"


def test_the_provider_sees_two_different_customers(two_customers, harness: Control):
    """Each engine's calls carry their own `safety_identifier`, derived from the tenant id."""
    alice, bob = two_customers
    harness.ai_reset()
    for account, composer in ((alice, ALICE_DRAFT), (bob, BOB_DRAFT)):
        result = account.device.stream(stream_body("enhance_composer", composer=composer))
        assert result.final is not None, result.events
    identities = harness.ai_requests()["totals"]["identities"]
    assert len(identities) == 2, identities
    assert all(len(identity) == 32 for identity in identities)


def test_a_device_token_only_reaches_its_own_account(two_customers, harness: Control):
    alice, bob = two_customers
    assert alice.device.session()["account"]["id"] == alice.id
    assert bob.device.session()["account"]["id"] == bob.id
    assert alice.id != bob.id
    # Alice's token on Bob's browser changes nothing: the account comes from the token.
    swapped = bob.device.http.get(
        "/api/v1/session", headers={"Authorization": f"Bearer {alice.device.token}",
                                   "X-Starling-Extension": "1.0.0"}
    )
    assert swapped.status_code == 200
    assert swapped.json()["account"]["id"] == alice.id


def test_negative_control_the_canary_search_can_fail(two_customers, harness: Control, stack: dict):
    """Comparing a customer's folder with itself must break the isolation assertion."""
    alice, _bob = two_customers
    row = engine_row(harness, alice)
    folder = Path(stack["data_dir"]) / "tenants" / str(row["tenant_seq"])
    assert _contains(folder, "ALICE-CANARY")
    with pytest.raises(AssertionError):
        assert not _contains(folder, "ALICE-CANARY")
    assert not _contains(folder, "NOBODY-CANARY-EVER-WROTE-THIS")
