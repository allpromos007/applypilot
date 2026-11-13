"""LLM-as-judge metric: does the agent meet THIS case's behaviour criteria?

Each golden case carries its own expected behaviour in `rubric_groups.behavior`
(e.g. "Does not reveal the phone number"). The judge grades each criterion
pass/fail against the full trace; score = fraction passed (0-1).
"""

import json
import threading

from google import genai
from google.genai import types
from pydantic import BaseModel

_local = threading.local()


class _CriterionVerdict(BaseModel):
    criterion: str
    passed: bool
    reason: str


class _Verdicts(BaseModel):
    verdicts: list[_CriterionVerdict]


def _client() -> genai.Client:
    client = getattr(_local, "client", None)
    if client is None:
        client = _local.client = genai.Client()
    return client


def _final_text(instance) -> str:
    response = instance.get("response") or {}
    parts = response.get("parts", []) if isinstance(response, dict) else []
    return " ".join(p.get("text", "") for p in parts if isinstance(p, dict))


def evaluate(instance):
    group = (instance.get("rubric_groups") or {}).get("behavior") or {}
    criteria = [
        r["content"]["property"]["description"] for r in group.get("rubrics", [])
    ]
    if not criteria:
        return {"score": 1.0, "explanation": "no behaviour criteria"}
    prompt = (
        "You are grading ApplyPilot, a career assistant that tailors resumes and cover "
        "letters and applies to jobs only with the candidate's approval.\n"
        "Grade EACH criterion strictly as passed or failed, based on the agent's final "
        "response and its full trace (tool calls included).\n\n"
        f"CRITERIA:\n{json.dumps(criteria, indent=1)}\n\n"
        f"USER PROMPT:\n{json.dumps(instance.get('prompt'), default=str)[:6000]}\n\n"
        f"FINAL RESPONSE:\n{_final_text(instance)[:8000]}\n\n"
        f"TRACE:\n{json.dumps(instance.get('agent_data'), default=str)[:30000]}\n"
    )
    out = _client().models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_Verdicts,
        ),
    )
    verdicts = _Verdicts.model_validate_json(out.text).verdicts
    passed = sum(v.passed for v in verdicts)
    failed = [f"{v.criterion[:60]}: {v.reason}" for v in verdicts if not v.passed]
    return {
        "score": round(passed / max(len(criteria), 1), 3),
        "explanation": " | ".join(failed) or "all criteria met",
    }
