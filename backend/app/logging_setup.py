"""Structured JSON logging with a request ID on every line and secret redaction."""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_SENSITIVE_KEY = re.compile(r"pass(word)?|secret|token|authorization|jwt|cookie|hash", re.I)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_LIKE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^:/@\s]+):([^@\s]+)@")
REDACTED = "[REDACTED]"

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def redact_text(text: str) -> str:
    text = _BEARER.sub("Bearer " + REDACTED, text)
    text = _JWT_LIKE.sub(REDACTED, text)
    return _URL_CREDENTIALS.sub(r"\1\2:" + REDACTED + "@", text)


def redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(key, v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "request_id": request_id_var.get(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact_value(key, value)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # Uvicorn's access log prints request lines only; it never includes headers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
