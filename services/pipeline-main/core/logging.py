"""
Pipeline-main — Structured JSON logger.

Identical API to orchestrator/core/logging.py.
Kept as a separate file so each service can customise severity defaults
or add service-specific context without coupling to the orchestrator.

Usage::

    from core.logging import get_logger
    log = get_logger(__name__)
    log.info("serper_query_sent", query=q[:60], tenant=tid[:8])
    log.warning("neg_shield_timeout", timeout_s=3.0)
    log.error("gemini_failure", error=str(e))
"""
from __future__ import annotations

import json
import logging
import sys
import datetime
from typing import Any


class _GCPJsonFormatter(logging.Formatter):
    _SEVERITY_MAP: dict[int, str] = {
        logging.DEBUG:    "DEBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARNING",
        logging.ERROR:    "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        payload: dict[str, Any] = {
            "time":     datetime.datetime.utcnow().isoformat() + "Z",
            "severity": self._SEVERITY_MAP.get(record.levelno, "DEFAULT"),
            "logger":   record.name,
            "message":  record.getMessage(),
        }
        if hasattr(record, "extra"):
            payload.update(record.extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _BoundLogger:
    def __init__(self, logger: logging.Logger, context: dict[str, Any]) -> None:
        self._logger = logger
        self._ctx = context

    def bind(self, **kwargs: Any) -> "_BoundLogger":
        """Return new BoundLogger with additional context fields."""
        return _BoundLogger(self._logger, {**self._ctx, **kwargs})

    def _emit(self, level: int, event: str, **kwargs: Any) -> None:
        extra = {**self._ctx, **kwargs}
        record = self._logger.makeRecord(
            self._logger.name, level, fn="", lno=0,
            msg=event, args=(), exc_info=None,
        )
        record.extra = extra  # type: ignore[attr-defined]
        self._logger.handle(record)

    def debug(self, event: str, **kwargs: Any) -> None:
        """Emit DEBUG log."""
        self._emit(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        """Emit INFO log."""
        self._emit(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        """Emit WARNING log."""
        self._emit(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        """Emit ERROR log."""
        self._emit(logging.ERROR, event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        """Emit CRITICAL log."""
        self._emit(logging.CRITICAL, event, **kwargs)


def get_logger(name: str, **context: Any) -> _BoundLogger:
    """Return a structured JSON bound logger.

    Args:
        name:    Module name (pass ``__name__``).
        **context: Initial context fields merged into every record.

    Returns:
        :class:`_BoundLogger` instance.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_GCPJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return _BoundLogger(logger, context)
