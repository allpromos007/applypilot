"""OpenTelemetry tracing helpers (rubric 4.3).

ADK already creates spans for each agent run, LLM call and tool call. We add
custom spans around our own work (document rendering, the ATS HTTP call), and
propagate W3C `traceparent` headers to the Mock ATS so one trace covers
user query -> coordinator -> tool -> Mock ATS -> answer.

Where spans go:
  - `agents-cli playground` / `adk web`: the Trace tab of the dev UI.
  - Agent Engine with telemetry enabled, or the FastAPI server with
    GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true: Cloud Trace.
We never configure a provider here; we use whatever ADK/Agent Engine installed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.trace import Span, Status, StatusCode

from app.observability.pii import get_log_scrubber as get_scrubber

tracer = trace.get_tracer("applypilot")


def _attr(value: Any) -> Any:
    if isinstance(value, (bool, int, float)):
        return value
    return get_scrubber().scrub_text(str(value))[:500]


@contextlib.contextmanager
def traced(name: str, **attributes: Any) -> Iterator[Span]:
    """Start a child span with PII-scrubbed `applypilot.*` attributes."""
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(f"applypilot.{key}", _attr(value))
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def mark_outcome(span: Span, result: dict[str, Any]) -> None:
    """Record a tool-style result dict's status on a span."""
    status = str(result.get("status", "unknown"))
    span.set_attribute("applypilot.outcome.status", status)
    if status == "error":
        span.set_attribute(
            "applypilot.outcome.error_code", str(result.get("error_code"))
        )
        span.set_status(Status(StatusCode.ERROR, str(result.get("error_code"))))


def trace_headers() -> dict[str, str]:
    """W3C trace-context headers for outgoing HTTP calls (distributed tracing)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier
