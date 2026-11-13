"""Client for the employer's Applicant Tracking System (our Mock ATS).

`ApplicationAdapter` is the seam for real ATS integrations later; today the
only implementation is `MockATSAdapter`. Every HTTP outcome is translated into
either a success dict or a guided error (rubric 1.4) so the agent can recover:
409 -> don't retry, tell the candidate; 422 -> fix the package; 5xx/timeout ->
retry later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import get_settings
from app.observability.tracing import mark_outcome, trace_headers, traced
from app.secrets import get_secret
from app.tools.errors import ToolErrorCode, tool_error

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ApplicationAdapter(Protocol):
    def submit(
        self,
        job_id: str,
        candidate: dict[str, Any],
        resume_path: Path,
        cover_letter_path: Path,
    ) -> dict[str, Any]: ...


def _id_token_headers(audience: str) -> dict[str, str]:
    """Cloud Run services are IAM-protected: send a Google-signed ID token.

    Locally (http://localhost) no token is needed or sent.
    """
    if not audience.startswith("https://"):
        return {}
    import google.auth.transport.requests
    from google.oauth2 import id_token

    token = id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)
    return {"Authorization": f"Bearer {token}"}


class MockATSAdapter:
    def __init__(
        self, base_url: str | None = None, transport: httpx.BaseTransport | None = None
    ):
        settings = get_settings().applications
        self.base_url = (base_url or settings.mock_ats_url).rstrip("/")
        self.timeout = settings.http_timeout_seconds
        self._transport = transport  # injected in tests

    def submit(
        self, job_id, candidate, resume_path, cover_letter_path
    ) -> dict[str, Any]:
        with traced(
            "mock_ats.submit_application", job_id=job_id, ats_url=self.base_url
        ) as span:
            result = self._submit(job_id, candidate, resume_path, cover_letter_path)
            mark_outcome(span, result)
            if result.get("application_id"):
                span.set_attribute(
                    "applypilot.application_id", result["application_id"]
                )
            return result

    def _submit(
        self, job_id, candidate, resume_path, cover_letter_path
    ) -> dict[str, Any]:
        try:
            # traceparent header -> the Mock ATS joins this same trace.
            headers = {**_id_token_headers(self.base_url), **trace_headers()}
            # Application-level key from Secret Manager (defence in depth on top
            # of Cloud Run IAM; the only auth when running locally).
            if api_key := get_secret("mock-ats-api-key"):
                headers["X-API-Key"] = api_key
            with (
                httpx.Client(timeout=self.timeout, transport=self._transport) as client,
                open(resume_path, "rb") as resume,
                open(cover_letter_path, "rb") as letter,
            ):
                response = client.post(
                    f"{self.base_url}/applications",
                    headers=headers,
                    data={"job_id": job_id, "candidate": json.dumps(candidate)},
                    files={
                        "resume": (resume_path.name, resume, DOCX_MIME),
                        "cover_letter": (cover_letter_path.name, letter, DOCX_MIME),
                    },
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return tool_error(
                ToolErrorCode.ATS_UNAVAILABLE,
                f"The application system at {self.base_url} is not reachable "
                f"({type(exc).__name__}).",
                "Nothing was submitted. Tell the candidate the employer's system is "
                "unavailable and offer to try again in a few minutes. Do not claim "
                "the application was sent.",
            )
        except Exception as exc:  # auth problems, unreadable files, ...
            return tool_error(
                ToolErrorCode.INTERNAL_ERROR,
                f"Could not submit the application: {type(exc).__name__}.",
                "Nothing was submitted. Explain this to the candidate; do not retry "
                "more than once.",
            )

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 201:
            return {
                "status": "success",
                "application_id": body.get("application_id"),
                "ats_status": body.get("status"),
                "job_id": body.get("job_id", job_id),
                "received_at": body.get("received_at"),
            }
        if response.status_code in (401, 403):
            return tool_error(
                ToolErrorCode.ATS_UNAVAILABLE,
                "The application system rejected ApplyPilot's credentials "
                f"(HTTP {response.status_code}).",
                "Nothing was submitted. This is a configuration problem, not the "
                "candidate's fault: tell them applications are temporarily unavailable. "
                "Do not retry.",
            )
        if response.status_code == 409:
            return tool_error(
                ToolErrorCode.DUPLICATE_APPLICATION,
                "The candidate has already applied to this job.",
                "Do NOT retry. Tell the candidate they already applied and share the "
                "existing application id; offer to tailor for a different job.",
                existing_application_id=body.get("details", {}).get(
                    "existing_application_id"
                ),
            )
        if response.status_code == 422:
            return tool_error(
                ToolErrorCode.APPLICATION_REJECTED,
                "The employer's system rejected the application as incomplete or invalid.",
                "Read 'problems'. If documents are the issue, call "
                "prepare_application_package again; if contact details are missing, "
                "ask the candidate to add them to their resume file and reload it. "
                "Then ask for approval again before resubmitting.",
                problems=body.get("details", {}).get("problems", []),
            )
        return tool_error(
            ToolErrorCode.ATS_UNAVAILABLE,
            f"The application system returned HTTP {response.status_code}.",
            "Nothing was confirmed as submitted. Tell the candidate and offer to try "
            "again later.",
        )
