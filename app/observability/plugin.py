"""ADK plugin that logs every step as structured, PII-safe events (rubrics 4.1, 4.2).

Registered once on the App, so it covers the coordinator AND every specialist
(AgentTool propagates plugins into nested runs).

Event types (the `event_type` JSON field):
    REQUEST_RECEIVED / REQUEST_COMPLETED   one user turn, with total latency
    AGENT_START / AGENT_END                each agent's turn
    MODEL_CALL / MODEL_RESPONSE            model routing + token usage + latency
    AGENT_INTENT                           BEFORE a tool runs: what the agent plans
                                           to do, with which (scrubbed) arguments, why
    AGENT_OUTCOME                          AFTER the tool: what actually happened,
                                           status, error code, latency, result summary
    *_ERROR                                exceptions

AGENT_INTENT and AGENT_OUTCOME share an `action_id`, so in Cloud Logging you can
filter `jsonPayload.action_id="..."` and see the plan next to the result, or
query for intents with no successful outcome.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from opentelemetry import trace

from app.observability.logging import get_logger, log_event
from app.observability.pii import get_log_scrubber as get_scrubber
from app.observability.pii import known_pii_from_state

logger = get_logger(__name__)

# Actions with real-world side effects, flagged in logs for audit.
HIGH_STAKES_TOOLS = frozenset({"submit_job_application"})
_MAX_FIELD = 300


def _session_fields(ctx: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, getter in (
        ("session_id", lambda: ctx.session.id),
        ("invocation_id", lambda: ctx.invocation_id),
        ("agent_name", lambda: ctx.agent_name),
    ):
        try:
            fields[key] = getter()
        except Exception:
            pass
    return fields


def _known(ctx: Any) -> list[str]:
    try:
        return known_pii_from_state(ctx.state)
    except Exception:
        return []


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD:
        return value[:_MAX_FIELD] + f"... (+{len(value) - _MAX_FIELD} chars)"
    return value


def summarize_result(result: Any) -> dict[str, Any]:
    """Small, log-friendly view of a tool result: scalars kept, big values sized."""
    if not isinstance(result, dict):
        return {"type": type(result).__name__, "preview": _short(str(result))}
    summary: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, (bool, int, float)) or value is None:
            summary[key] = value
        elif isinstance(value, str):
            summary[key] = _short(value)
        elif isinstance(value, (list, tuple)):
            summary[key] = f"<list len={len(value)}>"
        elif isinstance(value, dict):
            summary[key] = f"<dict keys={sorted(value)[:8]}>"
    return summary


class ObservabilityPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="applypilot_observability")
        self._started: dict[str, float] = {}
        self._rationale: dict[tuple[str, str], str] = {}

    # ---- request ---------------------------------------------------------- #
    async def before_run_callback(self, *, invocation_context):
        self._started[f"run:{invocation_context.invocation_id}"] = time.perf_counter()
        text = ""
        content = invocation_context.user_content
        if content and content.parts:
            text = " ".join(p.text for p in content.parts if getattr(p, "text", None))
        log_event(
            logger,
            "REQUEST_RECEIVED",
            session_id=invocation_context.session.id,
            invocation_id=invocation_context.invocation_id,
            user_id_present=bool(invocation_context.user_id),
            user_message=_short(text),
            known_pii=known_pii_from_state(invocation_context.session.state),
        )
        return None

    async def after_run_callback(self, *, invocation_context):
        start = self._started.pop(f"run:{invocation_context.invocation_id}", None)
        log_event(
            logger,
            "REQUEST_COMPLETED",
            session_id=invocation_context.session.id,
            invocation_id=invocation_context.invocation_id,
            latency_ms=round((time.perf_counter() - start) * 1000) if start else None,
        )
        self._rationale = {
            k: v
            for k, v in self._rationale.items()
            if k[0] != invocation_context.invocation_id
        }

    async def on_run_error_callback(self, *, invocation_context, error):
        log_event(
            logger,
            "REQUEST_ERROR",
            severity=logging.ERROR,
            session_id=invocation_context.session.id,
            invocation_id=invocation_context.invocation_id,
            error_type=type(error).__name__,
            error=_short(str(error)),
        )

    # ---- agents ----------------------------------------------------------- #
    async def before_agent_callback(self, *, agent, callback_context):
        self._started[f"agent:{callback_context.invocation_id}:{agent.name}"] = (
            time.perf_counter()
        )
        log_event(logger, "AGENT_START", **_session_fields(callback_context))
        return None

    async def after_agent_callback(self, *, agent, callback_context):
        start = self._started.pop(
            f"agent:{callback_context.invocation_id}:{agent.name}", None
        )
        log_event(
            logger,
            "AGENT_END",
            **_session_fields(callback_context),
            latency_ms=round((time.perf_counter() - start) * 1000) if start else None,
        )
        return None

    async def on_agent_error_callback(self, *, agent, callback_context, error):
        log_event(
            logger,
            "AGENT_ERROR",
            severity=logging.ERROR,
            **_session_fields(callback_context),
            error_type=type(error).__name__,
            error=_short(str(error)),
        )

    # ---- model ------------------------------------------------------------ #
    async def before_model_callback(self, *, callback_context, llm_request):
        key = f"model:{callback_context.invocation_id}:{callback_context.agent_name}"
        self._started[key] = time.perf_counter()
        log_event(
            logger,
            "MODEL_CALL",
            **_session_fields(callback_context),
            model=llm_request.model,
            tools_available=len(getattr(llm_request, "tools_dict", {}) or {}),
        )
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        key = f"model:{callback_context.invocation_id}:{callback_context.agent_name}"
        start = self._started.pop(key, None)
        usage = llm_response.usage_metadata
        calls, text = [], []
        if llm_response.content and llm_response.content.parts:
            for part in llm_response.content.parts:
                if part.function_call:
                    calls.append(part.function_call.name)
                elif part.text and not getattr(part, "thought", False):
                    text.append(part.text)
        if calls and text:
            # The model's own words next to its tool call = its stated rationale.
            self._rationale[
                (callback_context.invocation_id, callback_context.agent_name)
            ] = " ".join(text)
        log_event(
            logger,
            "MODEL_RESPONSE",
            **_session_fields(callback_context),
            model_version=llm_response.model_version,
            latency_ms=round((time.perf_counter() - start) * 1000) if start else None,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            thinking_tokens=getattr(usage, "thoughts_token_count", None),
            finish_reason=str(llm_response.finish_reason)
            if llm_response.finish_reason
            else None,
            tool_calls_requested=calls,
        )
        return None

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        log_event(
            logger,
            "MODEL_ERROR",
            severity=logging.ERROR,
            **_session_fields(callback_context),
            model=llm_request.model,
            error_type=type(error).__name__,
            error=_short(str(error)),
        )
        return None

    # ---- tools: intent vs outcome ---------------------------------------- #
    def _action_id(self, tool_context) -> str:
        return (
            tool_context.function_call_id
            or f"{tool_context.invocation_id}:{id(tool_context)}"
        )

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        action_id = self._action_id(tool_context)
        self._started[f"tool:{action_id}"] = time.perf_counter()
        known = _known(tool_context)
        purpose = (
            (tool.description or "").strip().splitlines()[0] if tool.description else ""
        )
        rationale = self._rationale.get(
            (tool_context.invocation_id, tool_context.agent_name), ""
        )
        is_confirmation_reply = (
            getattr(tool_context, "tool_confirmation", None) is not None
        )
        span = trace.get_current_span()
        span.set_attribute("applypilot.action_id", action_id)
        span.set_attribute("applypilot.high_stakes", tool.name in HIGH_STAKES_TOOLS)
        log_event(
            logger,
            "AGENT_INTENT",
            f"{tool_context.agent_name} intends to call {tool.name}",
            **_session_fields(tool_context),
            action_id=action_id,
            tool=tool.name,
            tool_purpose=_short(purpose),
            arguments=get_scrubber().scrub(
                {k: _short(v) for k, v in tool_args.items()}, known
            ),
            model_rationale=_short(rationale) or None,
            high_stakes=tool.name in HIGH_STAKES_TOOLS,
            human_decision=(
                ("approved" if tool_context.tool_confirmation.confirmed else "rejected")
                if is_confirmation_reply
                else None
            ),
            known_pii=known,
        )
        return None

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        action_id = self._action_id(tool_context)
        start = self._started.pop(f"tool:{action_id}", None)
        status = (
            result.get("status", "success") if isinstance(result, dict) else "success"
        )
        error_code = result.get("error_code") if isinstance(result, dict) else None
        span = trace.get_current_span()
        span.set_attribute("applypilot.outcome.status", str(status))
        log_event(
            logger,
            "AGENT_OUTCOME",
            f"{tool.name} finished with status={status}",
            severity=logging.WARNING if status == "error" else logging.INFO,
            **_session_fields(tool_context),
            action_id=action_id,
            tool=tool.name,
            status=status,
            achieved_intent=status == "success",
            error_code=error_code,
            recovery_instruction=_short(result.get("recovery_instruction"))
            if isinstance(result, dict)
            else None,
            latency_ms=round((time.perf_counter() - start) * 1000) if start else None,
            result_summary=summarize_result(result),
            state_keys_changed=sorted(tool_context.actions.state_delta.keys())
            if getattr(tool_context.actions, "state_delta", None)
            else [],
            high_stakes=tool.name in HIGH_STAKES_TOOLS,
            known_pii=_known(tool_context),
        )
        return None

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        action_id = self._action_id(tool_context)
        start = self._started.pop(f"tool:{action_id}", None)
        log_event(
            logger,
            "AGENT_OUTCOME",
            f"{tool.name} raised {type(error).__name__}",
            severity=logging.ERROR,
            **_session_fields(tool_context),
            action_id=action_id,
            tool=tool.name,
            status="exception",
            achieved_intent=False,
            error_type=type(error).__name__,
            error=_short(str(error)),
            latency_ms=round((time.perf_counter() - start) * 1000) if start else None,
            known_pii=_known(tool_context),
        )
        return None
