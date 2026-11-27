"""Per-account concurrency: one active metered request, one waiter (CONTRACTS section 7).

Process-local asyncio state, one registry per event loop. The slot is held for the whole
`async with` block: the engine pool keeps the block open until the engine reports the request
finished (final, error or acknowledged cancel), so the next request cannot start before that.
"""
from __future__ import annotations

import asyncio
import weakref
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from .service import UsageError

BUSY_MESSAGE = "Starling is still working on your previous request. Try again in a moment."


@dataclass
class _Slot:
    active: bool = False
    waiters: "deque[asyncio.Future[None]]" = field(default_factory=deque)


_registries: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Slot]]" = weakref.WeakKeyDictionary()


def _registry() -> dict[str, _Slot]:
    loop = asyncio.get_running_loop()
    slots = _registries.get(loop)
    if slots is None:
        slots = _registries[loop] = {}
    return slots


def _hand_over(slots: dict[str, _Slot], account_id: str, slot: _Slot) -> None:
    """Pass the slot to the first waiter still waiting, or free it."""
    while slot.waiters:
        waiter = slot.waiters.popleft()
        if not waiter.done():
            waiter.set_result(None)
            return
    slot.active = False
    if slots.get(account_id) is slot:
        del slots[account_id]


@asynccontextmanager
async def account_slot(account_id: str, max_waiting: int = 1, wait_s: float = 15) -> AsyncIterator[None]:
    """Hold the account's request slot; raises `UsageError("busy", 409)` when the queue is full
    or the wait times out."""
    slots = _registry()
    slot = slots.get(account_id)
    if slot is None:
        slot = slots[account_id] = _Slot()
    if not slot.active:
        slot.active = True
    else:
        if len(slot.waiters) >= max_waiting:
            raise UsageError("busy", 409, BUSY_MESSAGE)
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        slot.waiters.append(waiter)
        try:
            async with asyncio.timeout(wait_s):
                await waiter
        except BaseException as exc:  # timed out or cancelled while waiting
            if waiter.done() and not waiter.cancelled():
                _hand_over(slots, account_id, slot)  # handed to us as we gave up: pass it on
            else:
                waiter.cancel()
                try:
                    slot.waiters.remove(waiter)
                except ValueError:
                    pass
            if isinstance(exc, TimeoutError):
                raise UsageError("busy", 409, BUSY_MESSAGE) from None
            raise
    try:
        yield
    finally:
        _hand_over(slots, account_id, slot)


def slot_status(account_id: str) -> tuple[bool, int]:
    """(active, waiting) for the account on the running loop; lets a route emit `queued` events."""
    slot = _registry().get(account_id)
    if slot is None:
        return False, 0
    return slot.active, sum(1 for waiter in slot.waiters if not waiter.done())
