"""Golden-set regression suite (rubric 5.1), deterministic with no model calls.

Each JSON file in tests/eval/golden/ is a golden dataset for one quality-critical
component. Runs in CI on every push, so regressions in scoring, fabrication
detection, injection detection or PII redaction fail the build before deploy.

The LLM-in-the-loop golden set (the full agent) lives in
tests/eval/datasets/golden.json and runs with `agents-cli eval run`.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

from app.guardrails import detect_prompt_injection, find_unsupported_claims
from app.observability.pii import PIIScrubber
from app.tools import calculate_ats_match_score, parse_resume_document

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "eval" / "golden"


def _load(name: str) -> dict:
    return json.loads((GOLDEN / name).read_text())


ATS = _load("ats_scorer.json")
TRUTH = _load("truthfulness.json")
INJECTION = _load("prompt_injection.json")
PII = _load("pii_redaction.json")


@cache
def _score(resume: str, job: str) -> dict:
    text = parse_resume_document(ATS["resumes"][resume])["full_text"]
    meta = ATS["jobs"][job]
    jd = (ROOT / meta["path"]).read_text()
    result = calculate_ats_match_score(text, jd, meta["title"], "before")
    return {
        "overall": result["overall_score"],
        **{c["name"]: c["score"] for c in result["components"]},
    }


@pytest.mark.parametrize(
    "case", ATS["baselines"], ids=lambda c: f"{c['resume']}-{c['job']}"
)
def test_ats_scores_match_baseline(case):
    got = _score(case["resume"], case["job"])
    for metric in ("overall", "keyword_match"):
        assert abs(got[metric] - case[metric]) <= ATS["tolerance"], (
            f"{metric} drifted: {got[metric]} vs baseline {case[metric]}"
        )


@pytest.mark.parametrize("inv", ATS["invariants"], ids=lambda i: i["name"])
def test_ats_invariants(inv):
    metric = inv.get("metric", "overall")
    assert _score(*inv["better"])[metric] > _score(*inv["worse"])[metric]


@pytest.mark.parametrize("case", ATS["honest_tailoring_cases"], ids=lambda c: c["name"])
def test_honest_tailoring_improves_score(case):
    before = calculate_ats_match_score(
        case["before"], case["job_description"], case["title"], "before"
    )
    after = calculate_ats_match_score(
        case["after"], case["job_description"], case["title"], "after"
    )
    gain = after["overall_score"] - before["overall_score"]
    assert gain >= case["min_gain"], f"gain {gain:.1f} < {case['min_gain']}"


@pytest.mark.parametrize("case", TRUTH["cases"], ids=lambda c: c["name"])
def test_truthfulness_evidence(case):
    assert (
        find_unsupported_claims(case["original"], case["tailored"])
        == case["unsupported"]
    )


@pytest.mark.parametrize("case", INJECTION["cases"], ids=lambda c: c["text"][:40])
def test_prompt_injection_detector(case):
    assert bool(detect_prompt_injection(case["text"])) is case["flagged"]


_SCRUBBER = PIIScrubber(use_dlp=False)


@pytest.mark.parametrize("case", PII["cases"], ids=lambda c: c["input"][:40])
def test_pii_redaction(case):
    out = _SCRUBBER.scrub_text(case["input"], case.get("known_values", []))
    for secret in case["must_not_contain"]:
        assert secret.lower() not in out.lower(), f"leaked {secret!r}: {out}"
    for keep in case["must_keep"]:
        assert keep in out, f"over-redacted {keep!r}: {out}"
