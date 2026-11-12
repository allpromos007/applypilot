"""Job discovery tools backed by public Greenhouse and Lever job-board APIs.

Both APIs are public, read-only and need no API key:
- Greenhouse: https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
- Lever:      https://api.lever.co/v0/postings/{company}?mode=json
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from app.config import get_settings
from app.tools.errors import ToolErrorCode, tool_error, validation_error
from app.tools.schemas import (
    JobDetailsInput,
    JobPostingDetails,
    JobPostingSummary,
    JobSearchInput,
    JobSearchOutput,
    JobSource,
    WorkplaceType,
)

GREENHOUSE_LIST = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
GREENHOUSE_JOB = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
LEVER_LIST = "https://api.lever.co/v0/postings/{company}?mode=json"
LEVER_JOB = "https://api.lever.co/v0/postings/{company}/{job_id}"
USER_AGENT = "ApplyPilot/0.1 (L200 capstone; contact via GitHub repo)"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def html_to_text(raw_html: str) -> str:
    """Convert (possibly HTML-escaped) job-board HTML into readable plain text."""
    text = html.unescape(raw_html or "")
    text = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _infer_workplace(
    location: str, text: str, hint: str | None = None
) -> WorkplaceType:
    blob = f"{hint or ''} {location} {text[:600]}".lower()
    if "hybrid" in blob:
        return WorkplaceType.HYBRID
    if "remote" in blob:
        return WorkplaceType.REMOTE
    if location:
        return WorkplaceType.ONSITE
    return WorkplaceType.ANY


def _load_default_companies() -> tuple[list[str], list[str]]:
    settings = get_settings().jobs
    greenhouse, lever = list(settings.greenhouse_boards), list(settings.lever_companies)
    path = Path(settings.companies_file)
    if path.is_file():
        data = yaml.safe_load(path.read_text()) or {}
        greenhouse += [str(b) for b in data.get("greenhouse", []) or []]
        lever += [str(c) for c in data.get("lever", []) or []]
    return sorted(set(greenhouse)), sorted(set(lever))


def _fetch_json(client: httpx.Client, url: str) -> Any:
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def _normalise_greenhouse(board: str, job: dict[str, Any]) -> dict[str, Any]:
    location = (job.get("location") or {}).get("name", "") or ""
    text = html_to_text(job.get("content", ""))
    return {
        "job_id": f"greenhouse:{board}:{job['id']}",
        "source": JobSource.GREENHOUSE,
        "company": job.get("company_name") or board,
        "title": job.get("title", "").strip(),
        "location": location,
        "workplace_type": _infer_workplace(location, text),
        "url": job.get("absolute_url")
        or f"https://boards.greenhouse.io/{board}/jobs/{job['id']}",
        "text": text,
    }


def _normalise_lever(company: str, job: dict[str, Any]) -> dict[str, Any]:
    categories = job.get("categories") or {}
    location = categories.get("location", "") or ""
    parts = [job.get("descriptionPlain") or html_to_text(job.get("description", ""))]
    for block in job.get("lists") or []:
        parts.append(block.get("text", ""))
        parts.append(html_to_text(block.get("content", "")))
    parts.append(job.get("additionalPlain") or "")
    text = "\n".join(p for p in parts if p).strip()
    return {
        "job_id": f"lever:{company}:{job['id']}",
        "source": JobSource.LEVER,
        "company": company,
        "title": (job.get("text") or "").strip(),
        "location": location,
        "workplace_type": _infer_workplace(location, text, job.get("workplaceType")),
        "url": job.get("hostedUrl") or f"https://jobs.lever.co/{company}/{job['id']}",
        "text": text,
    }


def _has_word(word: str, text: str) -> bool:
    """Whole-word match, so 'data' does not match 'database'."""
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def _score_job(
    job: dict[str, Any], prefs: JobSearchInput
) -> tuple[float, list[str]] | None:
    """Return (score, reasons) or None if the job is filtered out."""
    title = job["title"].lower()
    blob = f"{title} {job['location'].lower()} {job['text'].lower()}"

    for term in prefs.excluded_keywords:
        if _has_word(term, blob):
            return None

    title_hits = [
        t for t in prefs.target_titles if all(_has_word(w, title) for w in t.split())
    ]
    if not title_hits:
        return None

    if prefs.workplace_type != WorkplaceType.ANY and job["workplace_type"] not in (
        prefs.workplace_type,
        WorkplaceType.ANY,
    ):
        return None

    reasons = [f"title matches '{title_hits[0]}'"]
    score = 50.0

    if prefs.locations:
        loc = job["location"].lower()
        loc_hits = [p for p in prefs.locations if p in loc]
        remote_ok = job["workplace_type"] == WorkplaceType.REMOTE and any(
            p in ("remote", "anywhere") for p in prefs.locations
        )
        if not loc_hits and not remote_ok:
            return None
        score += 15
        reasons.append(f"location fits ({job['location'] or 'remote'})")
    else:
        score += 10

    if prefs.required_keywords:
        hits = [k for k in prefs.required_keywords if _has_word(k, blob)]
        score += 35 * len(hits) / len(prefs.required_keywords)
        if hits:
            reasons.append("mentions " + ", ".join(hits))
    else:
        score += 20

    return min(score, 100.0), reasons


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def search_matching_job_postings(
    target_titles: list[str],
    locations: list[str] | None = None,
    workplace_type: str = "any",
    required_keywords: list[str] | None = None,
    excluded_keywords: list[str] | None = None,
    greenhouse_boards: list[str] | None = None,
    lever_companies: list[str] | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Find open jobs that match the candidate's preferences on Greenhouse and Lever.

    Searches public company job boards, filters by title, location, workplace
    type and excluded terms, then ranks results by preference fit (0-100).
    Call this after the candidate's preferences are known. Use the returned
    `job_id` with get_job_posting_details to read the full description.

    Args:
        target_titles: Desired job titles/keywords, e.g. ["data engineer"]. Every
            word of a title must appear in the posting title to match.
        locations: Acceptable locations, e.g. ["new york", "remote"]. Empty = any.
        workplace_type: One of "remote", "hybrid", "onsite", "any".
        required_keywords: Skills that should appear in the posting, e.g.
            ["python", "gcp"]. Used for ranking, not hard filtering.
        excluded_keywords: Terms that disqualify a posting, e.g. ["crypto"].
        greenhouse_boards: Greenhouse board tokens to search. If both company lists
            are empty, the defaults in config/companies.yaml are used; if either is
            set, ONLY the named companies are searched.
        lever_companies: Lever company slugs to search (same default rule).
        max_results: Maximum jobs to return (1-50).

    Returns:
        On success: {"status": "success", "total_scanned", "returned", "jobs":
        [{"job_id", "source", "company", "title", "location", "workplace_type",
        "url", "match_score", "match_reasons"}], "boards_failed"}.
        On failure: {"status": "error", "error_code", "message",
        "recovery_instruction"}.
    """
    try:
        prefs = JobSearchInput(
            target_titles=target_titles,
            locations=locations or [],
            workplace_type=workplace_type,
            required_keywords=required_keywords or [],
            excluded_keywords=excluded_keywords or [],
            greenhouse_boards=greenhouse_boards or [],
            lever_companies=lever_companies or [],
            max_results=max_results,
        )
    except ValidationError as exc:
        return validation_error(exc, "search_matching_job_postings")

    # If the caller names ANY companies, search only those; otherwise use defaults.
    if prefs.greenhouse_boards or prefs.lever_companies:
        gh_boards, lever_cos = prefs.greenhouse_boards, prefs.lever_companies
    else:
        gh_boards, lever_cos = _load_default_companies()
    if not gh_boards and not lever_cos:
        return tool_error(
            ToolErrorCode.NO_COMPANIES_CONFIGURED,
            "No Greenhouse boards or Lever companies are configured to search.",
            "Ask the candidate which companies they are interested in, then call again "
            "with greenhouse_boards and/or lever_companies set to their board names.",
        )

    settings = get_settings().jobs
    jobs: list[dict[str, Any]] = []
    failed: list[str] = []
    with httpx.Client(
        timeout=settings.http_timeout_seconds, headers={"User-Agent": USER_AGENT}
    ) as client:
        for board in gh_boards:
            try:
                data = _fetch_json(client, GREENHOUSE_LIST.format(board=board))
                jobs += [_normalise_greenhouse(board, j) for j in data.get("jobs", [])]
            except (httpx.HTTPError, ValueError):
                failed.append(f"greenhouse:{board}")
        for company in lever_cos:
            try:
                data = _fetch_json(client, LEVER_LIST.format(company=company))
                jobs += [_normalise_lever(company, j) for j in data or []]
            except (httpx.HTTPError, ValueError):
                failed.append(f"lever:{company}")

    if not jobs and failed:
        return tool_error(
            ToolErrorCode.JOB_BOARD_UNAVAILABLE,
            f"None of the job boards could be reached: {', '.join(failed)}.",
            "Board names may be misspelled (they are case-sensitive slugs like "
            "'airbnb'), or the service may be down. Verify the names with the "
            "candidate or retry once in a minute.",
            boards_failed=failed,
        )

    ranked = []
    for job in jobs:
        result = _score_job(job, prefs)
        if result:
            score, reasons = result
            ranked.append((score, job, reasons))
    ranked.sort(key=lambda item: item[0], reverse=True)

    if not ranked:
        return tool_error(
            ToolErrorCode.NO_JOBS_MATCHED,
            f"Scanned {len(jobs)} postings but none matched the preferences.",
            "Suggest relaxing filters to the candidate: broaden target_titles (e.g. "
            "'engineer' instead of 'senior data engineer'), clear locations, set "
            "workplace_type to 'any', or add more companies. Then search again.",
            total_scanned=len(jobs),
            boards_failed=failed,
        )

    top = ranked[: prefs.max_results]
    output = JobSearchOutput(
        total_scanned=len(jobs),
        returned=len(top),
        jobs=[
            JobPostingSummary(
                job_id=j["job_id"],
                source=j["source"],
                company=j["company"],
                title=j["title"],
                location=j["location"] or "Not specified",
                workplace_type=j["workplace_type"],
                url=j["url"],
                match_score=round(score, 1),
                match_reasons=reasons,
            )
            for score, j, reasons in top
        ],
        boards_failed=failed,
    )
    return output.model_dump(mode="json")


