"""Shared fixtures for unit tests: hermetic (no Secret Manager, no network)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_secrets(monkeypatch):
    from app.secrets import clear_secret_cache

    monkeypatch.setenv("APPLYPILOT_DISABLE_SECRET_MANAGER", "true")
    monkeypatch.delenv("MOCK_ATS_API_KEY", raising=False)
    clear_secret_cache()
    yield
    clear_secret_cache()
