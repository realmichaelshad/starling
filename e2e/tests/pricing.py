"""What one AI call should cost, worked out from `config/ai_pricing.json` independently.

The services process computes the same number with `config.ModelPrice.cost_micros`; this is a
second implementation from the same published prices, so a journey checking `ai_calls` is
checking the product's arithmetic rather than repeating it.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

MICROS_PER_USD = 1_000_000


@lru_cache(maxsize=4)
def _models(path: str) -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(Path(path).read_text(encoding="utf-8"))["models"])


def price_for(pricing_file: str | Path, model: str, when: date | None = None) -> dict[str, Any]:
    today = when or datetime.now(timezone.utc).date()
    candidates = [
        entry for entry in _models(str(pricing_file))
        if entry["model"] == model and date.fromisoformat(entry["effective_from"]) <= today
    ]
    assert candidates, f"no price for {model} in {pricing_file}"
    return max(candidates, key=lambda entry: entry["effective_from"])


def tool_fee_micros(pricing_file: str | Path, tool: str = "web_search", when: date | None = None) -> int:
    """A provider tool's per-call fee from the `tools` rows (W4: the web search tool)."""
    today = when or datetime.now(timezone.utc).date()
    rows = json.loads(Path(pricing_file).read_text(encoding="utf-8")).get("tools", [])
    candidates = [row for row in rows
                  if row["tool"] == tool and date.fromisoformat(row["effective_from"]) <= today]
    assert candidates, f"no price for the {tool} tool in {pricing_file}"
    row = max(candidates, key=lambda entry: entry["effective_from"])
    return round(row["per_1k_calls"] / 1000 * MICROS_PER_USD)


def cost_micros(pricing_file: str | Path, model: str, *, input_tokens: int, output_tokens: int,
                cached_tokens: int = 0) -> int:
    """`input_tokens` includes the cached ones, as providers report them."""
    entry = price_for(pricing_file, model)
    uncached = max(0, int(input_tokens) - int(cached_tokens))
    total = (
        uncached * round(entry["input_per_mtok"] * MICROS_PER_USD)
        + int(cached_tokens) * round(entry["cached_input_per_mtok"] * MICROS_PER_USD)
        + int(output_tokens) * round(entry["output_per_mtok"] * MICROS_PER_USD)
    )
    return math.ceil(total / MICROS_PER_USD)
