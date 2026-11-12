"""Strict input/output schemas for every ApplyPilot tool (rubric 1.3).

Each tool validates its arguments against an `*Input` model before doing any
work and returns a JSON-serialised `*Output` model, so both what the LLM sends
and what it receives back are constrained and self-describing.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# Resume parsing
# --------------------------------------------------------------------------- #
class ParseResumeInput(StrictModel):
    file_path: str = Field(
        min_length=1,
        description="Absolute or project-relative path to a .pdf, .docx or .txt resume.",
    )


class ResumeSection(StrictModel):
    heading: str = Field(description="Normalised section name, e.g. 'experience'.")
    content: str = Field(description="Raw text of the section.")


class ParsedResume(StrictModel):
    status: str = "success"
    file_name: str
    file_type: str = Field(description="pdf | docx | txt")
    word_count: int = Field(ge=0)
    sections: list[ResumeSection]
    missing_standard_sections: list[str] = Field(
        description="Standard ATS sections not found (e.g. 'skills')."
    )
    contact_fields_detected: list[str] = Field(
        description="Which contact fields exist (email, phone, linkedin). Values are "
        "intentionally NOT returned to keep PII out of model context and logs."
    )
    full_text: str = Field(description="Resume text with contact details masked.")


# --------------------------------------------------------------------------- #
# Job search
# --------------------------------------------------------------------------- #
class JobSource(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"


class WorkplaceType(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    ANY = "any"


class JobSearchInput(StrictModel):
    target_titles: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Job titles or title keywords the candidate wants, e.g. "
        "['data engineer', 'analytics engineer'].",
    )
    locations: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Acceptable locations (city, state or country). Empty = any.",
    )
    workplace_type: WorkplaceType = Field(
        default=WorkplaceType.ANY, description="remote | hybrid | onsite | any"
    )
    required_keywords: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Skills that should appear in the job description, e.g. ['python'].",
    )
    excluded_keywords: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Terms that disqualify a job, e.g. ['crypto', 'unpaid'].",
    )
    greenhouse_boards: list[str] = Field(
        default_factory=list,
        description="Greenhouse board tokens to search (the <name> in "
        "boards.greenhouse.io/<name>). Empty = use configured defaults.",
    )
    lever_companies: list[str] = Field(
        default_factory=list,
        description="Lever company slugs (the <name> in jobs.lever.co/<name>). "
        "Empty = use configured defaults.",
    )
    max_results: int = Field(default=10, ge=1, le=50)

    @field_validator(
        "target_titles", "required_keywords", "excluded_keywords", "locations"
    )
    @classmethod
    def _lowercase(cls, values: list[str]) -> list[str]:
        return [v.lower().strip() for v in values if v.strip()]


class JobPostingSummary(StrictModel):
    job_id: str = Field(description="Stable id in the form '<source>:<company>:<id>'.")
    source: JobSource
    company: str
    title: str
    location: str
    workplace_type: WorkplaceType
    url: HttpUrl
    match_score: float = Field(ge=0, le=100, description="Preference fit, 0-100.")
    match_reasons: list[str]


class JobSearchOutput(StrictModel):
    status: str = "success"
    total_scanned: int = Field(ge=0)
    returned: int = Field(ge=0)
    jobs: list[JobPostingSummary]
    boards_failed: list[str] = Field(
        default_factory=list, description="Boards that could not be reached."
    )


class JobDetailsInput(StrictModel):
    job_id: str = Field(
        pattern=r"^(greenhouse|lever):[A-Za-z0-9_.-]+:[A-Za-z0-9_-]+$",
        description="job_id exactly as returned by search_matching_job_postings.",
    )


class JobPostingDetails(StrictModel):
    status: str = "success"
    job_id: str
    company: str
    title: str
    location: str
    url: HttpUrl
    description_text: str = Field(description="Job description as plain text.")


# --------------------------------------------------------------------------- #
# Job requirements + ATS scoring
# --------------------------------------------------------------------------- #
class ExtractRequirementsInput(StrictModel):
    job_description: str = Field(min_length=50, description="Full JD plain text.")
    job_title: str = Field(default="", description="Posting title, if known.")


class JobRequirements(StrictModel):
    status: str = "success"
    job_title: str
    seniority: str = Field(
        description="intern | junior | mid | senior | staff+ | unknown"
    )
    required_skills: list[str]
    preferred_skills: list[str]
    top_keywords: list[str] = Field(description="Most frequent meaningful JD terms.")
    min_years_experience: int | None = Field(default=None, ge=0, le=40)


class AtsScoreInput(StrictModel):
    resume_text: str = Field(min_length=100, description="Plain-text resume to score.")
    job_description: str = Field(min_length=50, description="Target JD plain text.")
    job_title: str = Field(default="", description="Target job title.")
    label: str = Field(
        default="before",
        pattern=r"^(before|after|other)$",
        description="'before' for the original resume, 'after' for the tailored one.",
    )


class ScoreComponent(StrictModel):
    name: str
    score: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    notes: list[str]


class AtsScoreOutput(StrictModel):
    status: str = "success"
    label: str
    overall_score: float = Field(ge=0, le=100)
    components: list[ScoreComponent]
    matched_keywords: list[str]
    missing_keywords: list[str]
    recommendations: list[str]
