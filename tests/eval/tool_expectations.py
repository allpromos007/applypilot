"""Deterministic metric: did the agent call the tools it must, and avoid the ones it must not?

Expectations live on each case as `rubric_groups.tools` with rubric ids
`must_call:<tool>` / `must_not_call:<tool>`. Only presence/absence is checked,
never exact order, so harmless extra steps don't fail a case.
Score = fraction of expectations met (0-1).
"""


def _called_tools(node, found):
    if isinstance(node, dict):
        call = node.get("function_call")
        if isinstance(call, dict) and call.get("name"):
            found.add(call["name"])
        for value in node.values():
            _called_tools(value, found)
    elif isinstance(node, list):
        for value in node:
            _called_tools(value, found)
    return found


def evaluate(instance):
    group = (instance.get("rubric_groups") or {}).get("tools") or {}
    expectations = [r["rubric_id"] for r in group.get("rubrics", [])]
    if not expectations:
        return {"score": 1.0, "explanation": "no tool expectations for this case"}
    called = _called_tools(instance.get("agent_data") or {}, set())
    failures = []
    for rubric_id in expectations:
        kind, _, tool = rubric_id.partition(":")
        if kind == "must_call" and tool not in called:
            failures.append(f"missing call to {tool}")
        elif kind == "must_not_call" and tool in called:
            failures.append(f"forbidden call to {tool}")
    score = 1 - len(failures) / len(expectations)
    return {
        "score": round(score, 3),
        "explanation": "; ".join(failures) or f"all met (called: {sorted(called)})",
    }
