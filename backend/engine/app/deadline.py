"""One wall-clock budget for one user-facing request.

A draft makes several model calls in sequence (reading, deciding, drafting,
checking), and per-call timeouts do not add up to a per-request one. The rule
here is that **the request knows how long it has**: every model call is capped
by what is LEFT rather than by a constant, and a call that cannot start inside
the budget is not started at all. The pipeline then does what it already does
when a model returns nothing: degrade, and say so, while the reader still waits.

The same check stops a request that was cancelled (the gateway's cancel route, a
client disconnect, shutdown) or that the AI service already refused in a way the
next call would repeat (see `request_context`).

Deliberately a budget and not a preemption: a call that is already running keeps
its capped timeout, and the next one sees a smaller budget. `contextvars` carry
the budget to every call site: `asyncio.to_thread` copies the context, and every
other thread or executor site copies it explicitly.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import time
from typing import Iterator

from . import request_context

# One contract with the gateway (CONTRACTS §6.4: 240 seconds in total and about
# 90 to the first token). The rule behind all three numbers is that the ENGINE
# gives up first: only the engine knows WHY it ran out, and a gateway timeout can
# only ever say "could not finish".
#
# One-shot `/browser/assist`: when the answer must be ON THE WIRE, not when the
# last model call may start (see TAIL_RESERVE_S).
#
# 50 s, BELOW THE EXTENSION'S 60 s (S5, 2026-09-21). It was 145 s, sized for a
# local model at 5-9 tok/s; the extension gives up on ✦ at 60 s, so for up to
# 85 s after the reader was told "could not finish" the engine kept drafting,
# kept spending and kept the account's slot busy. On the API model a ✦ press
# is 2-4 s; 50 s is a safety net, and it means the ENGINE gives up first, with
# a reason, while the reader is still waiting.
DEFAULT_BUDGET_S = 50.0

# Streamed `/browser/assist/stream`: tokens reach the composer as they are
# produced, so the reader can watch it working. Still inside the gateway's 240 s.
STREAM_BUDGET_S = 225.0

# A STREAM THAT HAS SAID NOTHING IS NOT A STREAM. The longer stream budget is
# justified by the reader watching tokens arrive, which is void while nothing has
# arrived. A legitimate press can spend a while grounding before the writer
# starts, so the bound leaves room for that and still ends before the gateway's
# first-token limit.
#
# Enforced in `main.browser_assist_stream`, not here: giving up on the reader's
# behalf is the streaming endpoint's decision, because only the endpoint knows
# whether anything has been said. Giving up sets the request's cancel event,
# which closes the services stream at its next chunk.
FIRST_TOKEN_BUDGET_S = 85.0


def first_token_budget() -> float:
    """`FIRST_TOKEN_BUDGET_S`, overridable by env for measurement and tuning.

    Read at CALL time, not import time, so a value can be tried against a
    running engine without editing code. It exists because this number is the
    one knob in the file that cannot be verified through `TestClient`:
    `Request.is_disconnected()` blocks under that harness (it does not under
    uvicorn), so the streaming loop there only advances once the worker ends and
    a timing assertion measures the harness instead of the fix. Proving this
    therefore means a real HTTP request against a real engine with a small
    value set, which needs an override to be possible at all.

    Zero or negative disables the guard rather than abandoning instantly — an
    off switch is the behaviour someone typing 0 wants, and a stream that gives
    up before it starts would be worse than the bug.
    """
    raw = os.environ.get("STARLING_FIRST_TOKEN_BUDGET_S", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return FIRST_TOKEN_BUDGET_S
        return value if value > 0 else float("inf")
    return FIRST_TOKEN_BUDGET_S

# A call needs enough time to be worth starting. Below this a model cannot load,
# let alone answer, so the remaining seconds are better spent returning what the
# pipeline already has.
MIN_USEFUL_S = 5.0

# The model is not the last thing that happens. After the final call the gates
# run, the draft is scored and formatted, rejections are read back out of
# storage and the response is composed — none of which touches a model and all
# of which happens while the client is still counting.
#
# Measured after the first fix: model calls stopped at the 140 s budget and the
# answer reached the wire at 151.3 s, still past the native host's 150 s. The
# budget above is the WIRE deadline; model calls have to stop early enough to
# leave this much room for the finishing work.
#
# 5 s on the API model (S5): the finishing work is gates, scoring and storage,
# which take well under a second; 25 s was sized for a machine whose every step
# was slow.
TAIL_RESERVE_S = 5.0

# What the DRAFT itself needs, kept back from the work that only prepares it.
#
# The one thing the user asked for is the draft; the grounding chain in front of
# it (the research summary, then the radar's ranking call) is an INPUT to that
# draft. Nothing said so, so on a cold cache the two grounding calls could spend
# most of the budget and hand the draft what was left — and a post that is
# perfectly grounded and never written is worth less than a plain one that
# arrives. Measured on this box on 2026-07-28: 167 tok/s prompt eval and
# 9.4 tok/s decode, so a 2k-token prompt plus 260-320 predicted tokens is
# ~45-50 s, and the assembly and gates run after it.
#
# This is a floor for the mandatory work, not a cap on the optional work: with a
# warm cache grounding costs almost nothing and nothing here ever fires.
#
# 10 s on the API model (S5): room for two draft attempts (~1.5 s each under a
# 20 s per-call cap that the deadline shortens anyway) plus the gates.
DRAFT_RESERVE_S = 10.0

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "starling_request_deadline", default=None)


@contextlib.contextmanager
def budget(seconds: float = DEFAULT_BUDGET_S) -> Iterator[None]:
    """Give everything inside this block `seconds` of wall clock, together."""
    token = _deadline.set(time.monotonic() + max(0.0, float(seconds)))
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining() -> float | None:
    """Seconds left, or None when this request has no budget.

    None is not zero and the difference matters: every caller outside a request
    — the arena, the learning loop, the tools — runs with no budget and must
    keep its full timeouts.
    """
    at = _deadline.get()
    if at is None:
        return None
    return at - time.monotonic()


def model_time() -> float | None:
    """How long a MODEL call may run, or None when there is no budget.

    What is left, less the room the finishing work needs. A call allowed to run
    right up to the wire deadline guarantees the answer misses it.
    """
    left = remaining()
    if left is None:
        return None
    return left - TAIL_RESERVE_S


def expired() -> bool:
    """True when no model call can usefully start for this request.

    That is: the budget exists and is spent, the request was cancelled
    (`POST /internal/requests/{id}/cancel`, a client disconnect, shutdown), or
    the AI service already refused the request in a way the next call would
    repeat (a limit, an exhausted budget, an authentication failure).
    """
    if request_context.cancelled() or request_context.fatal_failure() is not None:
        return True
    left = model_time()
    return left is not None and left < MIN_USEFUL_S


def cap(timeout_s: float) -> float:
    """`timeout_s`, shortened to fit what is left for model work."""
    left = model_time()
    if left is None:
        return timeout_s
    return max(0.0, min(float(timeout_s), left))


def clear() -> None:
    """Test hook: drop any budget this context is carrying."""
    _deadline.set(None)
