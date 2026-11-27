"""Fixtures for the API-level journeys.

The whole session shares one harness. By default the session starts its own (fresh data
folder, fresh ports) and stops it again, engines included. Set `STARLING_E2E_STATE` to the
state file of a harness started with `python -m e2e.harness up --detach` to run against that
one instead; the session then leaves it running, which is how part 2 will share a stack
between the API journeys and the browser journeys.

Accounts are session scoped and named: a journey asks for the account it needs by name and
gets the same one for the whole session. Each one signs up from its own simulated network
(`X-Forwarded-For`, trusted because the harness lists a proxy), so the per-network sign-up and
rate limits separate the journeys instead of adding up across them.
"""
from __future__ import annotations

import itertools
import os
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from e2e.harness import env as harness_env  # noqa: E402
from e2e.harness.stack import Stack, attach  # noqa: E402
from e2e.tests.client import (  # noqa: E402
    Account,
    Control,
    Device,
    Website,
    pkce_pair,
    new_state,
    redirect_query,
    redirect_uri_for,
)

# Each simulated customer gets its own /64 out of the IPv6 documentation range, sent as
# `X-Forwarded-For`. The addresses are never contacted; they only separate the per-network
# rate limits the way real customers are separated.
CLIENT_NETWORK = "2001:db8:{index:x}::5"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-harness",
        action="store_true",
        default=False,
        help="leave the harness running after the session (it is stopped by default)",
    )


@pytest.fixture(scope="session")
def running_stack(request: pytest.FixtureRequest) -> Any:
    """(state, Stack or None). The Stack is None when the session attached to someone else's."""
    external = os.environ.get("STARLING_E2E_STATE")
    if external:
        state = attach(Path(external))
        if state is None:
            raise pytest.UsageError(f"STARLING_E2E_STATE={external} does not name a running harness")
        yield state.as_dict(), None
        return
    started = Stack()
    state = started.up()
    try:
        yield state.as_dict(), started
    finally:
        if request.config.getoption("--keep-harness"):
            print(f"\nharness left running at {state.gateway_url} (state: {state.state_file})", file=sys.stderr)
        else:
            report = started.down()
            if report.get("engines_swept"):
                print(f"\nharness swept stray engines: {report['engines_swept']}", file=sys.stderr)


@pytest.fixture(scope="session")
def stack(running_stack) -> Any:
    return running_stack[0]


@pytest.fixture(scope="session")
def services_restart(running_stack) -> Any:
    """Restart the services process with different settings, or skip when we do not own it.

    Journey 5 needs it twice: once with a tiny AI budget so the next call trips the daily stop,
    and once with the budget raised again, which is what clears the stop at startup
    (CONTRACTS 4.4).
    """
    _state, owned = running_stack
    if owned is None:
        pytest.skip("this session attached to a harness it does not own; it cannot restart the services process")

    def restart(**overrides: str) -> str:
        return owned.restart_services({key: str(value) for key, value in overrides.items()})

    return restart


@pytest.fixture(scope="session")
def gateway_crash(running_stack) -> Any:
    """Kill the gateway without warning and start it again, or skip when we do not own it."""
    _state, owned = running_stack
    if owned is None:
        pytest.skip("this session attached to a harness it does not own; it cannot crash the gateway")
    return owned.crash_gateway


@pytest.fixture(scope="session")
def harness(stack: dict[str, Any]) -> Any:
    """Everything a journey needs that is not a browser or the extension."""
    control = Control(stack)
    try:
        yield control
    finally:
        control.close()


@pytest.fixture(scope="session")
def gateway_url(stack: dict[str, Any]) -> str:
    return str(stack["gateway_url"])


@pytest.fixture(scope="session")
def extension_id(stack: dict[str, Any]) -> str:
    return str(stack["extension_id"])


@pytest.fixture(scope="session")
def consent_version(stack: dict[str, Any]) -> str:
    return str(stack["settings"]["consent_version"])


@pytest.fixture(scope="session")
def plans(stack: dict[str, Any]) -> dict[str, Any]:
    return dict(stack["settings"]["plans"]["codes"])


