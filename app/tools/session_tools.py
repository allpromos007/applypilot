"""Session-aware tools: thin wrappers that connect the pure tools to ADK state.

The pure tools in this package are stateless and unit-tested in isolation.
These wrappers add `tool_context` (injected by ADK, invisible to the model) so
agents can hand large artefacts to each other through session state instead of
copying resumes and job descriptions through every prompt.

State keys written here (single source of truth for the agent graph):
    resume_text, resume_file, resume_missing_sections   <- load_candidate_resume
    candidate_preferences                               <- save_candidate_preferences
    job_shortlist                                       <- find_jobs_matching_saved_preferences
    target_job_id, target_job_title, target_company,
    target_job_url, target_job_description              <- select_target_job
    ats_score_before, ats_score_after                   <- score_resume_for_target_job
    tailoring_plan, tailored_resume, cover_letter       <- written by agents (output_key)
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext
from pydantic import Field, ValidationError

from app.tools.ats_tools import calculate_ats_match_score, extract_job_requirements
from app.tools.errors import ToolErrorCode, tool_error, validation_error
from app.tools.job_search_tools import (
    get_job_posting_details,
    search_matching_job_postings,
)
from app.tools.resume_tools import parse_resume_document
from app.tools.schemas import StrictModel, WorkplaceType


class CandidatePreferences(StrictModel):
    """Job-search preferences captured from the candidate."""

    target_titles: list[str] = Field(min_length=1, max_length=10)
    locations: list[str] = Field(default_factory=list, max_length=10)
    workplace_type: WorkplaceType = WorkplaceType.ANY
    required_keywords: list[str] = Field(default_factory=list, max_length=20)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=20)
    min_salary_usd: int | None = Field(default=None, ge=0, le=2_000_000)
    greenhouse_boards: list[str] = Field(default_factory=list)
    lever_companies: list[str] = Field(default_factory=list)


def _missing_state(key: str, fix: str) -> dict[str, Any]:
    return tool_error(
        ToolErrorCode.INVALID_INPUT,
        f"Required session data '{key}' is not available yet.",
        fix,
    )


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
def load_candidate_resume(file_path: str, tool_context: ToolContext) -> dict[str, Any]:
    """Load the candidate's resume file into this session for all later steps.

    Parses the file, masks contact details, and stores the resume text in
    session state so other agents can score and tailor it. Call this once per
    resume; calling again replaces the stored resume.

    Args:
        file_path: Path to a .pdf, .docx, .txt or .md resume, e.g.
            "samples/resumes/maya_lin_healthcare_full_stack.pdf".

    Returns:
        The parse result: {"status": "success", "file_name", "word_count",
        "sections", "missing_standard_sections", "contact_fields_detected",
        "full_text"} or a guided error with "recovery_instruction".
    """
    result = parse_resume_document(file_path)
    if result.get("status") == "success":
        tool_context.state["resume_text"] = result["full_text"]
        tool_context.state["resume_file"] = result["file_name"]
        tool_context.state["resume_missing_sections"] = result[
            "missing_standard_sections"
        ]
    return result


def save_candidate_preferences(
    target_titles: list[str],
    tool_context: ToolContext,
    locations: list[str] | None = None,
    workplace_type: str = "any",
    required_keywords: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
    min_salary_usd: int | None = None,
    greenhouse_boards: list[str] | None = None,
    lever_companies: list[str] | None = None,
) -> dict[str, Any]:
    """Save the candidate's job-search preferences for this session.

    Only record what the candidate actually said; leave anything unstated empty.

    Args:
        target_titles: Desired roles, e.g. ["full stack engineer", "software engineer"].
        locations: Acceptable locations, e.g. ["seattle", "remote"]. Empty = any.
        workplace_type: "remote", "hybrid", "onsite" or "any".
        required_keywords: Skills the job should mention, e.g. ["react", "fhir"].
        excluded_keywords: Deal-breakers, e.g. ["crypto", "defense"].
        min_salary_usd: Minimum acceptable annual base salary in USD, if stated.
        greenhouse_boards: Specific Greenhouse companies to search, if named.
        lever_companies: Specific Lever companies to search, if named.

    Returns:
        {"status": "success", "preferences": {...}} or a guided error.
    """
    try:
        prefs = CandidatePreferences(
            target_titles=target_titles,
            locations=locations or [],
            workplace_type=workplace_type,
            required_keywords=required_keywords or [],
            excluded_keywords=excluded_keywords or [],
            min_salary_usd=min_salary_usd,
            greenhouse_boards=greenhouse_boards or [],
            lever_companies=lever_companies or [],
        )
    except ValidationError as exc:
        return validation_error(exc, "save_candidate_preferences")
    data = prefs.model_dump(mode="json")
    tool_context.state["candidate_preferences"] = data
    return {"status": "success", "preferences": data}


# --------------------------------------------------------------------------- #
# Job matching
# --------------------------------------------------------------------------- #
def find_jobs_matching_saved_preferences(
    tool_context: ToolContext, max_results: int = 10
) -> dict[str, Any]:
    """Search Greenhouse and Lever using the preferences saved for this session.

    Requires save_candidate_preferences to have been called first. Stores the
    ranked shortlist in session state.

    Args:
        max_results: Maximum number of jobs to return (1-50).

    Returns:
        {"status": "success", "total_scanned", "returned", "jobs": [...],
        "boards_failed"} or a guided error with "recovery_instruction".
    """
    prefs = tool_context.state.get("candidate_preferences")
    if not prefs:
        return _missing_state(
            "candidate_preferences",
            "Ask the candidate for target roles and location, call "
            "save_candidate_preferences, then call this tool again.",
        )
    result = search_matching_job_postings(
        target_titles=prefs["target_titles"],
        locations=prefs["locations"],
        workplace_type=prefs["workplace_type"],
        required_keywords=prefs["required_keywords"],
        excluded_keywords=prefs["excluded_keywords"],
        greenhouse_boards=prefs["greenhouse_boards"],
        lever_companies=prefs["lever_companies"],
        max_results=max_results,
    )
    if result.get("status") == "success":
        tool_context.state["job_shortlist"] = result["jobs"]
    return result


def select_target_job(job_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Choose the job to tailor the resume and cover letter for.

    Fetches the full job description and stores it in session state so the
    tailoring pipeline can use it.

    Args:
        job_id: An id from the shortlist, e.g. "greenhouse:airbnb:8224032".

    Returns:
        {"status": "success", "job_id", "company", "title", "location", "url",
        "description_chars"} or a guided error with "recovery_instruction".
    """
    details = get_job_posting_details(job_id)
    if details.get("status") != "success":
        return details
    state = tool_context.state
    state["target_job_id"] = details["job_id"]
    state["target_job_title"] = details["title"]
    state["target_company"] = details["company"]
    state["target_job_url"] = details["url"]
    state["target_job_description"] = details["description_text"]
    # A new target invalidates documents tailored for the previous one.
    for stale in (
        "tailoring_plan",
        "tailored_resume",
        "cover_letter",
        "ats_score_after",
    ):
        state[stale] = ""
    return {
        "status": "success",
        "job_id": details["job_id"],
        "company": details["company"],
        "title": details["title"],
        "location": details["location"],
        "url": details["url"],
        "description_chars": len(details["description_text"]),
    }


