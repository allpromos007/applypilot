"""Unit tests for session-aware tool wrappers (no network, no LLM)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tools import session_tools
from app.tools.errors import ToolErrorCode

ROOT = Path(__file__).resolve().parents[2]
MAYA = "samples/resumes/maya_lin_healthcare_full_stack.pdf"
JD = (ROOT / "samples/job_descriptions/senior_full_stack_healthcare.txt").read_text()


@pytest.fixture
def ctx():
    """Minimal stand-in for ADK's ToolContext: tools only use `.state`."""
    return SimpleNamespace(state={})


def test_load_resume_populates_state(ctx):
    result = session_tools.load_candidate_resume(MAYA, ctx)
    assert result["status"] == "success"
    assert ctx.state["resume_file"] == "maya_lin_healthcare_full_stack.pdf"
    assert "[EMAIL]" in ctx.state["resume_text"]


def test_load_resume_error_does_not_touch_state(ctx):
    result = session_tools.load_candidate_resume("missing.pdf", ctx)
    assert result["status"] == "error"
    assert ctx.state == {}


def test_save_preferences_validates(ctx):
    ok = session_tools.save_candidate_preferences(
        ["full stack engineer"], ctx, workplace_type="remote", min_salary_usd=150000
    )
    assert ok["status"] == "success"
    assert ctx.state["candidate_preferences"]["workplace_type"] == "remote"
    bad = session_tools.save_candidate_preferences([], ctx)
    assert bad["error_code"] == ToolErrorCode.INVALID_INPUT


def test_find_jobs_requires_preferences(ctx):
    result = session_tools.find_jobs_matching_saved_preferences(ctx)
    assert result["status"] == "error"
    assert "save_candidate_preferences" in result["recovery_instruction"]


def test_pasted_jd_rejects_short_text(ctx):
    result = session_tools.use_pasted_job_description("SWE", "Acme", "too short", ctx)
    assert result["status"] == "error"


def test_before_after_scoring_flow(ctx):
    session_tools.load_candidate_resume(MAYA, ctx)
    # Scoring before a job is selected gives guidance, not a crash.
    assert session_tools.score_resume_for_target_job("before", ctx)["status"] == "error"

    session_tools.use_pasted_job_description(
        "Senior Full Stack Engineer", "CareCo", JD, ctx
    )
    before = session_tools.score_resume_for_target_job("before", ctx)
    assert before["status"] == "success"
    assert ctx.state["ats_score_before"]["overall_score"] == before["overall_score"]

    # "after" needs a tailored resume first.
    missing = session_tools.score_resume_for_target_job("after", ctx)
    assert "rewriter" in missing["recovery_instruction"]

    ctx.state["tailored_resume"] = ctx.state["resume_text"] + (
        "\nSKILLS\nGoogle Cloud (GCP), Kubernetes, WebSockets, WebRTC, SOC 2\n"
    )
    after = session_tools.score_resume_for_target_job("after", ctx)
    assert after["status"] == "success"
    assert after["before_overall_score"] == before["overall_score"]
    assert after["score_change"] == pytest.approx(
        after["overall_score"] - before["overall_score"], abs=0.11
    )

    docs = session_tools.get_tailored_application_documents(ctx)
    assert docs["score_before"] == before["overall_score"]
    assert docs["target"] == "Senior Full Stack Engineer at CareCo"


def test_invalid_version(ctx):
    result = session_tools.score_resume_for_target_job("final", ctx)
    assert result["error_code"] == ToolErrorCode.INVALID_INPUT


def test_new_target_clears_stale_documents(ctx):
    ctx.state.update(tailored_resume="old", cover_letter="old", tailoring_plan="old")
    session_tools.use_pasted_job_description("SWE", "NewCo", JD, ctx)
    assert ctx.state["tailored_resume"] == ""
    assert ctx.state["cover_letter"] == ""