def get_job_posting_details(job_id: str) -> dict[str, Any]:
    """Fetch the full description of one job posting so it can be analysed.

    Call this before extracting requirements, scoring a resume or writing a
    cover letter for a specific job.

    Args:
        job_id: The id exactly as returned by search_matching_job_postings, in the
            form "<source>:<company>:<id>", e.g. "greenhouse:airbnb:7123456".

    Returns:
        On success: {"status": "success", "job_id", "company", "title",
        "location", "url", "description_text"}.
        On failure: {"status": "error", "error_code", "message",
        "recovery_instruction"}.
    """
    try:
        args = JobDetailsInput(job_id=job_id)
    except ValidationError as exc:
        return validation_error(exc, "get_job_posting_details")

    source, company, raw_id = args.job_id.split(":", 2)
    url = (
        GREENHOUSE_JOB.format(board=company, job_id=raw_id)
        if source == JobSource.GREENHOUSE
        else LEVER_JOB.format(company=company, job_id=raw_id)
    )
    settings = get_settings().jobs
    try:
        with httpx.Client(
            timeout=settings.http_timeout_seconds, headers={"User-Agent": USER_AGENT}
        ) as client:
            data = _fetch_json(client, url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return tool_error(
                ToolErrorCode.JOB_NOT_FOUND,
                f"Job '{args.job_id}' no longer exists (it may have been closed).",
                "Tell the candidate this posting is closed and call "
                "search_matching_job_postings again to find current openings.",
            )
        return tool_error(
            ToolErrorCode.JOB_BOARD_UNAVAILABLE,
            f"The {source} API returned HTTP {exc.response.status_code}.",
            "Retry once; if it fails again, continue with another job.",
        )
    except (httpx.HTTPError, ValueError):
        return tool_error(
            ToolErrorCode.JOB_BOARD_UNAVAILABLE,
            f"Could not reach the {source} API.",
            "Retry once; if it fails again, continue with another job.",
        )

    job = (
        _normalise_greenhouse(company, data)
        if source == JobSource.GREENHOUSE
        else _normalise_lever(company, data)
    )
    return JobPostingDetails(
        job_id=job["job_id"],
        company=job["company"],
        title=job["title"],
        location=job["location"] or "Not specified",
        url=job["url"],
        description_text=job["text"],
    ).model_dump(mode="json")
