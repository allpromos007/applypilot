"""Resume ingestion tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import PROJECT_ROOT
from app.tools.errors import ToolErrorCode, tool_error, validation_error
from app.tools.schemas import ParsedResume, ParseResumeInput, ResumeSection
from app.tools.text_analysis import (
    REQUIRED_SECTIONS,
    clean_extracted_text,
    detect_sections,
    mask_contact_details,
)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _read_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    lines = [p.text for p in document.paragraphs]
    # Tables are an ATS anti-pattern but people still use them; read them anyway.
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(lines)


def _resolve(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def parse_resume_document(file_path: str) -> dict[str, Any]:
    """Read a candidate's resume file and split it into standard ATS sections.

    Use this FIRST whenever the candidate provides a resume file, before scoring
    or tailoring. Contact details (email, phone, LinkedIn) are masked in the
    returned text so they never enter the conversation; only which fields exist
    is reported.

    Args:
        file_path: Path to the resume. Supported types: .pdf, .docx, .txt, .md.
            Relative paths are resolved from the project root (e.g.
            'samples/resumes/jane_doe.pdf').

    Returns:
        On success: {"status": "success", "file_name", "file_type", "word_count",
        "sections": [{"heading", "content"}], "missing_standard_sections",
        "contact_fields_detected", "full_text"}.
        On failure: {"status": "error", "error_code", "message",
        "recovery_instruction"}.
    """
    try:
        args = ParseResumeInput(file_path=file_path)
    except ValidationError as exc:
        return validation_error(exc, "parse_resume_document")

    path = _resolve(args.file_path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return tool_error(
            ToolErrorCode.UNSUPPORTED_FILE_TYPE,
            f"'{path.name}' has unsupported type '{suffix or 'none'}'.",
            "Ask the candidate to upload the resume as PDF or DOCX (or paste it as "
            "text), then call parse_resume_document again with that file.",
            supported=sorted(SUPPORTED_EXTENSIONS),
        )
    if not path.is_file():
        return tool_error(
            ToolErrorCode.FILE_NOT_FOUND,
            f"No file found at '{args.file_path}'.",
            "Confirm the exact file path with the candidate (check spelling and "
            "folder), then retry. Do not invent a path.",
        )

    try:
        if suffix == ".pdf":
            raw = _read_pdf(path)
        elif suffix == ".docx":
            raw = _read_docx(path)
        else:
            raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # corrupt / encrypted files
        return tool_error(
            ToolErrorCode.INTERNAL_ERROR,
            f"Could not read '{path.name}': {type(exc).__name__}.",
            "The file may be encrypted, scanned or corrupt. Ask the candidate for an "
            "unlocked, text-based PDF/DOCX, or to paste the resume text directly.",
        )

    if len(raw.split()) < 50:
        return tool_error(
            ToolErrorCode.EMPTY_DOCUMENT,
            f"'{path.name}' contains almost no extractable text ({len(raw.split())} words).",
            "This is likely a scanned/image-only resume, which ATS systems also cannot "
            "read. Tell the candidate this and ask for a text-based version.",
        )

    masked, contact_fields = mask_contact_details(clean_extracted_text(raw))
    sections = detect_sections(masked)
    result = ParsedResume(
        file_name=path.name,
        file_type=suffix.lstrip("."),
        word_count=len(masked.split()),
        sections=[ResumeSection(heading=k, content=v) for k, v in sections.items()],
        missing_standard_sections=[s for s in REQUIRED_SECTIONS if s not in sections],
        contact_fields_detected=contact_fields,
        full_text=masked.strip(),
    )
    return result.model_dump(mode="json")
