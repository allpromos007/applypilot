"""Structured JSON logging with trace correlation and PII redaction (rubric 4.1).

Uses Google Cloud Logging's `StructuredLogHandler`: every record is one JSON
line on stdout, which Cloud Logging on Agent Engine / Cloud Run parses into
`jsonPayload` with proper severity, and links to the Cloud Trace span via
`logging.googleapis.com/trace`. Locally you get the same JSON in the terminal.

Every record passes through `PIIRedactionFilter` BEFORE it is formatted, so no
handler ever sees raw PII.

Usage:
    from app.observability.logging import get_logger, log_event
    log_event(get_logger(__name__), "AGENT_INTENT", tool="submit_job_application", ...)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from opentelemetry import trace

from app.observability.pii import get_scrubber

_CONFIGURED = False
LOGGER_ROOT = "applypilot"


def current_trace_ids() -> dict[str, str]:
    """trace_id/span_id of the active OpenTelemetry span (empty if none)."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": f"{ctx.trace_id:032x}", "span_id": f"{ctx.span_id:016x}"}


class PIIRedactionFilter(logging.Filter):
    """Scrubs the message and structured fields of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        scrubber = get_scrubber()
        known = getattr(record, "known_pii", ()) or ()
        record.msg = scrubber.scrub_text(record.getMessage(), known)
        record.args = ()
        fields = getattr(record, "json_fields", None)
        if isinstance(fields, dict):
            record.json_fields = scrubber.scrub(fields, known)
        return True


class TraceContextFilter(logging.Filter):
    """Adds trace_id/span_id so logs can be joined with traces."""

    def filter(self, record: logging.LogRecord) -> bool:
        fields = dict(getattr(record, "json_fields", None) or {})
        for key, value in current_trace_ids().items():
            fields.setdefault(key, value)
        record.json_fields = fields
        return True


def build_handler(stream: Any = None) -> logging.Handler:
    """Cloud Logging JSON handler with trace + PII filters (stream defaults to stdout)."""
    from google.cloud.logging.handlers import StructuredLogHandler

    handler = StructuredLogHandler(
        stream=stream or sys.stdout,
        project_id=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
        labels={"service": "applypilot"},
    )
    # Run ours first so the Cloud Logging filter only ever sees redacted data.
    handler.filters[:0] = [TraceContextFilter(), PIIRedactionFilter()]
    return handler


def configure_logging(level: str | None = None) -> None:
    """Idempotently attach the structured handler to the `applypilot` logger tree."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger(LOGGER_ROOT)
    root.handlers = [build_handler()]
    root.setLevel(level or os.getenv("APPLYPILOT_LOG_LEVEL", "INFO"))
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Logger under the `applypilot` tree (configures logging on first use)."""
    configure_logging()
    short = name.removeprefix("app.")
    return logging.getLogger(f"{LOGGER_ROOT}.{short}")


def log_event(
    logger: logging.Logger,
    event_type: str,
    message: str = "",
    *,
    severity: int = logging.INFO,
    known_pii: list[str] | tuple[str, ...] = (),
    **fields: Any,
) -> None:
    """Emit one structured event. `fields` become top-level JSON keys."""
    logger.log(
        severity,
        message or event_type,
        extra={
            "json_fields": {"event_type": event_type, **fields},
            "known_pii": known_pii,
        },
    )
