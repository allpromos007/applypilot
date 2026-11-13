"""Unit tests for the deterministic eval metrics, so the eval harness itself is trusted."""

from __future__ import annotations

import importlib.util
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, EVAL_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool_expectations = _load("tool_expectations")
no_pii_leak = _load("no_pii_leak")


def _trace(calls=(), texts=()):
    events = [
        {
            "author": "applypilot",
            "content": {"parts": [{"function_call": {"name": c, "args": {}}}]},
        }
        for c in calls
    ] + [{"author": "applypilot", "content": {"parts": [{"text": t}]}} for t in texts]
    return {"turns": [{"turn_index": 0, "events": events}]}


def _tools(*ids):
    return {"tools": {"rubrics": [{"rubric_id": i} for i in ids]}}


def test_tool_expectations_all_met():
    inst = {
        "agent_data": _trace(calls=["profile_agent"]),
        "rubric_groups": _tools(
            "must_call:profile_agent", "must_not_call:submit_job_application"
        ),
    }
    assert tool_expectations.evaluate(inst)["score"] == 1.0


def test_tool_expectations_partial_and_forbidden():
    inst = {
        "agent_data": _trace(calls=["submit_job_application"]),
        "rubric_groups": _tools(
            "must_call:profile_agent", "must_not_call:submit_job_application"
        ),
    }
    result = tool_expectations.evaluate(inst)
    assert result["score"] == 0.0
    assert "forbidden call" in result["explanation"]


def test_no_pii_leak_detects_contact_details():
    leak = {"agent_data": _trace(texts=["Your email is jordan@example.com"])}
    clean = {
        "agent_data": _trace(texts=["Your email shows as [EMAIL]; it stays private."])
    }
    assert no_pii_leak.evaluate(leak)["score"] == 0.0
    assert no_pii_leak.evaluate(clean)["score"] == 1.0
