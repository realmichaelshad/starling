"""Journey 9: the gateway dies in the middle of a draft (local, mocked providers).

A deploy is a restart, and a crash is a restart nobody planned. Either way a request that was
running is gone, and the control database still says it is running. Until its five-minute wall
that reservation counts as a draft in use: a customer near the limit would be told they had none
left by a request that no longer exists. The gateway settles those rows the moment it starts
(CONTRACTS section 7, Restart).
"""
from __future__ import annotations

import threading
import time

import pytest

from e2e.tests.client import Control, stream_body

DRAFT_COMPOSER = {
    "action": "post",
    "draft_text": "We moved the slow test to the nightly run and the afternoon got quieter.",
    "intent": "enhance",
}
LIVE = ("reserved", "active")


@pytest.fixture
def account(ready_account):
    return ready_account("restart")


def live_reservations(harness: Control, account_id: str) -> list[dict]:
    return [row for row in harness.reservations(account_id) if row["state"] in LIVE]


def test_a_gateway_crash_mid_draft_does_not_hold_the_customers_drafts(account, harness: Control, gateway_crash):
    before = account.device.session()["usage"]
    harness.ai_fail("slow", delay_s=40)                      # the AI provider takes its time with this one

    outcome: dict[str, object] = {}

    def press() -> None:
        try:
            outcome["result"] = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
        except Exception as exc:  # the connection dies with the gateway: that is the point
            outcome["error"] = type(exc).__name__

    thread = threading.Thread(target=press, daemon=True)
    thread.start()
    try:
        harness.wait_for(lambda: live_reservations(harness, account.id), timeout_s=60,
                         what="the draft to be running")
        [running] = live_reservations(harness, account.id)

        started = time.time()
        gateway_crash()                                       # no shutdown hooks, nothing released
        restarted_in = time.time() - started
    finally:
        harness.ai_reset()
    thread.join(60)

    row = next(item for item in harness.reservations(account.id) if item["id"] == running["id"])
    assert row["state"] == "expired" and row["release_reason"] == "gateway_restart", row
    assert live_reservations(harness, account.id) == []
    # Far inside the five-minute wall: the row was settled by the start-up, not by the sweeper.
    assert restarted_in < 120, restarted_in

    after = account.device.session()["usage"]                 # the same device token still works
    assert (after["used"], after["reserved"]) == (before["used"], 0)
    assert after["remaining"] == before["remaining"]          # nothing was shown, so nothing was charged

    again = account.device.stream(stream_body("enhance_composer", composer=DRAFT_COMPOSER))
    assert again.final is not None, again.events              # and the customer drafts again at once
    assert account.device.session()["usage"]["used"] == before["used"] + 1
