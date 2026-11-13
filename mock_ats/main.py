"""Mock Applicant Tracking System (ATS).

A small, realistic stand-in for an employer's application endpoint so the
agent's `submit_job_application` tool has something real to call, including
real failure modes the agent must recover from:

    201  application accepted             -> {application_id, status: RECEIVED}
    409  same candidate already applied   -> DUPLICATE_APPLICATION
    422  missing/invalid fields or files  -> VALIDATION_FAILED

Run locally:  uv run uvicorn mock_ats.main:app --port 8081
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from mock_ats.models import (
    PLACEHOLDERS,
    ApplicationReceipt,
    ApplicationRecord,
    CandidateInfo,
    ErrorBody,
)
from mock_ats.store import (
    ApplicationStore,
    DuplicateApplicationError,
    SQLiteApplicationStore,
)

MAX_FILE_BYTES = 5 * 1024 * 1024
DOCX_MAGIC = b"PK\x03\x04"  # .docx files are zip archives


def _error(status: int, code: str, message: str, **details) -> JSONResponse:
    body = ErrorBody(error_code=code, message=message, details=details)
    return JSONResponse(status_code=status, content=body.model_dump())


def create_app(store: ApplicationStore | None = None) -> FastAPI:
    app = FastAPI(title="Mock ATS", version="1.0.0")
    app.state.store = store or SQLiteApplicationStore(
        Path(os.getenv("MOCK_ATS_DATA_DIR", ".mock_ats_data"))
    )

    @app.exception_handler(RequestValidationError)
    async def _on_validation(_: Request, exc: RequestValidationError):
        problems = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        ]
        return _error(
            422, "VALIDATION_FAILED", "Invalid application.", problems=problems
        )

    @app.exception_handler(HTTPException)
    async def _on_http(_: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
        return _error(
            exc.status_code,
            detail.get("error_code", "HTTP_ERROR"),
            detail.get("message", ""),
            **detail.get("details", {}),
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    async def _read_docx(upload: UploadFile, field: str) -> tuple[str, bytes]:
        data = await upload.read()
        name = upload.filename or f"{field}.docx"
        problems = []
        if not name.lower().endswith(".docx"):
            problems.append(f"{field}: must be a .docx file, got '{name}'")
        if not data:
            problems.append(f"{field}: file is empty")
        elif len(data) > MAX_FILE_BYTES:
            problems.append(f"{field}: larger than 5 MB")
        elif not data.startswith(DOCX_MAGIC):
            problems.append(f"{field}: not a valid Word (.docx) document")
        else:
            import docx

            text = "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
            leftover = [p for p in PLACEHOLDERS if p in text]
            if leftover:
                problems.append(f"{field}: unresolved placeholders {leftover}")
        if problems:
            raise HTTPException(
                422,
                {
                    "error_code": "VALIDATION_FAILED",
                    "message": "Invalid application documents.",
                    "details": {"problems": problems},
                },
            )
        return name, data

    @app.post("/applications", status_code=201, response_model=ApplicationReceipt)
    async def create_application(
        job_id: Annotated[str, Form(min_length=3, max_length=200)],
        candidate: Annotated[str, Form(description="CandidateInfo as JSON")],
        resume: Annotated[UploadFile, File()],
        cover_letter: Annotated[UploadFile, File()],
    ):
        try:
            info = CandidateInfo.model_validate(json.loads(candidate))
        except (json.JSONDecodeError, ValidationError) as exc:
            problems = (
                [
                    f"candidate.{'.'.join(map(str, e['loc']))}: {e['msg']}"
                    for e in exc.errors()
                ]
                if isinstance(exc, ValidationError)
                else ["candidate: must be valid JSON"]
            )
            raise HTTPException(
                422,
                {
                    "error_code": "VALIDATION_FAILED",
                    "message": "Invalid candidate details.",
                    "details": {"problems": problems},
                },
            ) from exc
        resume_file = await _read_docx(resume, "resume")
        letter_file = await _read_docx(cover_letter, "cover_letter")
        try:
            record = app.state.store.create(job_id, info, resume_file, letter_file)
        except DuplicateApplicationError as dup:
            raise HTTPException(
                409,
                {
                    "error_code": "DUPLICATE_APPLICATION",
                    "message": "This candidate has already applied to this job.",
                    "details": {"existing_application_id": dup.existing_id},
                },
            ) from dup
        return ApplicationReceipt(
            **record.model_dump(include=set(ApplicationReceipt.model_fields))
        )

    @app.get("/applications/{application_id}", response_model=ApplicationRecord)
    def get_application(application_id: str):
        record = app.state.store.get(application_id)
        if not record:
            raise HTTPException(
                404, {"error_code": "NOT_FOUND", "message": "No such application."}
            )
        return record

    @app.get("/applications", response_model=list[ApplicationRecord])
    def list_applications(email: str):
        return app.state.store.list_for_email(email)

    return app


app = create_app()
