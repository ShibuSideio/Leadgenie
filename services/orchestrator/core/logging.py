"""
Sideio Lead Sniper — Structured JSON logger.

Wraps Python's standard ``logging`` module with GCP Cloud Logging-compatible
JSON output.  All service code calls ``get_logger(__name__)`` rather than
``print()``.

GCP Cloud Logging integration:
  Cloud Run captures stdout and ingests it into Cloud Logging.  When each log
  line is a JSON object with a ``severity`` key, the Log Explorer can filter
  on severity, operation, and resource labels — something ``print()`` cannot
  provide.

Usage::

    from core.logging import get_logger
    log = get_logger(__name__)

    log.info("shadow_tracker_upserted", ngrams=5, persona="SaaS CFO", tenant=uid[:8])
    log.warning("neg_shield_timeout", timeout_s=3.0, tenant=uid[:8])
    log.error("bq_insert_failed", error=str(e), table="Negative_Signals")
"""
from __future__ import annotations

import json
import logging
import sys
import datetime
from typing import Any


class _GCPJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON for GCP Cloud Logging."""

    # Maps Python log levels to GCP severity strings
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
        # Merge any extra fields bound to the record (via log.info("msg", key=val))
        if hasattr(record, "extra"):
            payload.update(record.extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _BoundLogger:
    """Thin wrapper that injects context fields into every log call.

    Args:
        logger:  The underlying :class:`logging.Logger` instance.
        context: Key-value pairs merged into every emitted log record.
    """

    def __init__(self, logger: logging.Logger, context: dict[str, Any]) -> None:
        self._logger = logger
        self._ctx = context

    def bind(self, **kwargs: Any) -> "_BoundLogger":
        """Return a new BoundLogger with additional context fields."""
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
        """Emit a DEBUG-level structured log."""
        self._emit(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        """Emit an INFO-level structured log."""
        self._emit(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        """Emit a WARNING-level structured log."""
        self._emit(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        """Emit an ERROR-level structured log."""
        self._emit(logging.ERROR, event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        """Emit a CRITICAL-level structured log."""
        self._emit(logging.CRITICAL, event, **kwargs)


def get_logger(name: str, **context: Any) -> _BoundLogger:
    """Return a structured JSON logger bound with optional context fields.

    Args:
        name:    Module or component name (use ``__name__``).
        **context: Initial context key-value pairs merged into every record.

    Returns:
        A :class:`_BoundLogger` instance.

    Example::

        log = get_logger(__name__, service="orchestrator")
        log.info("request_received", path="/api/campaigns", tenant=tid[:8])
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_GCPJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return _BoundLogger(logger, context)
