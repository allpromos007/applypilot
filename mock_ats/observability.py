"""Structured logging + trace joining for the Mock ATS (rubrics 4.1, 4.3, 4.4).

- Reads the W3C `traceparent` header the agent sends, so Mock ATS log lines
  carry the SAME trace id as the agent's spans. In Cloud Logging the
  `logging.googleapis.com/trace` field links each line to that Cloud Trace trace.
- JSON lines on stdout (Cloud Run parses them into jsonPayload).
- Emails/phones are redacted before writing; the ATS stores PII by design
  (it is the employer's system), but its logs never contain it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\w)"
)
_TRACEPARENT = re.compile(r"^[\da-f]{2}-([\da-f]{32})-([\da-f]{16})-[\da-f]{2}$")


def redact(text: str) -> str:
    return _PHONE.sub("[REDACTED_PHONE]", _EMAIL.sub("[REDACTED_EMAIL]", text))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "timestamp": datetime.now(UTC).isoformat(),
            "service": "mock-ats",
            "message": redact(record.getMessage()),
            **{
                k: (redact(v) if isinstance(v, str) else v)
                for k, v in getattr(record, "fields", {}).items()
            },
        }
        trace_id = getattr(record, "trace_id", None)
        if trace_id:
            payload["trace_id"] = trace_id
            project = os.getenv("GOOGLE_CLOUD_PROJECT")
            if project:
                payload["logging.googleapis.com/trace"] = (
                    f"projects/{project}/traces/{trace_id}"
                )
            if getattr(record, "span_id", None):
                payload["logging.googleapis.com/spanId"] = record.span_id
        return json.dumps(payload, ensure_ascii=False)


def get_logger() -> logging.Logger:
    logger = logging.getLogger("mock_ats")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def parse_traceparent(header: str | None) -> tuple[str | None, str | None]:
    match = _TRACEPARENT.match((header or "").strip().lower())
    return (match.group(1), match.group(2)) if match else (None, None)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """One structured line per request, tagged with the caller's trace id."""

    async def dispatch(self, request, call_next):
        trace_id, parent_span = parse_traceparent(request.headers.get("traceparent"))
        request.state.trace_id = trace_id
        start = time.perf_counter()
        response = await call_next(request)
        get_logger().info(
            f"{request.method} {request.url.path} -> {response.status_code}",
            extra={
                "trace_id": trace_id,
                "span_id": parent_span,
                "fields": {
                    "event_type": "HTTP_REQUEST",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            },
        )
        return response
