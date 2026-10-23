"""JSON logs on stdout (structlog), with request ids and secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_SECRET_KEYS = {"password", "token", "authorization", "cookie", "api_key", "openai_api_key"}
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}")


def _redact(_logger: Any, _method: str, event: dict[str, Any]) -> dict[str, Any]:
    for key, value in event.items():
        if key.lower() in _SECRET_KEYS:
            event[key] = "[REDACTED]"
        elif isinstance(value, str):
            event[key] = _API_KEY.sub("[REDACTED]", value)
    return event


def configure_logging(level: str = "INFO") -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        _redact,
    ]
    renderer = structlog.processors.JSONRenderer()
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),  # writes to the current sys.stdout
        cache_logger_on_first_use=False,
    )
    # Route library logging (uvicorn, alembic, openai, httpx) through the same JSON output.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").disabled = True  # requests are logged by our middleware
    for noisy in ("pdfminer", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
