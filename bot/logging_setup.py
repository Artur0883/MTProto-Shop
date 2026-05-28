"""Structured logging setup using structlog.

Produces JSON output in production and pretty console output in dev.
Routes stdlib `logging` (from aiogram, aiohttp, aiosqlite) through structlog
processors so every log line — ours or third-party — gets the same format.
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog + stdlib logging.

    Args:
        level: log level name (DEBUG, INFO, WARNING, ERROR)
        fmt:   "json" for production, "console" for local development
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _mask_secrets,
    ]

    if fmt == "console":
        renderer: Any = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    for noisy in ("aiogram.event", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


_SECRET_KEY_PARTS = ("secret", "token", "password", "admin_id")
_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
_HEX_SECRET_RE = re.compile(r"\b[0-9a-fA-F]{32}\b")


def _mask_string(value: str) -> str:
    masked = _TOKEN_RE.sub("***masked***", value)
    masked = _HEX_SECRET_RE.sub("***masked***", masked)
    return masked


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > 10:
            return f"{value[:6]}...{value[-4:]}"
        return "***"
    if isinstance(value, int):
        return "***"
    if isinstance(value, dict):
        return {
            key: _mask_value(item) if _is_secret_key(str(key)) else _mask_nested(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_nested(item) for item in value]
    return value


def _mask_nested(value: Any) -> Any:
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, dict):
        return {
            key: _mask_value(item) if _is_secret_key(str(key)) else _mask_nested(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_nested(item) for item in value]
    return value


def _is_secret_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _mask_secrets(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Mask secret-looking values everywhere in the event dict.

    Reduces the risk of leaking bot tokens or proxy secrets into logs.
    """
    for key, value in list(event_dict.items()):
        if _is_secret_key(str(key)):
            event_dict[key] = _mask_value(value)
        else:
            event_dict[key] = _mask_nested(value)
    return event_dict
