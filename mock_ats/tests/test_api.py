"""API tests for the Mock ATS (no network: FastAPI TestClient)."""

from __future__ import annotations

import io
import json

import docx
import pytest
from fastapi.testclient import TestClient

from mock_ats.main import create_app
from mock_ats.store import SQLiteApplicationStore

CANDIDATE = {
    "full_name": "Maya Lin",
    "email": "Maya.Lin@example.com",
    "phone": "555-0100",
}


def _docx_bytes(text: str = "Maya Lin\nEXPERIENCE\n- Built things.") -> bytes:
    document = docx.Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(SQLiteApplicationStore(tmp_path)))


def _post(
    client, job_id="greenhouse:acme:1", candidate=CANDIDATE, resume=None, letter=None
):
    files = {
        "resume": ("resume.docx", resume if resume is not None else _docx_bytes()),
        "cover_letter": ("cover.docx", letter if letter is not None else _docx_bytes()),
    }
    return client.post(
        "/applications",
        data={"job_id": job_id, "candidate": json.dumps(candidate)},
        files=files,
    )


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_accepts_valid_application_and_reads_it_back(client):
    resp = _post(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "RECEIVED" and body["application_id"].startswith("app_")
    record = client.get(f"/applications/{body['application_id']}").json()
    assert record["candidate"]["email"] == "maya.lin@example.com"  # normalised
    listed = client.get(
        "/applications", params={"email": "maya.lin@example.com"}
    ).json()
    assert [r["application_id"] for r in listed] == [body["application_id"]]


def test_duplicate_returns_409_with_existing_id(client):
    first = _post(client).json()
    resp = _post(client, candidate={**CANDIDATE, "email": "maya.lin@EXAMPLE.com"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "DUPLICATE_APPLICATION"
    assert body["details"]["existing_application_id"] == first["application_id"]


def test_missing_file_returns_422(client):
    resp = client.post(
        "/applications",
        data={"job_id": "greenhouse:acme:1", "candidate": json.dumps(CANDIDATE)},
        files={"resume": ("resume.docx", _docx_bytes())},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_FAILED"
    assert any("cover_letter" in p for p in resp.json()["details"]["problems"])


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"resume": b"not a docx"}, "not a valid Word"),
        ({"resume": b""}, "empty"),
        ({"letter": _docx_bytes("Call me at [PHONE]")}, "unresolved placeholders"),
        ({"candidate": {**CANDIDATE, "email": "[EMAIL]"}}, "candidate.email"),
        ({"candidate": {**CANDIDATE, "phone": "[PHONE]"}}, "placeholder"),
    ],
)
def test_invalid_inputs_return_422_with_problems(client, kwargs, expected):
    resp = _post(client, **kwargs)
    assert resp.status_code == 422
    assert any(expected in p for p in resp.json()["details"]["problems"])


def test_unknown_application_returns_404(client):
    resp = client.get("/applications/app_nope")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"
