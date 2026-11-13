# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ApplyPilot agent graph.

    applypilot (Coordinator, FAST model)
    ├── profile_agent                (FAST)   resume + preferences
    ├── job_matcher_agent            (FAST)   Greenhouse/Lever search, job selection
    └── resume_tailoring_pipeline    SequentialAgent
        ├── ats_gap_analyst          (FAST)   BEFORE score + tailoring plan
        ├── document_writers         ParallelAgent
        │   ├── resume_rewriter      (REASONING) tailored ATS resume
        │   └── cover_letter_writer  (REASONING) matching cover letter
        ├── ats_rescorer             (FAST)   AFTER score + comparison
        └── truthfulness_judge       (FAST)   LLM-as-judge fabrication check
    + prepare_application_package, submit_job_application (HUMAN APPROVAL),
      get_application_history                 -> Mock ATS service

Specialists are exposed to the coordinator as AgentTools, so the coordinator
stays in control of the conversation and receives each specialist's result.
Agents share large artefacts (resume, JD, drafts) through session state.
"""

from google.adk.agents import Agent, ParallelAgent, SequentialAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.adk.tools.load_memory_tool import load_memory_tool
from google.adk.tools.preload_memory_tool import preload_memory_tool
from google.genai import types

from app import prompts
from app.config import get_settings
from app.context_management import ContextBudgetPlugin, build_compaction_config
from app.guardrails import SafetyGuardrailPlugin, prepare_truthfulness_evidence
from app.memory import MemoryConsolidationPlugin
from app.observability.plugin import ObservabilityPlugin
from app.tools.schemas import TruthfulnessReview
from app.tools.session_tools import (
    extract_requirements_for_target_job,
    find_jobs_matching_saved_preferences,
    get_application_history,
    get_tailored_application_documents,
    load_candidate_resume,
    prepare_application_package,
    save_candidate_preferences,
    score_resume_for_target_job,
    select_target_job,
    submit_job_application,
    use_pasted_job_description,
)

settings = get_settings()

# --------------------------------------------------------------------------- #
# Strategic model routing (rubric 3.2)
# FAST      -> conversation, routing, tool calling, analysis   (low latency/cost)
# REASONING -> long-form, quality-critical writing             (best quality)
# --------------------------------------------------------------------------- #
_RETRY = types.HttpRetryOptions(attempts=3)
FAST_MODEL = Gemini(model=settings.models.fast, retry_options=_RETRY)
REASONING_MODEL = Gemini(model=settings.models.reasoning, retry_options=_RETRY)

# Gemini safety settings: first guardrail layer on every model call (rubric 3.3).
_SAFETY = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE)
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]
_PRECISE = types.GenerateContentConfig(temperature=0.1, safety_settings=_SAFETY)
_CREATIVE = types.GenerateContentConfig(temperature=0.4, safety_settings=_SAFETY)
_JUDGE = types.GenerateContentConfig(temperature=0.0, safety_settings=_SAFETY)

# --------------------------------------------------------------------------- #
# Specialists
# --------------------------------------------------------------------------- #
profile_agent = Agent(
    name="profile_agent",
    description="Loads the candidate's resume file and saves their job-search preferences.",
    model=FAST_MODEL,
    instruction=prompts.PROFILE_AGENT_INSTRUCTION,
    tools=[load_candidate_resume, save_candidate_preferences],
    generate_content_config=_PRECISE,
)

job_matcher_agent = Agent(
    name="job_matcher_agent",
    description=(
        "Finds open jobs on Greenhouse/Lever matching the saved preferences, selects "
        "the target job by job_id, or uses a job description pasted by the candidate."
    ),
    model=FAST_MODEL,
    instruction=prompts.JOB_MATCHER_INSTRUCTION,
    tools=[
        find_jobs_matching_saved_preferences,
        select_target_job,
        use_pasted_job_description,
    ],
    generate_content_config=_PRECISE,
)

ats_gap_analyst = Agent(
    name="ats_gap_analyst",
    description="Scores the original resume and writes a truthful tailoring plan.",
    model=FAST_MODEL,
    instruction=prompts.GAP_ANALYST_INSTRUCTION,
    tools=[score_resume_for_target_job, extract_requirements_for_target_job],
    output_key="tailoring_plan",
    generate_content_config=_PRECISE,
)

resume_rewriter = Agent(
    name="resume_rewriter",
    description="Writes the tailored, ATS-friendly resume.",
    model=REASONING_MODEL,
    instruction=prompts.RESUME_REWRITER_INSTRUCTION,
    include_contents="none",  # everything it needs is injected from state
    output_key="tailored_resume",
    generate_content_config=_CREATIVE,
)

cover_letter_writer = Agent(
    name="cover_letter_writer",
    description="Writes a cover letter aligned with the job and the candidate's real experience.",
    model=REASONING_MODEL,
    instruction=prompts.COVER_LETTER_INSTRUCTION,
    include_contents="none",
    output_key="cover_letter",
    generate_content_config=_CREATIVE,
)

# Resume and cover letter are independent given the plan, so write them in parallel.
document_writers = ParallelAgent(
    name="document_writers",
    description="Writes the tailored resume and cover letter concurrently.",
    sub_agents=[resume_rewriter, cover_letter_writer],
)

ats_rescorer = Agent(
    name="ats_rescorer",
    description="Scores the tailored resume and reports the before/after change.",
    model=FAST_MODEL,
    instruction=prompts.ATS_RESCORER_INSTRUCTION,
    tools=[score_resume_for_target_job],
    output_key="ats_comparison",
    generate_content_config=_PRECISE,
)

# LLM-as-judge self-evaluation guardrail (rubric 3.3). Structured output, so the
# apply step can gate on verdict == "PASS" deterministically.
truthfulness_judge = Agent(
    name="truthfulness_judge",
    description="Independently checks the tailored documents for fabricated claims.",
    model=FAST_MODEL,
    instruction=prompts.TRUTHFULNESS_JUDGE_INSTRUCTION,
    include_contents="none",
    before_agent_callback=prepare_truthfulness_evidence,
    output_schema=TruthfulnessReview,
    output_key="truthfulness_review",
    generate_content_config=_JUDGE,
)

resume_tailoring_pipeline = SequentialAgent(
    name="resume_tailoring_pipeline",
    description=(
        "For the selected target job: scores the original resume (BEFORE), plans "
        "truthful changes, writes the tailored resume and cover letter in parallel, "
        "re-scores (AFTER), then a judge checks for fabricated claims. Requires a "
        "loaded resume and a selected job."
    ),
    sub_agents=[ats_gap_analyst, document_writers, ats_rescorer, truthfulness_judge],
)

# --------------------------------------------------------------------------- #
# Coordinator
# --------------------------------------------------------------------------- #
root_agent = Agent(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml:
    # telemetry reports this name as gen_ai.agent.name, and agents-cli reports the
    # manifest's value. Renaming the agent only here makes the two disagree, and
    # anything selecting traces by name stops finding this agent's.
    name="applypilot",
    description="Career assistant that tailors resumes and cover letters to jobs.",
    model=FAST_MODEL,
    instruction=prompts.COORDINATOR_INSTRUCTION,
    tools=[
        AgentTool(agent=profile_agent),
        AgentTool(agent=job_matcher_agent),
        AgentTool(agent=resume_tailoring_pipeline),
        get_tailored_application_documents,
        # Applying lives on the coordinator, not in an AgentTool specialist:
        # submit_job_application pauses for human approval, and AgentTool
        # discards its nested session, so it could never resume after approval.
        prepare_application_package,
        submit_job_application,
        get_application_history,
        # Long-term memory (rubric 2.3): relevant memories are injected
        # automatically each turn; load_memory lets the agent search on demand.
        preload_memory_tool,
        load_memory_tool,
    ],
    generate_content_config=_PRECISE,
)

app = App(
    root_agent=root_agent,
    name="app",
    # Structured, PII-scrubbed AGENT_INTENT / AGENT_OUTCOME logging for every
    # agent, model call and tool, including specialists run via AgentTool.
    plugins=[
        SafetyGuardrailPlugin(),  # Model Armor + injection screening (3.3)
        ContextBudgetPlugin(),  # sliding-window trim of old tool output (2.2)
        ObservabilityPlugin(),  # intent/outcome logs (4.1, 4.2)
        MemoryConsolidationPlugin(),  # background, PII-scrubbed memory (2.4)
    ],
    # Summarise older turns with the fast model (2.2).
    events_compaction_config=build_compaction_config(FAST_MODEL),
)