@pytest.fixture
def site(gateway_url: str) -> Any:
    """A fresh browser (its own cookies and its own network), closed at the end of the test."""
    browser = Website(gateway_url, client_ip=CLIENT_NETWORK.format(index=_next_network()))
    try:
        yield browser
    finally:
        browser.close()


_network_counter = itertools.count(1)


def _next_network() -> int:
    return next(_network_counter)


# --------------------------------------------------------------------------- accounts


def _make_account(gateway_url: str, control: Control, email: str, verified: bool = True) -> Account:
    browser = Website(gateway_url, client_ip=CLIENT_NETWORK.format(index=_next_network()))
    response = browser.signup(email)
    assert response.status_code == 303, f"signup failed: {response.status_code} {response.text[:300]}"
    if verified:
        assert browser.verify_email(email).status_code == 303
    assert browser.login(email).status_code == 303
    return Account(email=email, password="a long enough passphrase", id=control.account_id(email), site=browser)


def connect_device(account: Account, extension_id: str, gateway_url: str, label: str = "E2E Chrome") -> Device:
    verifier, challenge = pkce_pair()
    state = new_state()
    params = {
        "client_id": extension_id,
        "redirect_uri": redirect_uri_for(extension_id),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "device_label": label,
    }
    approved = account.site.approve_connect(params)
    query = redirect_query(approved)
    assert query["state"] == state
    exchange = account.site.http.post(
        "/api/v1/devices/token",
        json={
            "grant_type": "authorization_code",
            "code": query["code"],
            "code_verifier": verifier,
            "redirect_uri": params["redirect_uri"],
            "client_id": extension_id,
            "device_label": label,
            "extension_version": harness_env.EXTENSION_VERSION,
        },
    )
    assert exchange.status_code == 200, exchange.text
    token = exchange.json()
    return Device(gateway_url, token["token"], token["device_id"], harness_env.EXTENSION_VERSION)


@pytest.fixture(scope="session")
def accounts(stack: dict[str, Any], harness: Control) -> Any:
    """`accounts("drafting")` gives the same account for the whole session.

    Every account is a real sign-up through the website, from its own simulated network, so a
    journey never inherits another journey's per-network rate-limit budget.
    """
    gateway_url = str(stack["gateway_url"])
    made: dict[str, Account] = {}
    # Per session, not per harness: a second `pytest` run against a harness that is still up
    # must sign up fresh addresses rather than re-verify accounts the first run already made.
    suffix = secrets.token_hex(4)

    def factory(name: str, *, verified: bool = True, with_device: bool = False) -> Account:
        if name not in made:
            email = f"e2e-{name}-{suffix}@starling.example"
            made[name] = _make_account(gateway_url, harness, email, verified=verified)
        account = made[name]
        if with_device and account.device is None:
            account.device = connect_device(account, str(stack["extension_id"]), gateway_url)
        return account

    try:
        yield factory
    finally:
        for account in made.values():
            account.close()


@pytest.fixture
def ready_account(accounts: Callable[..., Account], consent_version: str) -> Any:
    """`ready_account("drafting")`: signed up, verified, connected and consented."""

    def factory(name: str, *, passive_learning: bool = False) -> Account:
        account = accounts(name, with_device=True)
        if not account.extras.get("consented"):
            response = account.device.consent(passive_learning=passive_learning, version=consent_version)
            assert response.status_code in (200, 201), response.text
            account.extras["consented"] = True
        return account

    return factory


# --------------------------------------------------------------------------- small helpers


@pytest.fixture
def fresh_key() -> Callable[[], str]:
    return lambda: str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _clean_fakes(harness: Control) -> Any:
    """Every test starts with an empty provider call log, no queued failure and Stripe's clock at now.

    A renewal moves the fake's clock to the period end (a month ahead), and a Checkout session
    whose expiry is a month from Stripe's clock is refused, so the offset is put back between
    tests rather than left for the next journey to trip over.
    """
    harness.ai_reset()
    harness.stripe("/clock", {"offset_s": 0})
    yield
    harness.ai_reset()
