"""Asynchronous, PII-safe long-term memory consolidation (rubrics 2.3, 2.4, 4.4).

After each user turn of the coordinator, if something worth remembering
changed (preferences, target job, ATS result, an application), we schedule a
BACKGROUND task with `asyncio.create_task`. The user's response is never held
up by it. The task:

  1. builds a compact "career profile" summary from session state,
  2. copies the conversation, keeping only text and dropping tool payloads
     (resumes, job descriptions),
  3. redacts PII from both with the PIIScrubber (known contact values plus
     Cloud DLP/regex),
  4. hands the scrubbed session to the memory service:
       - Vertex AI Memory Bank (Agent Engine) runs LLM fact extraction and
         consolidation on its side, which is slow, hence background;
       - SQLite memory (local) indexes it for full-text search.

In a later session, `preload_memory` / `load_memory` on the coordinator bring
these memories back, so a returning candidate doesn't repeat their preferences.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from google.adk.events import Event
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.sessions import Session
from google.genai import types

from app.observability.logging import get_logger, log_event
from app.observability.pii import get_scrubber, known_pii_from_state

logger = get_logger(__name__)

COORDINATOR_NAME = "applypilot"
_MEMORABLE_KEYS = (
    "candidate_preferences",
    "target_job_title",
    "target_company",
    "ats_score_after",
    "user:application_history",
)
_pending: set[asyncio.Task] = set()


def build_profile_summary(state: Any) -> str:
    """Plain-text facts about the candidate's job search, no contact details."""
    lines = ["CANDIDATE JOB-SEARCH PROFILE (from previous sessions)"]
    prefs = state.get("candidate_preferences") or {}
    if prefs:
        lines.append(f"Target roles: {', '.join(prefs.get('target_titles', []))}")
        if prefs.get("locations"):
            lines.append(f"Locations: {', '.join(prefs['locations'])}")
        lines.append(f"Workplace type: {prefs.get('workplace_type', 'any')}")
        if prefs.get("required_keywords"):
            lines.append(f"Must-have skills: {', '.join(prefs['required_keywords'])}")
        if prefs.get("excluded_keywords"):
            lines.append(f"Deal-breakers: {', '.join(prefs['excluded_keywords'])}")
        if prefs.get("min_salary_usd"):
            lines.append(f"Minimum salary (USD): {prefs['min_salary_usd']}")
    if state.get("resume_file"):
        lines.append(f"Resume file used: {state['resume_file']}")
    if state.get("target_job_title"):
        lines.append(
            f"Most recent target job: {state['target_job_title']} at "
            f"{state.get('target_company', '')}"
        )
    before = (state.get("ats_score_before") or {}).get("overall_score")
    after = (state.get("ats_score_after") or {}).get("overall_score")
    if before is not None and after is not None:
        lines.append(
            f"ATS score for that job: {before} before tailoring, {after} after"
        )
    for app in state.get("user:application_history") or []:
        lines.append(
            f"Applied: {app.get('target')} (job {app.get('job_id')}) on "
            f"{app.get('submitted_at')}, application {app.get('application_id')}"
        )
    return "\n".join(lines)


def _fingerprint(state: Any) -> str:
    snapshot = {k: state.get(k) for k in _MEMORABLE_KEYS}
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, default=str).encode()
    ).hexdigest()


def scrubbed_session_for_memory(session: Session) -> Session:
    """Copy of the session containing only scrubbed text plus a profile summary."""
    scrubber = get_scrubber()
    known = known_pii_from_state(session.state)
    events: list[Event] = []
    for event in session.events:
        if not event.content or not event.content.parts:
            continue
        texts = [
            p.text
            for p in event.content.parts
            if p.text and not getattr(p, "thought", False)
        ]
        if not texts:
            continue  # tool calls/responses carry resumes and JDs: never stored
        events.append(
            event.model_copy(
                update={
                    "content": types.Content(
                        role=event.content.role,
                        parts=[
                            types.Part(text=scrubber.scrub_text(" ".join(texts), known))
                        ],
                    )
                }
            )
        )
    events.append(
        Event(
            author=COORDINATOR_NAME,
            invocation_id="memory-profile",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=scrubber.scrub_text(
                            build_profile_summary(session.state), known
                        )
                    )
                ],
            ),
        )
    )
    return session.model_copy(update={"events": events, "state": {}})


async def consolidate_memory(memory_service: Any, session: Session) -> None:
    """The background job. Never raises: memory is best-effort."""
    start = time.perf_counter()
    try:
        scrubbed = scrubbed_session_for_memory(session)
        await memory_service.add_session_to_memory(scrubbed)
        log_event(
            logger,
            "MEMORY_CONSOLIDATED",
            session_id=session.id,
            memory_service=type(memory_service).__name__,
            events_stored=len(scrubbed.events),
            latency_ms=round((time.perf_counter() - start) * 1000),
        )
    except Exception as exc:
        log_event(
            logger,
            "MEMORY_CONSOLIDATION_FAILED",
            session_id=session.id,
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )


async def wait_for_pending_memory_tasks(timeout: float = 30.0) -> None:
    """For tests and graceful shutdown: let background consolidations finish."""
    if _pending:
        await asyncio.wait(set(_pending), timeout=timeout)


class MemoryConsolidationPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="applypilot_memory")
        self._last_fingerprint: dict[str, str] = {}

    async def after_run_callback(self, *, invocation_context) -> None:
        # Only the coordinator's own runs, not specialists run via AgentTool.
        if getattr(invocation_context.agent, "name", None) != COORDINATOR_NAME:
            return
        memory_service = invocation_context.memory_service
        if memory_service is None:
            return
        session = invocation_context.session
        if not any(session.state.get(k) for k in _MEMORABLE_KEYS):
            return  # e.g. a greeting: nothing worth remembering yet
        fingerprint = _fingerprint(session.state)
        if self._last_fingerprint.get(session.id) == fingerprint:
            return  # nothing new worth remembering
        self._last_fingerprint[session.id] = fingerprint
        snapshot = session.model_copy(deep=True)  # don't race with the next turn
        task = asyncio.create_task(consolidate_memory(memory_service, snapshot))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        log_event(logger, "MEMORY_CONSOLIDATION_SCHEDULED", session_id=session.id)
