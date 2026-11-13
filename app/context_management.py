"""Context-window management (rubric 2.2).

Two complementary mechanisms:

1. ADK events compaction (`build_compaction_config`). After every few user
   turns, or once a prompt passes a token threshold, ADK asks a FAST model to
   summarise older conversation events. The summary replaces them in future
   prompts, while the full history stays in the session store.

2. `ContextBudgetPlugin`, a before_model_callback sliding window. Tool outputs
   such as job lists and parsed resumes are large and mostly matter for the turn
   they appear in. When the estimated prompt size exceeds the budget, tool
   responses older than the most recent N messages are replaced by a short
   stub. Key facts are already saved in session state and re-injected via
   instruction placeholders, so nothing important is lost. The session itself
   is never modified; only the outgoing request is trimmed.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from app.observability.logging import get_logger, log_event

logger = get_logger(__name__)

TOKEN_BUDGET = int(os.getenv("APPLYPILOT_CONTEXT_TOKEN_BUDGET", "24000"))
KEEP_RECENT_CONTENTS = int(os.getenv("APPLYPILOT_CONTEXT_KEEP_RECENT", "6"))
LARGE_TOOL_OUTPUT_CHARS = 1500
TRIM_NOTE = (
    "[Older tool output trimmed to save context. The important results are in "
    "session state and the current instructions; call the tool again if you need details.]"
)


def build_compaction_config(summarizer_model: Any) -> EventsCompactionConfig:
    """Sliding-window + token-threshold summarisation of older events."""
    return EventsCompactionConfig(
        summarizer=LlmEventSummarizer(llm=summarizer_model),
        compaction_interval=int(os.getenv("APPLYPILOT_COMPACTION_INTERVAL", "5")),
        overlap_size=1,
        token_threshold=int(
            os.getenv("APPLYPILOT_COMPACTION_TOKEN_THRESHOLD", "60000")
        ),
        event_retention_size=10,
    )


def estimate_tokens(contents: list[types.Content]) -> int:
    """Cheap estimate (~4 characters per token) without calling the API."""
    chars = 0
    for content in contents:
        for part in content.parts or []:
            if part.text:
                chars += len(part.text)
            elif part.function_response:
                chars += len(
                    json.dumps(part.function_response.response or {}, default=str)
                )
            elif part.function_call:
                chars += len(json.dumps(part.function_call.args or {}, default=str))
    return chars // 4


def _trim_part(part: types.Part) -> tuple[types.Part, bool]:
    fr = part.function_response
    if not fr or not fr.response:
        return part, False
    if len(json.dumps(fr.response, default=str)) <= LARGE_TOOL_OUTPUT_CHARS:
        return part, False
    stub = {"status": fr.response.get("status", "unknown"), "note": TRIM_NOTE}
    # New objects: the original Part belongs to a session event and must not change.
    return (
        types.Part(
            function_response=types.FunctionResponse(
                id=fr.id, name=fr.name, response=stub
            )
        ),
        True,
    )


def trim_old_tool_outputs(
    contents: list[types.Content], keep_recent: int = KEEP_RECENT_CONTENTS
) -> tuple[list[types.Content], int]:
    """Return (new_contents, parts_trimmed). Recent contents are left untouched."""
    cutoff = max(0, len(contents) - keep_recent)
    trimmed = 0
    out: list[types.Content] = []
    for i, content in enumerate(contents):
        if i >= cutoff or not content.parts:
            out.append(content)
            continue
        new_parts = []
        changed = False
        for part in content.parts:
            new_part, did = _trim_part(part)
            new_parts.append(new_part)
            changed |= did
            trimmed += did
        out.append(
            types.Content(role=content.role, parts=new_parts) if changed else content
        )
    return out, trimmed


class ContextBudgetPlugin(BasePlugin):
    def __init__(self, token_budget: int = TOKEN_BUDGET) -> None:
        super().__init__(name="applypilot_context_budget")
        self.token_budget = token_budget

    async def before_model_callback(self, *, callback_context, llm_request):
        before = estimate_tokens(llm_request.contents)
        if before <= self.token_budget:
            return None
        llm_request.contents, trimmed = trim_old_tool_outputs(llm_request.contents)
        log_event(
            logger,
            "CONTEXT_TRIMMED",
            agent_name=callback_context.agent_name,
            estimated_tokens_before=before,
            estimated_tokens_after=estimate_tokens(llm_request.contents),
            tool_outputs_trimmed=trimmed,
            token_budget=self.token_budget,
        )
        return None
