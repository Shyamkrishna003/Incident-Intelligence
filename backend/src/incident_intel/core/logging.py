"""Structured JSON logging with secret redaction.

All log output (our own structlog loggers and stdlib loggers such as uvicorn's) goes through
one formatter so that redaction applies uniformly.
"""

import logging
import re
import sys
from collections.abc import Mapping
from typing import IO, Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED = "[REDACTED]"

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "x-api-key",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "password",
        "secret",
        "cookie",
        "set-cookie",
        "pepper",
        "api_key_pepper",
        "key_hash",
        "database_url",
    }
)

_SECRET_PATTERNS = (
    # Incident Intelligence API keys: ii_<prefix>_<secret>.
    re.compile(r"ii_[a-z0-9]{12}_[A-Za-z0-9_-]{20,}"),
    # Any bearer credential that ends up inside a message.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    # Google API keys (for example the Gemini key).
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    # name=value or name: value where the name says the value is a secret.
    re.compile(r"(?i)(?<=\b)(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"),
)

_MAX_DEPTH = 8


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS
            else _scrub(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_scrub(item, depth + 1) for item in value)
    return value


def scrub_text(text: str) -> str:
    """Redact secret-looking values inside free text (also used before text from
    monitored systems is sent to an LLM)."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_sensitive(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: mask sensitive keys and secret-looking substrings."""
    scrubbed: EventDict = _scrub(event_dict)
    return scrubbed


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def build_formatter(*, json: bool) -> structlog.stdlib.ProcessorFormatter:
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            # Redaction runs last before rendering so it sees tracebacks and foreign logs too.
            redact_sensitive,
            renderer,
        ],
    )


def configure_logging(*, level: str, json: bool, stream: IO[str] | None = None) -> None:
    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(build_formatter(json=json))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Route uvicorn through the root handler; request logging is done by our middleware.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
