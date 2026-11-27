"""JSON log lines for the gateway (CONTRACTS section 13).

uvicorn configures logging before it imports the app factory, so `configure()` runs afterwards and
takes over: one handler on the root logger writing one JSON object per line to stderr, with uvicorn's
own loggers propagating into it. Access logs stay off (the supervisor passes `--no-access-log`,
because query strings carry email verification and password reset tokens).

Each line carries `at`, `level`, `logger` and `message`. An exception adds `error` (the exception's
class name) and `where` (file and line), never the traceback text or the exception message: those can
quote customer data.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from typing import Any

QUIET_LOGGERS = ("httpx", "httpcore", "multipart")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            exc_type, _, tb = record.exc_info
            payload["error"] = getattr(exc_type, "__name__", "Exception")
            frames = traceback.extract_tb(tb)
            if frames:
                payload["where"] = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}"
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure(level: str = "INFO", stream: Any = None) -> logging.Handler:
    """Install the JSON handler as the only root handler and route uvicorn's loggers into it."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler
