"""Unit tests for M3: document rendering, ATS client, and the human-approval gate.

No network and no LLM: the ATS is either an httpx.MockTransport or the real
Mock ATS app running in-process via FastAPI's TestClient.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import docx
import httpx
import pytest
from fastapi.testclient import TestClient

from app.tools import session_tools
from app.tools.application_tools import MockATSAdapter
from app.tools.document_tools import fill_contact_placeholders, render_ats_docx
from app.tools.errors import ToolErrorCode
from mock_ats.main import create_app
from mock_ats.store import SQLiteApplicationStore

MAYA = "samples/resumes/maya_lin_healthcare_full_stack.pdf"
CONTACT = {"name": "Maya Lin", "email": "maya@example.com", "phone": "555-0100"}
TAILORED = """Maya Lin
Seattle, WA | [EMAIL] | [PHONE] | [LINKEDIN]
SUMMARY
Full stack engineer building **FHIR R4** patient apps.
SKILLS
React, TypeScript, AWS
EXPERIENCE
- Built a patient portal serving 140,000+ patients.
"""


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
def test_fill_placeholders_drops_missing_and_tidies_separators():
    out = fill_contact_placeholders("Seattle | [EMAIL] | [PHONE] | [LINKEDIN]", CONTACT)
    assert out == "Seattle | maya@example.com | 555-0100"


def test_render_docx_is_ats_friendly(tmp_path):
    path = render_ats_docx(TAILORED, CONTACT, tmp_path / "r.docx")
    document = docx.Document(str(path))
    paras = [(p.style.name, p.text) for p in document.paragraphs]
    text = "\n".join(t for _, t in paras)
    assert "maya@example.com" in text and "[EMAIL]" not in text and "**" not in text
    assert ("Heading 2", "EXPERIENCE") in paras
    assert ("List Bullet", "Built a patient portal serving 140,000+ patients.") in paras
    assert ("Normal", "React, TypeScript, AWS") in paras  # not mistaken for a heading
    assert not document.tables  # tables confuse ATS parsers


# --------------------------------------------------------------------------- #
# ATS client: HTTP status -> guided result
# --------------------------------------------------------------------------- #
@pytest.fixture
def docs(tmp_path):
    r = render_ats_docx(TAILORED, CONTACT, tmp_path / "r.docx")
    c = render_ats_docx("Dear Hiring Team,\nThanks.", CONTACT, tmp_path / "c.docx")
    return r, c


@pytest.mark.parametrize(
    ("status", "body", "expected_code"),
    [
        (
            409,
            {"details": {"existing_application_id": "app_1"}},
            ToolErrorCode.DUPLICATE_APPLICATION,
        ),
        (
            422,
            {"details": {"problems": ["resume: empty"]}},
            ToolErrorCode.APPLICATION_REJECTED,
        ),
        (503, {}, ToolErrorCode.ATS_UNAVAILABLE),
    ],
)
def test_adapter_maps_http_errors(docs, status, body, expected_code):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, json=body))
    result = MockATSAdapter("http://ats.test", transport).submit("j1", {}, *docs)
    assert result["status"] == "error"
    assert result["error_code"] == expected_code
    assert result["recovery_instruction"]


def test_adapter_reports_unreachable_ats(docs):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    result = MockATSAdapter("http://ats.test", httpx.MockTransport(boom)).submit(
        "j1", {}, *docs
    )
    assert result["error_code"] == ToolErrorCode.ATS_UNAVAILABLE
    assert "Nothing was submitted" in result["recovery_instruction"]


# --------------------------------------------------------------------------- #
# Full apply flow with the human-approval gate, against the real Mock ATS app
# --------------------------------------------------------------------------- #
class FakeToolContext(SimpleNamespace):
    """Mimics what ADK provides: state, tool_confirmation, request_confirmation()."""

    def __init__(self):
        super().__init__(state={}, tool_confirmation=None, actions=SimpleNamespace())
        self.requested = None

    def request_confirmation(self, *, hint=None, payload=None):
        self.requested = {"hint": hint, "payload": payload}


@pytest.fixture
def ats(tmp_path, monkeypatch):
    """Route the agent's ATS calls into an in-process Mock ATS."""
    client = TestClient(create_app(SQLiteApplicationStore(tmp_path / "ats")))

    class InProcessAdapter:
        def submit(self, job_id, candidate, resume_path, cover_letter_path):
            with open(resume_path, "rb") as r, open(cover_letter_path, "rb") as c:
                resp = client.post(
                    "/applications",
                    data={"job_id": job_id, "candidate": json.dumps(candidate)},
                    files={
                        "resume": (resume_path.name, r),
                        "cover_letter": (cover_letter_path.name, c),
                    },
                )
            transport = httpx.MockTransport(
                lambda req: httpx.Response(resp.status_code, json=resp.json())
            )
            return MockATSAdapter("http://ats.test", transport).submit(
                job_id, candidate, resume_path, cover_letter_path
            )

    monkeypatch.setattr(session_tools, "ADAPTER_FACTORY", InProcessAdapter)
    monkeypatch.setenv("APPLYPILOT_OUTPUT_DIR", str(tmp_path / "out"))
    return client


@pytest.fixture
def ready_ctx():
    ctx = FakeToolContext()
    session_tools.load_candidate_resume(MAYA, ctx)
    ctx.state.update(
        target_job_id="greenhouse:acme:42",
        target_job_title="Senior Full Stack Engineer",
        target_company="Acme Health",
        tailored_resume=TAILORED,
        cover_letter="Dear Hiring Team at Acme Health,\nThanks.\nMaya Lin | [EMAIL]",
        ats_score_before={"overall_score": 70.0},
        ats_score_after={"overall_score": 88.0},
    )
    return ctx


def test_resume_load_keeps_real_contact_out_of_model_output(ready_ctx):
    result = session_tools.load_candidate_resume(MAYA, ready_ctx)
    assert "maya.lin.dev@email.com" not in json.dumps(result)
    assert ready_ctx.state["candidate_contact"]["email"] == "maya.lin.dev@email.com"


def test_submit_requires_prepared_package(ready_ctx):
    result = session_tools.submit_job_application("greenhouse:acme:42", ready_ctx)
    assert result["status"] == "error"
    assert "prepare_application_package" in result["recovery_instruction"]


def test_full_apply_flow_with_approval(ats, ready_ctx):
    pkg = session_tools.prepare_application_package(ready_ctx)
    assert pkg["status"] == "success" and pkg["job_id"] == "greenhouse:acme:42"
    assert Path(pkg["resume_file"]).is_file()
    assert "maya.lin.dev@email.com" not in json.dumps(pkg)  # PII not sent to model

    # 1st call: pauses for approval, sends nothing.
    pending = session_tools.submit_job_application(pkg["job_id"], ready_ctx)
    assert pending["status"] == "pending_approval"
    assert "Acme Health" in ready_ctx.requested["hint"]
    assert "application_history" not in ready_ctx.state

    # Candidate approves -> ADK calls the tool again with the confirmation.
    ready_ctx.tool_confirmation = SimpleNamespace(confirmed=True)
    done = session_tools.submit_job_application(pkg["job_id"], ready_ctx)
    assert done["status"] == "success" and done["application_id"].startswith("app_")
    assert ready_ctx.state["application_decisions"][-1]["approved"] is True

    stored = ats.get(f"/applications/{done['application_id']}").json()
    assert stored["candidate"]["email"] == "maya.lin.dev@email.com"

    # Asking again is blocked locally before reaching the ATS.
    again = session_tools.submit_job_application(pkg["job_id"], ready_ctx)
    assert again["error_code"] == ToolErrorCode.DUPLICATE_APPLICATION


def test_rejection_sends_nothing(ats, ready_ctx):
    pkg = session_tools.prepare_application_package(ready_ctx)
    ready_ctx.tool_confirmation = SimpleNamespace(confirmed=False)
    result = session_tools.submit_job_application(pkg["job_id"], ready_ctx)
    assert result["status"] == "cancelled"
    assert not ready_ctx.state.get("application_history")
    assert ready_ctx.state["application_decisions"][-1]["approved"] is False
    assert (
        ats.get("/applications", params={"email": "maya.lin.dev@email.com"}).json()
        == []
    )


def test_ats_duplicate_409_is_surfaced_as_guided_error(ats, ready_ctx):
    """Simulates applying again from a new session: the ATS itself rejects it."""
    pkg = session_tools.prepare_application_package(ready_ctx)
    ready_ctx.tool_confirmation = SimpleNamespace(confirmed=True)
    session_tools.submit_job_application(pkg["job_id"], ready_ctx)
    ready_ctx.state["application_history"] = []  # new session forgets local history
    result = session_tools.submit_job_application(pkg["job_id"], ready_ctx)
    assert result["error_code"] == ToolErrorCode.DUPLICATE_APPLICATION
    assert result["details"]["existing_application_id"].startswith("app_")


def test_job_id_mismatch_is_rejected(ats, ready_ctx):
    session_tools.prepare_application_package(ready_ctx)
    result = session_tools.submit_job_application("lever:other:1", ready_ctx)
    assert result["error_code"] == ToolErrorCode.INVALID_INPUT
