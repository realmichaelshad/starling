"""Model access for the engine. Every call goes to the services AI proxy.

The router keeps the interface the drafting pipeline was written against
(`chat`, `chat_stream`, `status`, `model_for`, `text_model`, `fast_model`,
`last_timing`); the transport underneath is `cloud.py`. There is no local
fallback. When the service refuses a call the refusal is recorded for the
request being served (`request_context`), so the endpoint can tell the reader
why nothing was drafted instead of returning an empty draft with no reason.
"""
from __future__ import annotations

import contextvars
import threading
import time
from typing import Any, Iterator

from . import cloud, deadline, request_context

# Callers pass this to mean "whatever the quality lane is".
DEFAULT_TEXT_MODEL: str | None = None
# Embeddings are computed in-process by the bundled random projection
# (app/embeddings.py); the services contract has no embeddings route.
EMBEDDING_MODEL = "starling-embedding"
CHAT_TIMEOUT_S = cloud.CHAT_TIMEOUT_S
PROVIDER_NAME = "starling-services"

# Per call, not per router. One router serves every concurrent request, so a
# shared attribute would hand one request the timing of another. A ContextVar
# is private to the request's context and to the worker threads it started.
_LAST_TIMING: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "starling_last_timing", default=None)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


class ProviderRouter:
    """Chat through the services proxy, by lane: quality, fast or vision."""

    name = PROVIDER_NAME

    def __init__(self, text_model: str | None = None,
                 embedding_model: str = EMBEDDING_MODEL) -> None:
        # A pinned model name is sent as-is instead of the quality alias. Only
        # tools and tests pin; the shared router always uses the lane aliases.
        self._pinned_model = (text_model or "").strip() or None
        self.embedding_model = embedding_model

    # ---- timing ----------------------------------------------------------
    @property
    def last_timing(self) -> dict[str, Any]:
        return dict(_LAST_TIMING.get() or {})

    @last_timing.setter
    def last_timing(self, value: dict[str, Any] | None) -> None:
        _LAST_TIMING.set(dict(value or {}))

    def _record(self, timing: dict[str, Any], model: str) -> dict[str, Any]:
        stamped = dict(timing)
        stamped["provider"], stamped["model"] = self.name, model
        _LAST_TIMING.set(stamped)
        return dict(stamped)

    # ---- models ----------------------------------------------------------
    @property
    def text_model(self) -> str:
        return self._pinned_model or cloud.model_for("quality")

    @property
    def fast_model(self) -> str:
        return cloud.model_for("fast")

    @property
    def chosen_via(self) -> str:
        return "pinned" if self._pinned_model else "services"

    def model_for(self, lane: str) -> str:
        if lane == "fast":
            return self.fast_model
        if lane == "vision":
            return cloud.model_for("vision")
        return self.text_model

    def active_provider_name(self) -> str:
        return self.name

    # ---- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Cheap and local: no model or network call is made here."""
        available = cloud.is_configured()
        message = ("Writing through the Starling AI service." if available
                   else "The Starling AI service is not configured.")
        return {
            "available": available,
            "provider": self.name,
            "text_model": self.text_model,
            "fast_model": self.fast_model,
            "embedding_model": self.embedding_model,
            "text_model_installed": available,
            "model_loaded": available,
            "chosen_via": self.chosen_via,
            "message": message,
            "blocker": None if available else {"code": "services_unconfigured", "message": message},
            "last_timing": self.last_timing or None,
        }

    def invalidate_status(self) -> None:
        """Nothing is cached."""

    def warm(self) -> None:
        """A hosted model has nothing to pre-load."""

    def warm_async(self) -> None:
        """A hosted model has nothing to pre-load."""

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """No remote embeddings: callers use the in-process embedder (app/embeddings.py)."""
        return None

    # ---- inference -------------------------------------------------------
    def _refused(self, error: cloud.ServicesError, model: str, started: float) -> dict[str, Any]:
        request_context.record_failure(error.failure())
        return self._record({"total_ms": _elapsed_ms(started), "error": True,
                             "code": error.code, "message": error.message}, model)

    def chat(self, messages: list[dict[str, Any]], *, options: dict[str, Any] | None = None,
             fmt: str | dict[str, Any] | None = None, think: bool = False,
             lane: str = "quality") -> str | None:
        del think
        # Out of time, cancelled, or already refused for this request: None is
        # the answer every caller handles for a model that produced nothing.
        if deadline.expired():
            return None
        model = self.model_for(lane)
        if not cloud.is_configured():
            self._record({"error": True, "code": "services_unconfigured"}, model)
            return None
        started = time.perf_counter()
        try:
            content, timing = cloud.chat(model, messages, options=options, fmt=fmt)
        except cloud.ServicesError as error:
            self._refused(error, model, started)
            return None
        except Exception:                                   # noqa: BLE001
            self._refused(cloud.ServicesError("ai_unavailable"), model, started)
            return None
        self._record(timing, model)
        return content or None

    def chat_stream(self, messages: list[dict[str, Any]], *, options: dict[str, Any] | None = None,
                    think: bool = False, stop_event: threading.Event | None = None,
                    lane: str = "quality") -> Iterator[dict[str, Any]]:
        del think
        if deadline.expired():
            yield {"done": True, "timing": {"error": True, "expired": True}}
            return
        model = self.model_for(lane)
        if not cloud.is_configured():
            yield {"done": True, "timing": self._record(
                {"error": True, "code": "services_unconfigured"}, model)}
            return
        started = time.perf_counter()
        delivered = 0
        try:
            for event in cloud.chat_stream(model, messages, options=options,
                                           stop_event=stop_event):
                if event.get("done"):
                    yield {"done": True,
                           "timing": self._record(dict(event.get("timing") or {}), model)}
                    return
                delivered += 1
                yield event
        except cloud.ServicesError as error:
            timing = self._refused(error, model, started)
            if delivered:
                # Tokens already reached the reader; say the answer was cut short.
                timing["truncated"] = True
            yield {"done": True, "timing": timing}
            return
        except Exception:                                   # noqa: BLE001
            timing = self._refused(cloud.ServicesError("ai_unavailable"), model, started)
            if delivered:
                timing["truncated"] = True
            yield {"done": True, "timing": timing}
            return
        yield {"done": True, "timing": self._record({"total_ms": _elapsed_ms(started)}, model)}


_ROUTER: ProviderRouter | None = None
_ROUTER_LOCK = threading.Lock()


def get_router() -> ProviderRouter:
    global _ROUTER
    with _ROUTER_LOCK:
        if _ROUTER is None:
            _ROUTER = ProviderRouter()
        return _ROUTER


def reset_router() -> None:
    """Test hook: drop the shared router."""
    global _ROUTER
    with _ROUTER_LOCK:
        _ROUTER = None
