"""Job-description analysis and ATS scoring tools.

`calculate_ats_match_score` is fully deterministic and is used for BOTH the
"before" and "after" scores, so the uplift the candidate sees is an
apples-to-apples comparison rather than an LLM's opinion.

Scoring model (weights sum to 1.0):
    keyword_match      0.40  required skills (x2 weight) + preferred skills + top JD terms
    section_structure  0.20  summary / experience / education / skills present
    quantified_impact  0.15  share of bullets containing metrics (%, $, counts)
    title_alignment    0.15  target job title words appear in the resume
    ats_formatting     0.10  length, no tables/pipes, no unusual characters
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from app.tools.errors import validation_error
from app.tools.schemas import (
    AtsScoreInput,
    AtsScoreOutput,
    ExtractRequirementsInput,
    JobRequirements,
    ScoreComponent,
)
from app.tools.text_analysis import (
    REQUIRED_SECTIONS,
    count_quantified_bullets,
    detect_sections,
    estimate_min_years,
    estimate_seniority,
    extract_skills,
    normalise,
    top_keywords,
)

WEIGHTS = {
    "keyword_match": 0.40,
    "section_structure": 0.20,
    "quantified_impact": 0.15,
    "title_alignment": 0.15,
    "ats_formatting": 0.10,
}
PREFERRED_MARKERS = re.compile(
    r"(?i)(nice to have|preferred|bonus|plus|desired|good to have)"
)
TITLE_NOISE = {
    "senior",
    "sr",
    "junior",
    "jr",
    "lead",
    "staff",
    "principal",
    "i",
    "ii",
    "iii",
    "iv",
    "-",
    "&",
    "and",
    "of",
}


def _split_required_preferred(job_description: str) -> tuple[str, str]:
    """Split the JD at the first 'preferred / nice to have' marker."""
    match = PREFERRED_MARKERS.search(job_description)
    if not match:
        return job_description, ""
    return job_description[: match.start()], job_description[match.start() :]


def _requirements(job_description: str, job_title: str) -> JobRequirements:
    required_text, preferred_text = _split_required_preferred(job_description)
    required = extract_skills(job_title + "\n" + required_text)
    preferred = [s for s in extract_skills(preferred_text) if s not in required]
    return JobRequirements(
        job_title=job_title or "Unknown",
        seniority=estimate_seniority(job_title, job_description),
        required_skills=required,
        preferred_skills=preferred,
        top_keywords=top_keywords(job_description, limit=15),
        min_years_experience=estimate_min_years(job_description),
    )


def extract_job_requirements(
    job_description: str, job_title: str = ""
) -> dict[str, Any]:
    """Extract the skills, keywords and seniority an ATS will screen for in a job.

    Use this to explain to the candidate what a posting is really asking for,
    and to plan which truthful resume changes would improve keyword coverage.

    Args:
        job_description: Full plain-text job description (at least 50 characters),
            e.g. the `description_text` from get_job_posting_details.
        job_title: The posting's title, e.g. "Senior Data Engineer".

    Returns:
        {"status": "success", "job_title", "seniority", "required_skills",
        "preferred_skills", "top_keywords", "min_years_experience"} or a guided
        error dict with "recovery_instruction".
    """
    try:
        args = ExtractRequirementsInput(
            job_description=job_description, job_title=job_title
        )
    except ValidationError as exc:
        return validation_error(exc, "extract_job_requirements")
    return _requirements(args.job_description, args.job_title).model_dump(mode="json")


def _keyword_component(
    resume_text: str, reqs: JobRequirements
) -> tuple[ScoreComponent, list[str], list[str]]:
    resume_skills = set(extract_skills(resume_text))
    resume_norm = normalise(resume_text)
    req_hit = [s for s in reqs.required_skills if s in resume_skills]
    pref_hit = [s for s in reqs.preferred_skills if s in resume_skills]
    kw_pool = [k for k in reqs.top_keywords if k not in reqs.required_skills][:10]
    kw_hit = [k for k in kw_pool if k in resume_norm]

    points = 2 * len(req_hit) + len(pref_hit) + 0.5 * len(kw_hit)
    possible = (
        2 * len(reqs.required_skills) + len(reqs.preferred_skills) + 0.5 * len(kw_pool)
    )
    score = 100.0 * points / possible if possible else 100.0

    matched = req_hit + pref_hit
    missing = [s for s in reqs.required_skills if s not in resume_skills] + [
        s for s in reqs.preferred_skills if s not in resume_skills
    ]
    notes = [
        f"required skills matched {len(req_hit)}/{len(reqs.required_skills)}",
        f"preferred skills matched {len(pref_hit)}/{len(reqs.preferred_skills)}",
        f"top JD terms present {len(kw_hit)}/{len(kw_pool)}",
    ]
    return (
        ScoreComponent(
            name="keyword_match",
            score=round(score, 1),
            weight=WEIGHTS["keyword_match"],
            notes=notes,
        ),
        matched,
        missing,
    )


def _section_component(resume_text: str) -> ScoreComponent:
    sections = detect_sections(resume_text)
    present = [s for s in REQUIRED_SECTIONS if s in sections]
    missing = [s for s in REQUIRED_SECTIONS if s not in sections]
    notes = [f"found: {', '.join(present) or 'none'}"]
    if missing:
        notes.append(f"missing: {', '.join(missing)}")
    return ScoreComponent(
        name="section_structure",
        score=round(100.0 * len(present) / len(REQUIRED_SECTIONS), 1),
        weight=WEIGHTS["section_structure"],
        notes=notes,
    )


def _impact_component(resume_text: str) -> ScoreComponent:
    sections = detect_sections(resume_text)
    body = sections.get("experience") or resume_text
    quantified, total = count_quantified_bullets(body)
    ratio = quantified / total if total else 0.0
    # 60%+ quantified bullets earns full marks.
    score = min(100.0, 100.0 * ratio / 0.6)
    return ScoreComponent(
        name="quantified_impact",
        score=round(score, 1),
        weight=WEIGHTS["quantified_impact"],
        notes=[f"{quantified}/{total} experience bullets include a metric"],
    )


def _title_component(resume_text: str, job_title: str) -> ScoreComponent:
    words = [
        w for w in re.findall(r"[a-z0-9+#]+", job_title.lower()) if w not in TITLE_NOISE
    ]
    if not words:
        return ScoreComponent(
            name="title_alignment",
            score=100.0,
            weight=WEIGHTS["title_alignment"],
            notes=["no job title supplied"],
        )
    resume_norm = normalise(resume_text)
    hits = [
        w
        for w in words
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", resume_norm)
    ]
    full_phrase = " ".join(words) in resume_norm
    score = 100.0 if full_phrase else 80.0 * len(hits) / len(words)
    note = (
        "exact title phrase present"
        if full_phrase
        else f"title words present {len(hits)}/{len(words)}"
    )
    return ScoreComponent(
        name="title_alignment",
        score=round(score, 1),
        weight=WEIGHTS["title_alignment"],
        notes=[note],
    )


def _formatting_component(resume_text: str) -> ScoreComponent:
    score, notes = 100.0, []
    words = len(resume_text.split())
    if words < 250:
        score -= 30
        notes.append(f"short resume ({words} words); aim for 400-900")
    elif words > 1100:
        score -= 20
        notes.append(f"long resume ({words} words); aim for 400-900")
    pipe_lines = sum(1 for ln in resume_text.splitlines() if ln.count("|") >= 2)
    if pipe_lines > 2:
        score -= 25
        notes.append("table-like rows detected; ATS parsers often scramble tables")
    odd = len(
        re.findall(
            r"[^\x00-\x7F\u2013\u2014\u2018\u2019\u201c\u201d\u2022]", resume_text
        )
    )
    if odd > 10:
        score -= 15
        notes.append("many special characters/icons; use plain text")
    if not notes:
        notes.append("plain, parseable formatting")
    return ScoreComponent(
        name="ats_formatting",
        score=max(score, 0.0),
        weight=WEIGHTS["ats_formatting"],
        notes=notes,
    )


def _recommendations(components: list[ScoreComponent], missing: list[str]) -> list[str]:
    recs = []
    by_name = {c.name: c for c in components}
    if missing:
        recs.append(
            "Add these JD skills ONLY if the candidate genuinely has them: "
            + ", ".join(missing[:10])
        )
    if by_name["section_structure"].score < 100:
        recs.append("Use standard headings: Summary, Experience, Education, Skills.")
    if by_name["quantified_impact"].score < 70:
        recs.append(
            "Quantify achievements (%, $, time saved, users, scale) in experience bullets."
        )
    if by_name["title_alignment"].score < 80:
        recs.append(
            "Mirror the target job title in the summary if it reflects real experience."
        )
    if by_name["ats_formatting"].score < 100:
        recs.append("Remove tables/columns/icons; keep a single-column plain layout.")
    return recs


def calculate_ats_match_score(
    resume_text: str,
    job_description: str,
    job_title: str = "",
    label: str = "before",
) -> dict[str, Any]:
    """Score how well a resume would pass an ATS screen for a specific job (0-100).

    Deterministic and repeatable: the same inputs always give the same score.
    Call it on the ORIGINAL resume with label="before", and again on the
    tailored resume with label="after", to show the candidate the improvement.

    Args:
        resume_text: Plain-text resume (at least 100 characters), e.g. the
            `full_text` from parse_resume_document or a tailored draft.
        job_description: Plain-text job description of the target job.
        job_title: Target job title, e.g. "Senior Data Engineer".
        label: "before" (original resume), "after" (tailored resume) or "other".

    Returns:
        {"status": "success", "label", "overall_score", "components":
        [{"name", "score", "weight", "notes"}], "matched_keywords",
        "missing_keywords", "recommendations"} or a guided error dict with
        "recovery_instruction".
    """
    try:
        args = AtsScoreInput(
            resume_text=resume_text,
            job_description=job_description,
            job_title=job_title,
            label=label,
        )
    except ValidationError as exc:
        return validation_error(exc, "calculate_ats_match_score")

    reqs = _requirements(args.job_description, args.job_title)
    keyword, matched, missing = _keyword_component(args.resume_text, reqs)
    components = [
        keyword,
        _section_component(args.resume_text),
        _impact_component(args.resume_text),
        _title_component(args.resume_text, args.job_title),
        _formatting_component(args.resume_text),
    ]
    overall = sum(c.score * c.weight for c in components)
    return AtsScoreOutput(
        label=args.label,
        overall_score=round(overall, 1),
        components=components,
        matched_keywords=matched,
        missing_keywords=missing,
        recommendations=_recommendations(components, missing),
    ).model_dump(mode="json")