def use_pasted_job_description(
    job_title: str, company: str, job_description: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Use a job description the candidate pasted instead of one from search.

    Args:
        job_title: Title of the role, e.g. "Senior Full Stack Engineer".
        company: Hiring company name.
        job_description: Full job description text (at least 200 characters).

    Returns:
        {"status": "success", ...} or a guided error.
    """
    if len(job_description.strip()) < 200:
        return tool_error(
            ToolErrorCode.INVALID_INPUT,
            "The pasted job description is too short to tailor against.",
            "Ask the candidate to paste the complete job description, including "
            "responsibilities and requirements, then call this tool again.",
        )
    state = tool_context.state
    state["target_job_id"] = "pasted"
    state["target_job_title"] = job_title.strip()
    state["target_company"] = company.strip()
    state["target_job_url"] = ""
    state["target_job_description"] = job_description.strip()
    for stale in (
        "tailoring_plan",
        "tailored_resume",
        "cover_letter",
        "ats_score_after",
    ):
        state[stale] = ""
    return {"status": "success", "title": job_title, "company": company}


# --------------------------------------------------------------------------- #
# Tailoring support
# --------------------------------------------------------------------------- #
def extract_requirements_for_target_job(tool_context: ToolContext) -> dict[str, Any]:
    """Extract required/preferred skills and keywords from the selected job.

    Returns:
        {"status": "success", "required_skills", "preferred_skills",
        "top_keywords", "seniority", "min_years_experience"} or a guided error.
    """
    jd = tool_context.state.get("target_job_description")
    if not jd:
        return _missing_state(
            "target_job_description",
            "Select a job first with select_target_job (or use_pasted_job_description).",
        )
    return extract_job_requirements(jd, tool_context.state.get("target_job_title", ""))


def score_resume_for_target_job(
    version: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Score the original or tailored resume against the selected job (0-100).

    Uses the same deterministic ATS scorer for both versions so the comparison
    is fair. Scoring "after" also reports the change versus "before".

    Args:
        version: "before" to score the original resume, or "after" to score
            the tailored resume produced by the rewriter.

    Returns:
        {"status": "success", "label", "overall_score", "components",
        "matched_keywords", "missing_keywords", "recommendations"} plus
        "score_change" when version is "after"; or a guided error.
    """
    if version not in ("before", "after"):
        return tool_error(
            ToolErrorCode.INVALID_INPUT,
            f"version must be 'before' or 'after', got '{version}'.",
            "Call again with version='before' for the original resume or "
            "version='after' for the tailored one.",
        )
    state = tool_context.state
    jd = state.get("target_job_description")
    if not jd:
        return _missing_state("target_job_description", "Select a target job first.")
    resume_key = "resume_text" if version == "before" else "tailored_resume"
    resume = state.get(resume_key)
    if not resume:
        fix = (
            "Load the resume with load_candidate_resume first."
            if version == "before"
            else "The tailored resume has not been written yet; run the rewriter first."
        )
        return _missing_state(resume_key, fix)

    result = calculate_ats_match_score(
        resume, jd, state.get("target_job_title", ""), version
    )
    if result.get("status") == "success":
        state[f"ats_score_{version}"] = result
        before = state.get("ats_score_before")
        if version == "after" and isinstance(before, dict):
            result["score_change"] = round(
                result["overall_score"] - before["overall_score"], 1
            )
            result["before_overall_score"] = before["overall_score"]
    return result


def get_tailored_application_documents(tool_context: ToolContext) -> dict[str, Any]:
    """Return the tailored resume, cover letter and before/after scores to show the candidate.

    Returns:
        {"status": "success", "target", "tailored_resume", "cover_letter",
        "score_before", "score_after"} or a guided error if not generated yet.
    """
    state = tool_context.state
    if not state.get("tailored_resume"):
        return _missing_state(
            "tailored_resume",
            "Run the resume_tailoring_pipeline for the selected job first.",
        )
    before, after = (
        state.get("ats_score_before") or {},
        state.get("ats_score_after") or {},
    )
    return {
        "status": "success",
        "target": f"{state.get('target_job_title')} at {state.get('target_company')}",
        "tailored_resume": state.get("tailored_resume"),
        "cover_letter": state.get("cover_letter", ""),
        "score_before": before.get("overall_score"),
        "score_after": after.get("overall_score"),
    }
