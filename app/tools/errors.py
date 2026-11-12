"""Guided error handling for tools (rubric 1.4).

Tools never raise exceptions back into the agent loop. Instead they return a
structured error that tells the LLM *what went wrong* and *exactly how to
recover*, so the agent can self-correct instead of crashing or hallucinating.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class ToolErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    JOB_BOARD_NOT_FOUND = "JOB_BOARD_NOT_FOUND"
    JOB_BOARD_UNAVAILABLE = "JOB_BOARD_UNAVAILABLE"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    NO_JOBS_MATCHED = "NO_JOBS_MATCHED"
    NO_COMPANIES_CONFIGURED = "NO_COMPANIES_CONFIGURED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolError(BaseModel):
    """Error payload returned to the LLM instead of raising."""

    status: str = Field(default="error", description="Always 'error' for failures.")
    error_code: ToolErrorCode = Field(description="Machine-readable error category.")
    message: str = Field(description="What went wrong, in plain language.")
    recovery_instruction: str = Field(
        description="Concrete next step the agent should take to recover."
    )
    details: dict[str, Any] = Field(default_factory=dict)


def tool_error(
    code: ToolErrorCode,
    message: str,
    recovery_instruction: str,
    **details: Any,
) -> dict[str, Any]:
    """Build a guided error dict that ADK will hand back to the model."""
    return ToolError(
        error_code=code,
        message=message,
        recovery_instruction=recovery_instruction,
        details=details,
    ).model_dump(mode="json")


def validation_error(exc: ValidationError, tool_name: str) -> dict[str, Any]:
    """Translate a Pydantic validation failure into actionable guidance."""
    problems = [
        f"'{'.'.join(str(p) for p in err['loc'])}': {err['msg']}"
        for err in exc.errors()
    ]
    return tool_error(
        ToolErrorCode.INVALID_INPUT,
        message=f"{tool_name} received invalid arguments: " + "; ".join(problems),
        recovery_instruction=(
            f"Fix the listed fields and call {tool_name} again. Do not guess values the "
            "candidate has not provided; ask the candidate instead."
        ),
        problems=problems,
    )
