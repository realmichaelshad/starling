"""Per-account request slot (CONTRACTS section 7): one active request, one waiter for up to 15 s."""
from __future__ import annotations

import asyncio

import pytest

from starling_server.usage.concurrency import account_slot, slot_status
from starling_server.usage.service import UsageError


async def enter_and_exit(account_id: str, **options) -> None:
    async with account_slot(account_id, **options):
        pass


async def hold(account_id: str, until: asyncio.Event) -> None:
    async with account_slot(account_id):
        await until.wait()


def test_second_request_waits_and_a_third_is_busy():
    async def scenario() -> None:
        events: list[str] = []
        release_first = asyncio.Event()

        async def first() -> None:
            async with account_slot("acct"):
                events.append("first in")
                await release_first.wait()
                events.append("first out")

        async def second() -> None:
            async with account_slot("acct"):
                events.append("second in")

        first_task = asyncio.create_task(first())
        await asyncio.sleep(0)
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert slot_status("acct") == (True, 1)
        with pytest.raises(UsageError) as caught:
            await enter_and_exit("acct")
        assert (caught.value.http_status, caught.value.code) == (409, "busy")
        assert events == ["first in"]  # the slot is held until the first block exits
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert events == ["first in", "first out", "second in"]
        assert slot_status("acct") == (False, 0)
        async with account_slot("acct"):  # control: a free slot is taken at once
            assert slot_status("acct") == (True, 0)

    asyncio.run(scenario())


def test_waiter_gives_up_after_the_wait_time():
    async def scenario() -> None:
        until = asyncio.Event()
        holder = asyncio.create_task(hold("acct", until))
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(UsageError) as caught:
            await enter_and_exit("acct", wait_s=0.05)
        assert caught.value.code == "busy" and loop.time() - started >= 0.04
        assert slot_status("acct") == (True, 0)  # the timed-out waiter left the queue
        waiter = asyncio.create_task(enter_and_exit("acct"))
        await asyncio.sleep(0)
        assert slot_status("acct") == (True, 1)  # control: the queue place is free again
        until.set()
        await asyncio.gather(holder, waiter)
        assert slot_status("acct") == (False, 0)

    asyncio.run(scenario())


def test_slot_is_released_when_the_block_raises():
    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async with account_slot("acct"):
                raise RuntimeError("engine failed")
        assert slot_status("acct") == (False, 0)
        await enter_and_exit("acct")

    asyncio.run(scenario())


def test_cancelled_waiter_frees_its_place():
    async def scenario() -> None:
        until = asyncio.Event()
        holder = asyncio.create_task(hold("acct", until))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(enter_and_exit("acct"))
        await asyncio.sleep(0)
        assert slot_status("acct") == (True, 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert slot_status("acct") == (True, 0)
        replacement = asyncio.create_task(enter_and_exit("acct"))
        await asyncio.sleep(0)
        assert slot_status("acct") == (True, 1)
        until.set()
        await asyncio.gather(holder, replacement)
        assert slot_status("acct") == (False, 0)

    asyncio.run(scenario())


def test_slot_handed_to_a_cancelled_waiter_is_passed_on():
    async def scenario() -> None:
        slot = account_slot("acct")
        await slot.__aenter__()
        waiter = asyncio.create_task(enter_and_exit("acct"))
        await asyncio.sleep(0)
        await slot.__aexit__(None, None, None)  # hands the slot to the waiter without yielding
        waiter.cancel()  # ...which is cancelled before it resumes
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert slot_status("acct") == (False, 0)
        await enter_and_exit("acct")

    asyncio.run(scenario())


def test_accounts_are_independent_and_max_waiting_zero_refuses():
    async def scenario() -> None:
        async with account_slot("a"):
            async with account_slot("b"):
                assert slot_status("a") == (True, 0) and slot_status("b") == (True, 0)
            with pytest.raises(UsageError):
                await enter_and_exit("a", max_waiting=0)
        assert slot_status("a") == (False, 0) and slot_status("b") == (False, 0)

    asyncio.run(scenario())
