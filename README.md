# applypilot

Simple ReAct agent
Agent generated with `agents-cli` version `1.14.0`

## Project Structure

```
applypilot/
├── app/         # Core agent code
│   ├── agent.py               # Main agent logic
│   ├── fast_api_app.py        # FastAPI Backend server
│   └── app_utils/             # App utilities and helpers
├── .github/                   # CI/CD pipeline configurations for GitHub Actions
├── deployment/                # Infrastructure and deployment scripts
├── tests/                     # Unit, integration, and load tests
├── GEMINI.md                  # AI-assisted development guide
└── pyproject.toml             # Project dependencies
```

> 💡 **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)
- **Terraform**: For infrastructure deployment - [Install](https://developer.hashicorp.com/terraform/downloads)


## Quick Start

Install `agents-cli` and its skills if not already installed:

```bash
uvx google-agents-cli setup
```

Install required packages:

```bash
agents-cli install
```

Test the agent with a local web server:

```bash
agents-cli playground
```

You can also use features from the [ADK](https://adk.dev/) CLI with `uv run adk`.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv                                                         |
| `agents-cli playground` | Launch local development environment                                                  |
| `agents-cli lint`    | Run code quality checks                                                               |
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests                                                        |
| `agents-cli deploy`  | Deploy agent to Agent Runtime                                                                |
| `agents-cli publish gemini` | Register deployed agent with Gemini                                    || [A2A Inspector](https://github.com/a2aproject/a2a-inspector) | Launch A2A Protocol Inspector                                                        |
| `agents-cli infra single-project` | Set up single-project infrastructure using Terraform                              |

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit your agent logic in `app/agent.py` and test with `agents-cli playground` - it auto-reloads on save.

## Deployment

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```
To set up your production infrastructure, run `agents-cli infra cicd`.

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.

## A2A Inspector

This agent supports the [A2A Protocol](https://a2a-protocol.org/). Use the [A2A Inspector](https://github.com/a2aproject/a2a-inspector) to test interoperability.
See the [A2A Inspector docs](https://github.com/a2aproject/a2a-inspector) for details.

## Mock ATS (application endpoint)

`mock_ats/` is a small FastAPI service that stands in for an employer's Applicant Tracking System, so `submit_job_application` has a real endpoint to call.

| Response | When |
|---|---|
| `201` `{application_id, status: RECEIVED}` | Application accepted |
| `409` `DUPLICATE_APPLICATION` | Same candidate email has already applied to this job |
| `422` `VALIDATION_FAILED` | Missing or invalid fields/files, a non-.docx upload, or unresolved `[EMAIL]`-style placeholders |

```bash
uv run uvicorn mock_ats.main:app --port 8081       # start the Mock ATS locally
uv run pytest mock_ats/tests                        # API tests
```

**Human approval (rubric 3.4):** `submit_job_application` always pauses with ADK's tool-confirmation request (`adk_request_confirmation`). Nothing is sent until the candidate approves in the UI. Each approve or reject decision is recorded in session state (`application_decisions`). The tool sits on the coordinator rather than inside an `AgentTool` specialist, because `AgentTool` discards its nested session and so cannot resume after an approval pause.
