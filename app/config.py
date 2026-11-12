"""Central configuration for ApplyPilot.

Every tunable value is read from an environment variable with a safe default, so
the same code runs locally (values from `.env`) and on Agent Engine (values
injected at deploy time). Secrets are NOT read here; see `app/secrets.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class ModelConfig:
    """Strategic model routing (rubric 3.2).

    - FAST: high-volume, latency-sensitive work (parsing, triage, matching, scoring).
    - REASONING: quality-critical generation and judgement (rewriting, cover
      letters, truthfulness review).
    """

    fast: str = field(
        default_factory=lambda: os.getenv("APPLYPILOT_FAST_MODEL", "gemini-3.8-flash")
    )
    reasoning: str = field(
        default_factory=lambda: os.getenv(
            "APPLYPILOT_REASONING_MODEL", "gemini-3.1-pro-preview"
        )
    )


@dataclass(frozen=True)
class JobSearchConfig:
    """Where and how we look for jobs."""

    greenhouse_boards: list[str] = field(
        default_factory=lambda: _env_list("APPLYPILOT_GREENHOUSE_BOARDS", "")
    )
    lever_companies: list[str] = field(
        default_factory=lambda: _env_list("APPLYPILOT_LEVER_COMPANIES", "")
    )
    companies_file: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "APPLYPILOT_COMPANIES_FILE",
                str(PROJECT_ROOT / "config" / "companies.yaml"),
            )
        )
    )
    http_timeout_seconds: float = float(os.getenv("APPLYPILOT_HTTP_TIMEOUT", "15"))
    max_results: int = int(os.getenv("APPLYPILOT_MAX_JOB_RESULTS", "25"))


@dataclass(frozen=True)
class Settings:
    project_id: str = field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", "")
    )
    location: str = field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "global")
    )
    models: ModelConfig = field(default_factory=ModelConfig)
    jobs: JobSearchConfig = field(default_factory=JobSearchConfig)


def get_settings() -> Settings:
    """Return a fresh Settings object (cheap; reads env vars)."""
    return Settings()
