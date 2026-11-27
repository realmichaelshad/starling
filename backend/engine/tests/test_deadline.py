"""One budget for one request, so the engine gives up before the gateway does.

A draft makes several model calls IN SEQUENCE and each was allowed its own
timeout. Nothing added them up, so a pipeline could still be drafting into a
client that had already given up, with a message that carried no reason.
The request now knows how long it has, and so does every call inside it.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import cloud, deadline, providers, request_context
from app.providers import ProviderRouter


@pytest.fixture(autouse=True)
def clean():
    deadline.clear()
    yield
    deadline.clear()


# --------------------------------------------------------------------------- #
# The budget itself
# --------------------------------------------------------------------------- #

def test_no_budget_changes_nothing():
    """Every caller outside a request (the learning loop, the tools) runs with no
    budget and keeps its full timeouts. None is not zero."""
    assert deadline.remaining() is None
    assert deadline.expired() is False
    assert deadline.cap(120.0) == 120.0


def test_a_budget_shortens_every_call_inside_it():
    with deadline.budget(60):
        allowed = 60 - deadline.TAIL_RESERVE_S
        assert deadline.cap(120.0) == pytest.approx(allowed, abs=1.0)
        assert deadline.cap(5.0) == 5.0        # already shorter: left alone


def test_a_spent_budget_reads_as_expired():
    with deadline.budget(1):
        assert deadline.expired()
        assert deadline.cap(120.0) == 0.0


def test_the_finishing_work_gets_its_own_room():
    with deadline.budget(100):
        assert deadline.remaining() == pytest.approx(100.0, abs=1.0)
        assert deadline.model_time() == pytest.approx(100.0 - deadline.TAIL_RESERVE_S, abs=1.0)
        assert deadline.model_time() < deadline.remaining()


def test_a_budget_that_is_all_tail_starts_no_model_call():
    with deadline.budget(deadline.TAIL_RESERVE_S):
        assert deadline.expired()


def test_a_budget_is_released_at_the_end_of_the_request():
    with deadline.budget(30):
        assert deadline.remaining() is not None
    assert deadline.remaining() is None


def test_the_budget_reaches_the_worker_thread():
    """The pipeline runs under `asyncio.to_thread`, which copies the context."""
    async def main():
        with deadline.budget(60):
            return await asyncio.to_thread(deadline.cap, 120.0)
    assert asyncio.run(main()) == pytest.approx(60.0 - deadline.TAIL_RESERVE_S, abs=1.0)


def test_the_stream_budgets_stay_inside_the_gateway_limits():
    """CONTRACTS §6.4: 240 seconds in total and about 90 to the first token. The
    engine must give up first, because only the engine knows why."""
    assert deadline.STREAM_BUDGET_S < 240.0
    assert deadline.first_token_budget() < 90.0
    assert deadline.DEFAULT_BUDGET_S < 240.0


def test_the_one_shot_budget_ends_before_the_extension_gives_up():
    """S5: the extension abandons ✦ at 60 s. The engine must stop first, with a reason,
    and a single call may never outlast the press."""
    assert deadline.DEFAULT_BUDGET_S <= 50.0
    assert cloud.CHAT_TIMEOUT_S < deadline.DEFAULT_BUDGET_S
    assert cloud.STREAM_READ_TIMEOUT_S <= cloud.CHAT_TIMEOUT_S


def test_a_cancelled_request_starts_no_model_call():
    with request_context.bind("res_cancelled") as state:
        assert not deadline.expired()
        state.cancel_event.set()
        assert deadline.expired()
    assert not deadline.expired()


def test_a_fatal_refusal_ends_the_request_but_one_oversized_call_does_not():
    with request_context.bind("res_refused"):
        request_context.record_failure(request_context.AIFailure("budget_exhausted", "paused", 60, True))
        assert deadline.expired()
    with request_context.bind("res_oversized"):
        request_context.record_failure(request_context.AIFailure("input_too_large", "too big", None, False))
        assert not deadline.expired()


# --------------------------------------------------------------------------- #
# What the router does with it
# --------------------------------------------------------------------------- #

@pytest.fixture()
def services(monkeypatch):
    """A fake services proxy that records the read timeout each call was given."""
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "a draft"}}]})

    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:1")
    cloud.set_services_token("d" * 43)
    monkeypatch.setattr(cloud, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://services"))
    return timeouts


def test_a_call_is_given_what_is_left_rather_than_the_constant(services):
    # API-model timings (S5): a call's own timeout is 20 s, so the budget has to be the shorter one here.
    router = ProviderRouter()
    with deadline.budget(deadline.TAIL_RESERVE_S + 10):
        assert router.chat([{"role": "user", "content": "hi"}], options={}) == "a draft"
    assert services[0] == pytest.approx(10.0, abs=1.5)
    assert services[0] < providers.CHAT_TIMEOUT_S


def test_an_auxiliary_call_asks_for_its_own_shorter_timeout(services):
    router = ProviderRouter()
    assert router.chat([{"role": "user", "content": "hi"}], options={"timeout_s": cloud.AUX_TIMEOUT_S}) == "a draft"
    assert services[0] == cloud.AUX_TIMEOUT_S
    assert cloud.AUX_TIMEOUT_S < providers.CHAT_TIMEOUT_S


def test_without_a_budget_a_call_keeps_the_full_timeout(services):
    assert ProviderRouter().chat([{"role": "user", "content": "hi"}], options={}) == "a draft"
    assert services[0] == providers.CHAT_TIMEOUT_S


def test_a_call_that_cannot_finish_is_never_started(services):
    with deadline.budget(1):
        assert ProviderRouter().chat([{"role": "user", "content": "hi"}], options={}) is None
    assert services == [], "a spent budget must not reach the service"


def test_a_stream_that_cannot_finish_is_never_started(services):
    with deadline.budget(1):
        events = list(ProviderRouter().chat_stream([{"role": "user", "content": "hi"}], options={}))
    assert events == [{"done": True, "timing": {"error": True, "expired": True}}]
    assert services == []


# --------------------------------------------------------------------------- #
# What the DRAFTING PIPELINE does with it
# --------------------------------------------------------------------------- #

def test_an_unbudgeted_stream_still_runs():
    from app import intelligence

    assert deadline.remaining() is None
    assert intelligence._affordable(10_000.0) is True


# RETIRED (API-native pass, 2026-09-22): test_the_optional_extra_calls_stop_before_the_draft_does: the clarity rewrite is gone (style is a note).


def test_the_draft_keeps_a_floor_the_grounding_cannot_spend():
    assert deadline.DRAFT_RESERVE_S > deadline.MIN_USEFUL_S
    with deadline.budget(deadline.STREAM_BUDGET_S):
        assert deadline.model_time() > deadline.DRAFT_RESERVE_S
    # API-model reserves (S5): 10 s of draft floor, so "just under it" is 2 s under, not 15.
    with deadline.budget(deadline.TAIL_RESERVE_S + deadline.DRAFT_RESERVE_S - 2):
        left = deadline.model_time()
        assert left < deadline.DRAFT_RESERVE_S, "grounding must stand aside here"
        assert not deadline.expired(), "...and the draft itself must still run"
