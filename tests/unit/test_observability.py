"""Unit tests for M4 observability: PII scrubbing, JSON logs, intent/outcome, tracing."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.observability import pii
from app.observability.logging import LOGGER_ROOT, build_handler, log_event
from app.observability.pii import PIIScrubber
from app.observability.plugin import ObservabilityPlugin, summarize_result
from mock_ats.observability import JsonFormatter, parse_traceparent

# --------------------------------------------------------------------------- #
# PII scrubber (4.4)
# --------------------------------------------------------------------------- #
REGEX_ONLY = PIIScrubber(use_dlp=False)
run = asyncio.run


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("mail maya.lin@example.com now", "EMAIL"),
        ("call (206) 555-0184", "PHONE"),
        ("ssn 123-45-6789", "SSN"),
        ("card 4111 1111 1111 1111", "CREDIT_CARD"),
        (
            "key " + "AIza" + "x" * 35,
            "API_KEY",
        ),  # built at runtime: no key-shaped literal in git
        ("Authorization: " + "Bearer " + "a" * 32, "TOKEN"),
        ("see linkedin.com/in/mayalindev", "LINKEDIN"),
        ("code at https://github.com/mayalindev", "GITHUB"),
    ],
)
def test_regex_redacts_common_pii(raw, label):
    out = REGEX_ONLY.scrub_text(raw)
    assert f"[REDACTED_{label}]" in out


def test_regex_keeps_non_pii_numbers():
    text = "Cut latency 34% for 140,000 patients; order 1234567812345678 is not a card"
    assert REGEX_ONLY.scrub_text(text) == text  # fails Luhn -> not a card


def test_known_values_are_always_redacted():
    out = REGEX_ONLY.scrub_text("Maya Lin applied", known_values=["Maya Lin"])
    assert out == "[REDACTED_KNOWN_PII] applied"


def test_scrub_nested_masks_sensitive_keys():
    data = {
        "candidate": {"full_name": "Maya Lin", "email": "m@x.io"},
        "note": "reach me at m@x.io",
        "items": ["555-123-4567", 3],
    }
    out = REGEX_ONLY.scrub(data)
    assert out["candidate"] == {"full_name": "[REDACTED]", "email": "[REDACTED]"}
    assert out["note"] == "reach me at [REDACTED_EMAIL]"
    assert out["items"] == ["[REDACTED_PHONE]", 3]


def test_dlp_is_used_when_enabled(monkeypatch):
    calls = []

    def fake_dlp(text, project_id):
        calls.append(project_id)
        return text.replace("Maya", "[REDACTED_PERSON_NAME]")

    monkeypatch.setattr(pii, "redact_with_dlp", fake_dlp)
    scrubber = PIIScrubber(use_dlp=True, project_id="l200capstone")
    out = scrubber.scrub_text("Maya can be reached at maya@example.com")
    assert calls == ["l200capstone"]
    assert "[REDACTED_PERSON_NAME]" in out and "[REDACTED_EMAIL]" in out


def test_dlp_failure_falls_back_to_regex(monkeypatch):
    class PermissionDenied(Exception):  # same class name as google.api_core's
        pass

    def broken(text, project_id):
        raise PermissionDenied("DLP API disabled")

    monkeypatch.setattr(pii, "redact_with_dlp", broken)
    scrubber = PIIScrubber(use_dlp=True, project_id="p")
    assert "[REDACTED_EMAIL]" in scrubber.scrub_text(
        "contact me at maya@example.com please"
    )
    assert scrubber._dlp_failed  # stops retrying the broken API


# --------------------------------------------------------------------------- #
# Structured logging (4.1)
# --------------------------------------------------------------------------- #
@pytest.fixture
def log_lines():
    """Attach a fresh JSON handler writing to a buffer; yield a reader."""
    buf = io.StringIO()
    root = logging.getLogger(LOGGER_ROOT)
    saved = root.handlers
    root.handlers = [build_handler(buf)]
    root.setLevel(logging.INFO)
    root.propagate = False
    yield lambda: [
        rec
        for rec in (json.loads(line) for line in buf.getvalue().splitlines() if line)
        if "event_type" in rec  # skip the library's one-off diagnostic entry
    ]
    root.handlers = saved


def test_log_event_is_json_with_severity_and_redaction(log_lines):
    logger = logging.getLogger(f"{LOGGER_ROOT}.test")
    log_event(
        logger,
        "TEST_EVENT",
        "user maya@example.com said hi",
        severity=logging.WARNING,
        detail="phone 206-555-0184",
        known_pii=["Maya Lin"],
        who="Maya Lin",
    )
    (line,) = log_lines()
    assert line["severity"] == "WARNING"
    assert line["event_type"] == "TEST_EVENT"
    assert "[REDACTED_EMAIL]" in line["message"]
    assert line["detail"] == "phone [REDACTED_PHONE]"
    assert line["who"] == "[REDACTED_KNOWN_PII]"


# --------------------------------------------------------------------------- #
# Intent vs outcome (4.2)
# --------------------------------------------------------------------------- #
def _tool_ctx(call_id="fc-1", confirmation=None):
    return SimpleNamespace(
        function_call_id=call_id,
        invocation_id="inv-1",
        agent_name="applypilot",
        session=SimpleNamespace(id="sess-1"),
        state={"resume_path": "samples/resumes/maya_lin_healthcare_full_stack.pdf"},
        tool_confirmation=confirmation,
        actions=SimpleNamespace(state_delta={"application_history": []}),
    )


def test_intent_and_outcome_are_paired_and_scrubbed(log_lines):
    plugin = ObservabilityPlugin()
    tool = SimpleNamespace(
        name="submit_job_application",
        description="Submit the prepared application.\nMore details.",
    )
    ctx = _tool_ctx(confirmation=SimpleNamespace(confirmed=True))
    args = {"job_id": "greenhouse:acme:42", "note": "from maya.lin.dev@email.com"}
    run(plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=ctx))
    run(
        plugin.after_tool_callback(
            tool=tool,
            tool_args=args,
            tool_context=ctx,
            result={"status": "success", "application_id": "app_1", "note": "Maya Lin"},
        )
    )
    intent, outcome = log_lines()
    assert (
        intent["event_type"] == "AGENT_INTENT"
        and outcome["event_type"] == "AGENT_OUTCOME"
    )
    assert intent["action_id"] == outcome["action_id"] == "fc-1"
    assert intent["tool_purpose"] == "Submit the prepared application."
    assert intent["high_stakes"] is True and intent["human_decision"] == "approved"
    assert (
        intent["arguments"]["note"] == "from [REDACTED_KNOWN_PII]"
    )  # known value wins
    assert outcome["status"] == "success" and outcome["achieved_intent"] is True
    assert outcome["result_summary"]["note"] == "[REDACTED_KNOWN_PII]"
    assert isinstance(outcome["latency_ms"], int)
    assert outcome["session_id"] == "sess-1"
    assert "maya.lin.dev@email.com" not in json.dumps([intent, outcome])


def test_error_outcome_carries_error_code(log_lines):
    plugin = ObservabilityPlugin()
    tool = SimpleNamespace(name="select_target_job", description="Choose the job.")
    ctx = _tool_ctx("fc-2")
    run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"job_id": "x"}, tool_context=ctx
        )
    )
    run(
        plugin.after_tool_callback(
            tool=tool,
            tool_args={"job_id": "x"},
            tool_context=ctx,
            result={
                "status": "error",
                "error_code": "JOB_NOT_FOUND",
                "recovery_instruction": "Pick again.",
            },
        )
    )
    _, outcome = log_lines()
    assert outcome["severity"] == "WARNING"
    assert outcome["achieved_intent"] is False
    assert outcome["error_code"] == "JOB_NOT_FOUND"


def test_summarize_result_keeps_logs_small():
    summary = summarize_result(
        {"status": "success", "jobs": [1, 2, 3], "text": "x" * 1000}
    )
    assert summary["jobs"] == "<list len=3>"
    assert len(summary["text"]) < 400


# --------------------------------------------------------------------------- #
# Distributed tracing (4.3)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        pytest.skip("a global tracer provider was already installed")
    return exporter


def test_ats_call_is_traced_and_propagates_traceparent(spans, tmp_path):
    from app.tools.application_tools import MockATSAdapter
    from app.tools.document_tools import render_ats_docx

    seen = {}

    def handler(request):
        seen["traceparent"] = request.headers.get("traceparent")
        return httpx.Response(
            201, json={"application_id": "app_9", "status": "RECEIVED"}
        )

    doc = render_ats_docx("Maya Lin\nEXPERIENCE\n- Built.", {}, tmp_path / "r.docx")
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("user_request") as root:
        result = MockATSAdapter("http://ats.test", httpx.MockTransport(handler)).submit(
            "greenhouse:acme:1", {"email": "maya@example.com"}, doc, doc
        )
    assert result["status"] == "success"

    trace_id, _ = parse_traceparent(seen["traceparent"])
    assert (
        trace_id == f"{root.get_span_context().trace_id:032x}"
    )  # same trace end to end

    span = next(
        s for s in spans.get_finished_spans() if s.name == "mock_ats.submit_application"
    )
    assert span.parent.span_id == root.get_span_context().span_id
    assert span.attributes["applypilot.outcome.status"] == "success"
    assert span.attributes["applypilot.application_id"] == "app_9"


def test_mock_ats_log_line_joins_the_trace(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "l200capstone")
    record = logging.LogRecord(
        "mock_ats", logging.INFO, __file__, 1, "from maya.lin.dev@email.com", (), None
    )
    record.trace_id = "0af7651916cd43dd8448eb211c80319c"
    record.fields = {"event_type": "HTTP_REQUEST"}
    line = json.loads(JsonFormatter().format(record))
    assert line["logging.googleapis.com/trace"] == (
        "projects/l200capstone/traces/0af7651916cd43dd8448eb211c80319c"
    )
    assert line["message"] == "from [REDACTED_EMAIL]"


def test_known_name_redacted_in_file_names():
    out = REGEX_ONLY.scrub_text(
        "file maya_lin_resume.pdf / maya-lin", known_values=["Maya Lin"]
    )
    assert "maya" not in out.lower()


def test_transient_dlp_error_does_not_disable_dlp(monkeypatch):
    class InvalidArgument(Exception):
        message = "bad request"

    calls = []

    def flaky(text, project_id):
        calls.append(1)
        if len(calls) == 1:
            raise InvalidArgument()
        return text

    monkeypatch.setattr(pii, "redact_with_dlp", flaky)
    scrubber = PIIScrubber(use_dlp=True, project_id="p")
    scrubber.scrub_text("first text that is long enough for dlp")
    scrubber.scrub_text("second text that is long enough for dlp")
    assert len(calls) == 2 and not scrubber._dlp_failed


def test_logging_path_never_calls_dlp(monkeypatch):
    monkeypatch.setattr(
        pii,
        "redact_with_dlp",
        lambda *a: (_ for _ in ()).throw(AssertionError("DLP on hot path")),
    )
    assert not pii.get_log_scrubber().use_dlp
    assert "[REDACTED_EMAIL]" in pii.get_log_scrubber().scrub_text(
        "mail maya@example.com now please"
    )
