"""
Structured JSON logging with trace/correlation IDs.

Usage:
    from brain.core.log import get_logger, set_trace_id, get_trace_id, bind

    logger = get_logger(__name__)
    logger.info("something happened", extra={"workspace_id": ws})

Every log record emits newline-delimited JSON to stdout with:
    timestamp, level, logger, message, trace_id, span_id (optional),
    workspace_id (optional), plus any extra fields passed in.

Log level is controlled by the LOG_LEVEL env var (default: INFO).
"""

import json
import logging
import os
import sys
import datetime
from contextvars import ContextVar
from typing import Any

# ── Context variables propagated per-request ─────────────────────────────────

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_span_id_var: ContextVar[str] = ContextVar("span_id", default="")
_workspace_id_var: ContextVar[str] = ContextVar("workspace_id", default="")
_extra_ctx: ContextVar[dict] = ContextVar("log_extra", default={})


def get_trace_id() -> str:
    return _trace_id_var.get()


def set_trace_id(id: str) -> None:
    _trace_id_var.set(id)


def get_span_id() -> str:
    return _span_id_var.get()


def set_span_id(id: str) -> None:
    _span_id_var.set(id)


def bind(key: str, value: Any) -> None:
    """Attach a key-value pair to the current request context (propagated in all logs)."""
    current = _extra_ctx.get()
    _extra_ctx.set({**current, key: value})


def _get_bound() -> dict:
    return _extra_ctx.get()


# ── JSON formatter ─────────────────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    """Formats log records as newline-delimited JSON. Never raises."""

    # Fields we MUST NOT log (security)
    _BLOCKED = frozenset({
        "query", "answer", "email", "api_key", "token", "password",
        "secret", "authorization",
    })

    def format(self, record: logging.LogRecord) -> str:
        try:
            doc: dict = {
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                "level":     record.levelname,
                "logger":    record.name,
                "message":   record.getMessage(),
                "trace_id":  get_trace_id() or None,
            }
            span = get_span_id()
            if span:
                doc["span_id"] = span
            ws = _workspace_id_var.get()
            if ws:
                doc["workspace_id"] = ws

            # Merge per-request bound context
            for k, v in _get_bound().items():
                if k not in self._BLOCKED:
                    doc[k] = v

            # Merge extra fields passed to the log call
            if hasattr(record, "__dict__"):
                skip = {
                    "name", "msg", "args", "levelname", "levelno", "pathname",
                    "filename", "module", "exc_info", "exc_text", "stack_info",
                    "lineno", "funcName", "created", "msecs", "relativeCreated",
                    "thread", "threadName", "processName", "process", "message",
                    "taskName",
                }
                for k, v in record.__dict__.items():
                    if k not in skip and not k.startswith("_"):
                        if k not in self._BLOCKED:
                            doc[k] = v

            # Exception info
            if record.exc_info:
                import traceback as _tb
                doc["exception"] = _tb.format_exception(*record.exc_info)

            return json.dumps(doc, default=str)
        except Exception as e:
            # Last-resort fallback — must never throw
            return json.dumps({"level": "ERROR", "message": f"log formatter error: {e}",
                               "original": str(record.getMessage())})


# ── Logger factory ─────────────────────────────────────────────────────────────

_handler: logging.Handler | None = None
_log_level: int | None = None


def _ensure_handler() -> None:
    global _handler, _log_level
    if _handler is not None:
        return
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    _log_level = getattr(logging, level_name, logging.INFO)
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(_JsonFormatter())
    _handler.setLevel(_log_level)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that emits structured JSON to stdout."""
    _ensure_handler()
    logger = logging.getLogger(name)
    # Avoid adding duplicate handlers if called multiple times
    if not any(isinstance(h, logging.StreamHandler) and
               isinstance(h.formatter, _JsonFormatter)
               for h in logger.handlers):
        logger.addHandler(_handler)
    logger.setLevel(_log_level)
    logger.propagate = False
    return logger
