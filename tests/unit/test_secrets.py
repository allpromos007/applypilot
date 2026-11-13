"""Tests for secret management (rubric 5.3)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import secrets
from app.observability.pii import _PATTERNS
from app.tools.application_tools import MockATSAdapter
from app.tools.document_tools import render_ats_docx
from mock_ats.main import create_app
from mock_ats.store import SQLiteApplicationStore

ROOT = Path(__file__).resolve().parents[2]


def test_secret_manager_is_preferred(monkeypatch):
    monkeypatch.setenv("MOCK_ATS_API_KEY", "from-env")
    monkeypatch.setattr(secrets, "_from_secret_manager", lambda sid, ver: "from-sm")
    assert secrets.get_secret("mock-ats-api-key") == "from-sm"


def test_env_fallback_for_local_dev(monkeypatch):
    monkeypatch.setenv("MOCK_ATS_API_KEY", "from-env")
    monkeypatch.setattr(secrets, "_from_secret_manager", lambda sid, ver: None)
    assert secrets.get_secret("mock-ats-api-key") == "from-env"


def test_secret_manager_errors_fall_back(monkeypatch):
    def boom(sid, ver):
        raise RuntimeError("network down")

    monkeypatch.setenv("MOCK_ATS_API_KEY", "from-env")
    monkeypatch.setattr(secrets, "_from_secret_manager", boom)
    assert secrets.get_secret("mock-ats-api-key") == "from-env"


def test_unknown_secret_is_rejected():
    with pytest.raises(KeyError):
        secrets.get_secret("some-random-secret")


def test_secret_value_never_logged(monkeypatch, capsys):
    monkeypatch.setattr(
        secrets, "_from_secret_manager", lambda sid, ver: "s3cr3t-value-123"
    )
    calls = []
    monkeypatch.setattr(
        secrets, "log_event", lambda *a, **k: calls.append(json.dumps(k))
    )
    secrets.get_secret("mock-ats-api-key")
    assert calls and all("s3cr3t-value-123" not in c for c in calls)
    assert '"source": "secret_manager"' in calls[-1]


# --------------------------------------------------------------------------- #
# The key is actually enforced end to end
# --------------------------------------------------------------------------- #
@pytest.fixture
def secured_ats(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_ATS_API_KEY", "test-key-not-a-secret")
    return TestClient(create_app(SQLiteApplicationStore(tmp_path)))


def test_mock_ats_rejects_missing_key(secured_ats):
    assert secured_ats.get("/healthz").status_code == 200  # health stays open
    resp = secured_ats.get("/applications", params={"email": "a@b.co"})
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHORIZED"
    ok = secured_ats.get(
        "/applications",
        params={"email": "a@b.co"},
        headers={"X-API-Key": "test-key-not-a-secret"},
    )
    assert ok.status_code == 200


def test_adapter_sends_key_and_maps_401(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets, "_from_secret_manager", lambda sid, ver: "k-123")
    seen = SimpleNamespace(key=None)

    def handler(request):
        seen.key = request.headers.get("x-api-key")
        return httpx.Response(401, json={"error_code": "UNAUTHORIZED"})

    doc = render_ats_docx("Name\nEXPERIENCE\n- Built.", {}, tmp_path / "r.docx")
    result = MockATSAdapter("http://ats.test", httpx.MockTransport(handler)).submit(
        "j1", {}, doc, doc
    )
    assert seen.key == "k-123"
    assert result["status"] == "error" and "credentials" in result["message"]


# --------------------------------------------------------------------------- #
# No hardcoded secrets anywhere in the repository
# --------------------------------------------------------------------------- #
_SECRET_LABELS = {"API_KEY", "TOKEN"}
_EXTRA = [
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"\"private_key\"\s*:\s*\"-----BEGIN"),  # service-account JSON
    re.compile(
        r"(?i)(?:api[_-]?key|secret|password)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]"
    ),
]


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [
        ROOT / f for f in out.splitlines() if not f.endswith((".pdf", ".lock", ".docx"))
    ]


def test_no_hardcoded_secrets_in_repo():
    patterns = [p for label, p in _PATTERNS if label in _SECRET_LABELS] + _EXTRA
    offenders = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for pattern in patterns:
            if m := pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {m.group(0)[:20]}...")
    assert not offenders, "Possible hardcoded secrets:\n" + "\n".join(offenders)


def test_env_file_is_gitignored():
    tracked = {p.name for p in _tracked_files()}
    assert ".env" not in tracked
    assert ".env.example" in tracked
