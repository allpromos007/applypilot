"""Request/response schemas for the Mock ATS API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLACEHOLDERS = ("[EMAIL]", "[PHONE]", "[LINKEDIN]", "[GITHUB]")


class ApplicationStatus(StrEnum):
    RECEIVED = "RECEIVED"


class CandidateInfo(BaseModel):
    """Applicant details sent alongside the documents."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(min_length=2, max_length=120)
    email: str = Field(
        pattern=r"^[^@\s\[\]]+@[^@\s\[\]]+\.[A-Za-z]{2,}$", max_length=200
    )
    phone: str | None = Field(default=None, max_length=40)
    linkedin_url: str | None = Field(default=None, max_length=200)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.lower()

    @field_validator("full_name", "phone", "linkedin_url")
    @classmethod
    def no_unresolved_placeholders(cls, value: str | None) -> str | None:
        if value and any(p in value for p in PLACEHOLDERS):
            raise ValueError(
                "contains an unresolved placeholder such as [PHONE]; send real values"
            )
        return value


class ApplicationReceipt(BaseModel):
    """Returned when an application is accepted."""

    application_id: str
    status: ApplicationStatus
    job_id: str
    received_at: datetime


class ApplicationRecord(ApplicationReceipt):
    """Full stored application (returned by GET)."""

    candidate: CandidateInfo
    resume_filename: str
    cover_letter_filename: str


class ErrorBody(BaseModel):
    """Uniform error payload so clients can react programmatically."""

    error_code: str
    message: str
    details: dict = Field(default_factory=dict)
