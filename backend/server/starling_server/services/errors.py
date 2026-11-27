"""Error type and JSON error bodies shared by every services route.

Every error body follows the CONTRACTS convention `{"error": {"code": "...", "message": "..."}}`.
Messages are fixed, human-readable sentences; they never echo prompts, outputs, keys, tokens or
customer URLs. A refusal may add machine-readable fields beside them (`details`: the web search's
`reason` and `retry_after_s`, see world/search.py), never anything a customer wrote.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class ServiceError(Exception):
    """A refusal or failure that maps to an HTTP status and a stable error code."""

    def __init__(self, http_status: int, code: str, message: str, retry_after_s: int | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{http_status} {code}")
        self.http_status = http_status
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s
        self.details = dict(details) if details else None


def error_response(http_status: int, code: str, message: str, retry_after_s: int | None = None,
                   details: dict[str, Any] | None = None) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after_s is not None:
        headers["Retry-After"] = str(max(0, int(retry_after_s)))
    error: dict[str, Any] = {"code": code, "message": message}
    for key, value in (details or {}).items():
        if key not in error:
            error[key] = value
    return JSONResponse({"error": error}, status_code=http_status, headers=headers)


def service_error_response(exc: ServiceError) -> JSONResponse:
    return error_response(exc.http_status, exc.code, exc.message, exc.retry_after_s,
                          getattr(exc, "details", None))
