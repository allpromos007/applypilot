"""Unit tests for ApplyPilot tools (no network, no LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools import ats_tools, job_search_tools, resume_tools
from app.tools.errors import ToolErrorCode

ROOT = Path(__file__).resolve().parents[2]
RESUME = ROOT / "samples/resumes/jordan_avery_data_engineer.txt"
JD = (ROOT / "samples/job_descriptions/senior_data_engineer_gcp.txt").read_text()


# --------------------------------------------------------------------------- #
# parse_resume_document
# --------------------------------------------------------------------------- #
def test_parse_resume_masks_pii_and_finds_sections():
    result = resume_tools.parse_resume_document(str(RESUME))
    assert result["status"] == "success"
    assert set(result["contact_fields_detected"]) == {"email", "phone", "linkedin"}
    assert "jordan.avery@example.com" not in result["full_text"]
    assert "555-0142" not in result["full_text"]
    headings = {s["heading"] for s in result["sections"]}
    assert {"summary", "experience", "education", "skills"} <= headings
    assert result["missing_standard_sections"] == []


def test_parse_resume_docx(tmp_path):
    import docx

    doc = docx.Document()
    for line in RESUME.read_text().splitlines():
        doc.add_paragraph(line)
    path = tmp_path / "resume.docx"
    doc.save(path)
    result = resume_tools.parse_resume_document(str(path))
    assert result["status"] == "success"
    assert result["file_type"] == "docx"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("does/not/exist.pdf", ToolErrorCode.FILE_NOT_FOUND),
        ("resume.jpg", ToolErrorCode.UNSUPPORTED_FILE_TYPE),
        ("", ToolErrorCode.INVALID_INPUT),
    ],
)
def test_parse_resume_guided_errors(path, code):
    result = resume_tools.parse_resume_document(path)
    assert result["status"] == "error"
    assert result["error_code"] == code
    assert result["recovery_instruction"]


def test_parse_resume_rejects_near_empty_file(tmp_path):
    path = tmp_path / "scan.txt"
    path.write_text("Jordan Avery resume")
    result = resume_tools.parse_resume_document(str(path))
    assert result["error_code"] == ToolErrorCode.EMPTY_DOCUMENT


# --------------------------------------------------------------------------- #
# extract_job_requirements / calculate_ats_match_score
# --------------------------------------------------------------------------- #
def test_extract_job_requirements_splits_required_and_preferred():
    reqs = ats_tools.extract_job_requirements(JD, "Senior Data Engineer")
    assert reqs["status"] == "success"
    assert reqs["seniority"] == "senior"
    assert reqs["min_years_experience"] == 5
    for skill in (
        "python",
        "sql",
        "bigquery",
        "airflow",
        "kafka",
        "terraform",
        "kubernetes",
    ):
        assert skill in reqs["required_skills"]
    assert "dbt" in reqs["preferred_skills"]


def test_ats_score_is_deterministic_and_bounded():
    text = resume_tools.parse_resume_document(str(RESUME))["full_text"]
    first = ats_tools.calculate_ats_match_score(text, JD, "Senior Data Engineer")
    second = ats_tools.calculate_ats_match_score(text, JD, "Senior Data Engineer")
    assert first == second
    assert 0 <= first["overall_score"] <= 100
    assert abs(sum(c["weight"] for c in first["components"]) - 1.0) < 1e-9
    assert "bigquery" in first["missing_keywords"]
    assert "python" in first["matched_keywords"]


def test_ats_score_improves_after_truthful_tailoring():
    original = resume_tools.parse_resume_document(str(RESUME))["full_text"]
    before = ats_tools.calculate_ats_match_score(
        original, JD, "Senior Data Engineer", "before"
    )
    tailored = original.replace(
        "SUMMARY\nData engineer",
        "SUMMARY\nSenior Data Engineer: data engineer",
    ).replace(
        "- Designed dimensional data models used by the finance and operations teams.",
        "- Designed dimensional data models used by 3 teams (finance, operations, analytics), "
        "serving 50 dashboards.",
    )
    after = ats_tools.calculate_ats_match_score(
        tailored, JD, "Senior Data Engineer", "after"
    )
    assert after["label"] == "after"
    assert after["overall_score"] > before["overall_score"]


def test_ats_score_guided_error_on_short_input():
    result = ats_tools.calculate_ats_match_score("too short", JD)
    assert result["status"] == "error"
    assert result["error_code"] == ToolErrorCode.INVALID_INPUT
    assert "resume_text" in result["message"]


# --------------------------------------------------------------------------- #
# search_matching_job_postings / get_job_posting_details (HTTP mocked)
# --------------------------------------------------------------------------- #
GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 111,
            "title": "Senior Data Engineer",
            "company_name": "Acme",
            "location": {"name": "Remote - US"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/111",
            "content": "&lt;p&gt;Build pipelines with Python and BigQuery.&lt;/p&gt;",
        },
        {
            "id": 222,
            "title": "Data Engineer, Crypto Payments",
            "company_name": "Acme",
            "location": {"name": "New York, NY"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/222",
            "content": "<p>Crypto ledger pipelines.</p>",
        },
        {
            "id": 333,
            "title": "Account Executive",
            "company_name": "Acme",
            "location": {"name": "Remote - US"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/333",
            "content": "<p>Sales.</p>",
        },
    ]
}
LEVER_PAYLOAD = [
    {
        "id": "abc-123",
        "text": "Data Engineer",
        "categories": {"location": "Seattle, WA"},
        "workplaceType": "hybrid",
        "hostedUrl": "https://jobs.lever.co/globex/abc-123",
        "descriptionPlain": "Python, Airflow and Kafka pipelines.",
        "lists": [],
    }
]


@pytest.fixture
def fake_boards(monkeypatch):
    def fake_fetch(_client, url):
        if "greenhouse" in url and "acme" in url:
            return GREENHOUSE_PAYLOAD
        if "lever" in url and "globex" in url:
            return LEVER_PAYLOAD
        raise job_search_tools.httpx.ConnectError("unreachable")

    monkeypatch.setattr(job_search_tools, "_fetch_json", fake_fetch)


def test_search_filters_and_ranks(fake_boards):
    result = job_search_tools.search_matching_job_postings(
        target_titles=["data engineer"],
        excluded_keywords=["crypto"],
        required_keywords=["python", "bigquery"],
        greenhouse_boards=["acme"],
        lever_companies=["globex"],
    )
    assert result["status"] == "success"
    ids = [j["job_id"] for j in result["jobs"]]
    assert ids[0] == "greenhouse:acme:111"  # matches both keywords -> ranked first
    assert "greenhouse:acme:222" not in ids  # excluded keyword
    assert "greenhouse:acme:333" not in ids  # title mismatch
    assert "lever:globex:abc-123" in ids
    assert result["jobs"][0]["workplace_type"] == "remote"


def test_search_title_uses_whole_words(monkeypatch):
    payload = {
        "jobs": [
            {
                "id": 444,
                "title": "Systems Engineer - Database Platform",
                "company_name": "Acme",
                "location": {"name": "Remote"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/444",
                "content": "<p>Postgres.</p>",
            }
        ]
    }
    monkeypatch.setattr(job_search_tools, "_fetch_json", lambda _c, _u: payload)
    result = job_search_tools.search_matching_job_postings(
        target_titles=["data engineer"], greenhouse_boards=["acme"]
    )
    assert result["error_code"] == ToolErrorCode.NO_JOBS_MATCHED


def test_search_workplace_filter(fake_boards):
    result = job_search_tools.search_matching_job_postings(
        target_titles=["data engineer"],
        workplace_type="hybrid",
        greenhouse_boards=["acme"],
        lever_companies=["globex"],
    )
    assert [j["job_id"] for j in result["jobs"]] == ["lever:globex:abc-123"]


def test_search_reports_partial_failures(fake_boards):
    result = job_search_tools.search_matching_job_postings(
        target_titles=["data engineer"], greenhouse_boards=["acme", "nope"]
    )
    assert result["status"] == "success"
    assert result["boards_failed"] == ["greenhouse:nope"]


def test_search_no_match_gives_recovery(fake_boards):
    result = job_search_tools.search_matching_job_postings(
        target_titles=["astronaut"], greenhouse_boards=["acme"]
    )
    assert result["error_code"] == ToolErrorCode.NO_JOBS_MATCHED
    assert "broaden" in result["recovery_instruction"]


def test_search_all_boards_down(fake_boards):
    result = job_search_tools.search_matching_job_postings(
        target_titles=["data engineer"], greenhouse_boards=["nope"]
    )
    assert result["error_code"] == ToolErrorCode.JOB_BOARD_UNAVAILABLE


def test_search_invalid_workplace_type():
    result = job_search_tools.search_matching_job_postings(
        target_titles=["data engineer"], workplace_type="moon"
    )
    assert result["error_code"] == ToolErrorCode.INVALID_INPUT


def test_job_details_rejects_malformed_id():
    result = job_search_tools.get_job_posting_details("123")
    assert result["error_code"] == ToolErrorCode.INVALID_INPUT


def test_job_details_greenhouse(monkeypatch):
    monkeypatch.setattr(
        job_search_tools, "_fetch_json", lambda _c, _u: GREENHOUSE_PAYLOAD["jobs"][0]
    )
    result = job_search_tools.get_job_posting_details("greenhouse:acme:111")
    assert result["status"] == "success"
    assert "BigQuery" in result["description_text"]
    assert "<p>" not in result["description_text"]
