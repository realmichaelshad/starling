"""Operator commands: `ai-check` and `services-status` (`python -m starling_server.cli ...`)."""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Callable

import httpx

from ..config import Settings, get_settings
from . import client
from .admin import run_ai_check
from .capabilities import CapabilityTable
from .upstream import UpstreamClient


async def direct_ai_check(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """The same check as `POST /admin/ai-check`, run here with the configured key (not written to the database)."""
    capabilities = CapabilityTable()
    upstream = UpstreamClient(settings, capabilities, transport=transport)
    try:
        return await run_ai_check(settings, upstream, capabilities)
    finally:
        await upstream.aclose()


def format_ai_check(result: dict[str, Any]) -> str:
    lines = [f"AI provider: {result.get('provider') or 'openai'} at {result.get('base_url')}"]
    keys = result.get("keys") or {}
    for slot in ("primary", "secondary"):
        info = keys.get(slot) or {}
        if info.get("configured"):
            state = f"set, ends with {info['last4']}" if info.get("last4") else "set"
        else:
            state = "not set"
        lines.append(f"{slot} key: {state}")
    for item in result.get("results") or []:
        lanes = ", ".join(item.get("lanes") or [])
        if item.get("ok"):
            lines.append(f"OK    {item.get('model')} ({lanes}) {item.get('latency_ms')} ms: {item.get('message')}")
        else:
            lines.append(f"FAIL  {item.get('model')} ({lanes}) [{item.get('error_code')}]: {item.get('message')}")
    lines.append("AI check passed." if result.get("ok") else "AI check failed.")
    return "\n".join(lines)


def run_ai_check_command(
    settings: Settings,
    *,
    as_json: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Callable[[str], None] = print,
) -> int:
    result = asyncio.run(direct_ai_check(settings, transport=transport))
    out(json.dumps(result, indent=2, sort_keys=True) if as_json else format_ai_check(result))
    return 0 if result.get("ok") else 1


def run_status_command(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None, out: Callable[[str], None] = print
) -> int:
    result = asyncio.run(client.services_status(settings, transport=transport))
    out(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


def _ai_check(args: argparse.Namespace) -> int:
    return run_ai_check_command(get_settings(), as_json=bool(args.json))


def _services_status(args: argparse.Namespace) -> int:
    return run_status_command(get_settings())


def register(subparsers: argparse._SubParsersAction) -> None:
    check = subparsers.add_parser(
        "ai-check", help="send a minimal request to each configured AI model with the configured key"
    )
    check.add_argument("--json", action="store_true", help="print the full result as JSON")
    check.set_defaults(func=_ai_check)
    status = subparsers.add_parser(
        "services-status",
        help="show budgets, masked key status, upstream health and news freshness from the running services process",
    )
    status.set_defaults(func=_services_status)
