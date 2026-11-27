"""The requests that reach an engine without using a draft pass the same gate as the ones that do.

Found by the security review. `POST /api/v1/assist` with `mode: "schedule"` and the three
`/own-material` routes start the customer's engine, and they checked only that the account was not
suspended: no confirmed email, no consent, no rate limit, no limit on requests in flight. Engines
are the scarcest thing on the machine (`ENGINE_MAX_LIVE`, six by default) and one with a request in
flight is never evicted, so a handful of throwaway accounts that never confirmed an address could
hold every engine for ever while paying customers got "Starling's AI isn't responding".
"""
from __future__ import annotations

import threading
import uuid

import httpx
import pytest

from starling_server.auth.deps import DeviceAuth
from starling_server.security import ratelimit

from . import conftest as engine_conftest
from .support import served, set_consent

SCHEDULE = {"mode": "schedule", "schedule": {"op": "list"}}
UNMETERED = (
    ("POST", "/api/v1/assist", SCHEDULE),
    ("GET", "/api/v1/own-material", None),
    ("POST", "/api/v1/own-material", {"kind": "post", "text": "something I wrote"}),
    ("DELETE", "/api/v1/own-material/abc123", None),
)


def call(h, method: str, path: str, body=None, name: str = "a") -> httpx.Response:
    with httpx.Client(timeout=60, trust_env=False) as client:
        return client.request(method, h.url(path), json=body, headers=h.headers(name, None, "Europe/London"))


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    ratelimit.forget_refusals()
    yield
    ratelimit.forget_refusals()


def test_an_account_that_never_confirmed_its_email_cannot_start_an_engine(harness_factory, fake_usage, slots, monkeypatch):
    unverified = lambda account_id, seq, status="active": DeviceAuth(  # noqa: E731
        account_id=account_id, tenant_seq=seq, device_id=str(uuid.uuid4()), email_verified=False, status=status,
        extension_version="1.0.0")
    monkeypatch.setattr(engine_conftest, "device_for", unverified)
    h = harness_factory(("a",))
    for method, path, body in UNMETERED:
        response = call(h, method, path, body)
        assert (response.status_code, code(response)) == (403, "email_unverified"), path
    assert served(h.settings, 1) == [], "no engine was started for it"


def test_they_need_the_disclosure_accepted_like_every_other_engine_request(harness_factory, fake_usage, slots, settings, database):
    h = harness_factory(("a",), consent=False)
    for method, path, body in UNMETERED:
        response = call(h, method, path, body)
        assert (response.status_code, code(response)) == (403, "consent_required"), path
    assert served(h.settings, 1) == []
    set_consent(database, settings, h.account_id("a"), passive=False)
    assert call(h, "POST", "/api/v1/assist", SCHEDULE).status_code == 200  # control: the gate was the only thing in the way


def test_a_script_is_rate_limited_and_a_person_is_not(harness, monkeypatch):
    monkeypatch.setattr(ratelimit, "ENGINE_UNMETERED_PER_ACCOUNT", ratelimit.Rule("engine_unmetered", 3, 60))
    for _ in range(3):
        assert call(harness, "POST", "/api/v1/assist", SCHEDULE).status_code == 200
    refused = call(harness, "GET", "/api/v1/own-material")
    assert (refused.status_code, code(refused)) == (429, "rate_limited")
    assert int(refused.headers["retry-after"]) >= 1
    # The limit is per account: somebody else is not affected.
    assert call(harness, "POST", "/api/v1/assist", SCHEDULE, name="b").status_code == 200


def test_one_account_holds_one_engine_request_at_a_time(harness):
    """One running, one waiting, the rest refused: the same slot the metered requests use."""
    slow = {**SCHEDULE, "fake": {"script": "slow", "delay_s": 2.0}}
    statuses: list[int] = []

    def press() -> None:
        statuses.append(call(harness, "POST", "/api/v1/assist", slow).status_code)

    threads = [threading.Thread(target=press) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(statuses) == [200, 200, 409, 409], statuses
