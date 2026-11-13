"""Secure secret access (rubric 5.3).

Secrets are NEVER hardcoded or committed. Resolution order for a secret id:

1. Google Cloud Secret Manager: `projects/$GOOGLE_CLOUD_PROJECT/secrets/<id>`
   (version "latest" unless pinned). This is the path used on Agent Engine; the
   agent's service account only has `roles/secretmanager.secretAccessor` on
   the specific secrets it needs (Terraform, M7).
2. An environment variable named after the secret (e.g. `mock-ats-api-key` ->
   `MOCK_ATS_API_KEY`), for local development only. It is set in a git-ignored
   `.env`, never in code; `.env.example` lists the secret ids, not values.

Values are cached in memory, and only the *source* is ever logged, never the value.
"""

from __future__ import annotations

import os
import threading

from app.observability.logging import get_logger, log_event

logger = get_logger(__name__)

# Every secret the application uses. Anything else is a bug.
KNOWN_SECRETS = {
    "mock-ats-api-key": "Shared key the agent sends to the Mock ATS (X-API-Key).",
}

_cache: dict[str, str | None] = {}
_lock = threading.Lock()


def _env_name(secret_id: str) -> str:
    return secret_id.upper().replace("-", "_")


def _from_secret_manager(secret_id: str, version: str) -> str | None:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project or os.getenv("APPLYPILOT_DISABLE_SECRET_MANAGER", "").lower() in (
        "1",
        "true",
    ):
        return None
    from google.api_core import exceptions as gexc
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_id}/versions/{version}"
    try:
        response = client.access_secret_version(request={"name": name}, timeout=5.0)
    except (gexc.NotFound, gexc.PermissionDenied) as exc:
        log_event(
            logger,
            "SECRET_UNAVAILABLE",
            secret_id=secret_id,
            source="secret_manager",
            reason=type(exc).__name__,
        )
        return None
    return response.payload.data.decode("utf-8").strip()


def get_secret(secret_id: str, version: str = "latest") -> str | None:
    """Return the secret value, or None if it is not configured anywhere."""
    if secret_id not in KNOWN_SECRETS:
        raise KeyError(f"Unknown secret '{secret_id}'. Add it to KNOWN_SECRETS first.")
    with _lock:
        if secret_id in _cache:
            return _cache[secret_id]
        source, value = "none", None
        try:
            value = _from_secret_manager(secret_id, version)
            source = "secret_manager" if value else source
        except Exception as exc:  # network/auth problems: fall back, but log it
            log_event(
                logger,
                "SECRET_MANAGER_ERROR",
                secret_id=secret_id,
                error_type=type(exc).__name__,
            )
        if value is None and (env_value := os.getenv(_env_name(secret_id))):
            source, value = "environment", env_value
        log_event(logger, "SECRET_RESOLVED", secret_id=secret_id, source=source)
        _cache[secret_id] = value
        return value


def clear_secret_cache() -> None:
    """For tests and secret rotation."""
    with _lock:
        _cache.clear()
