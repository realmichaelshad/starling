"""Which optional request parameters each upstream model accepts.

Sampling parameters (`temperature`, `top_p`) are dropped unless the model is known to accept them:
reasoning models reject them. Other optional parameters are assumed to be supported until the
provider answers 400 `unsupported_parameter`; the proxy then records the refusal here and every
later request in this process omits the parameter.
"""
from __future__ import annotations

import threading
from typing import Iterable

SAMPLING_PARAMS = ("temperature", "top_p")
DROPPABLE_PARAMS = frozenset(
    {"temperature", "top_p", "reasoning_effort", "safety_identifier", "prompt_cache_key", "stream_options"}
)

# Model families that accept temperature and top_p (non-reasoning chat models).
SAMPLING_MODEL_PREFIXES: tuple[str, ...] = ("gpt-4.1", "gpt-4o")


class CapabilityTable:
    def __init__(self, sampling_model_prefixes: Iterable[str] = SAMPLING_MODEL_PREFIXES) -> None:
        self._prefixes = tuple(sampling_model_prefixes)
        self._learned: dict[str, dict[str, bool]] = {}
        self._lock = threading.Lock()

    def allows(self, model: str, param: str) -> bool:
        with self._lock:
            learned = self._learned.get(model, {}).get(param)
        if learned is not None:
            return learned
        if param in SAMPLING_PARAMS:
            return any(model == prefix or model.startswith(prefix + "-") for prefix in self._prefixes)
        return True

    def mark_unsupported(self, model: str, param: str) -> bool:
        """Remember that `model` rejects `param`; return True when this is new information."""
        with self._lock:
            params = self._learned.setdefault(model, {})
            if params.get(param) is False:
                return False
            params[param] = False
            return True

    def snapshot(self, models: Iterable[str]) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for model in sorted(set(models)):
            with self._lock:
                learned = sorted(p for p, ok in self._learned.get(model, {}).items() if not ok)
            out[model] = {
                "params": {param: self.allows(model, param) for param in sorted(DROPPABLE_PARAMS)},
                "learned_unsupported": learned,
            }
        return out
