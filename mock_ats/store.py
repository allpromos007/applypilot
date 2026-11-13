"""Storage for the Mock ATS.

`ApplicationStore` is the seam: locally we use SQLite + files on disk; in M7 a
Firestore + GCS implementation can be dropped in without touching the API.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from mock_ats.models import ApplicationRecord, ApplicationStatus, CandidateInfo


class DuplicateApplicationError(Exception):
    def __init__(self, existing_id: str):
        super().__init__(existing_id)
        self.existing_id = existing_id


class ApplicationStore(Protocol):
    def create(
        self,
        job_id: str,
        candidate: CandidateInfo,
        resume: tuple[str, bytes],
        cover_letter: tuple[str, bytes],
    ) -> ApplicationRecord: ...

    def get(self, application_id: str) -> ApplicationRecord | None: ...

    def list_for_email(self, email: str) -> list[ApplicationRecord]: ...


class SQLiteApplicationStore:
    """Thread-safe SQLite store; uploaded files saved under `<data_dir>/files/`."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.files_dir = self.data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            self.data_dir / "applications.db", check_same_thread=False
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS applications (
                application_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                email TEXT NOT NULL,
                record_json TEXT NOT NULL,
                UNIQUE(job_id, email)
            )"""
        )
        self._db.commit()

    def create(self, job_id, candidate, resume, cover_letter) -> ApplicationRecord:
        with self._lock:
            row = self._db.execute(
                "SELECT application_id FROM applications WHERE job_id=? AND email=?",
                (job_id, candidate.email),
            ).fetchone()
            if row:
                raise DuplicateApplicationError(row[0])
            app_id = f"app_{uuid.uuid4().hex[:12]}"
            folder = self.files_dir / app_id
            folder.mkdir(parents=True)
            (folder / "resume.docx").write_bytes(resume[1])
            (folder / "cover_letter.docx").write_bytes(cover_letter[1])
            record = ApplicationRecord(
                application_id=app_id,
                status=ApplicationStatus.RECEIVED,
                job_id=job_id,
                received_at=datetime.now(UTC),
                candidate=candidate,
                resume_filename=resume[0],
                cover_letter_filename=cover_letter[0],
            )
            self._db.execute(
                "INSERT INTO applications VALUES (?,?,?,?)",
                (app_id, job_id, candidate.email, record.model_dump_json()),
            )
            self._db.commit()
            return record

    def get(self, application_id: str) -> ApplicationRecord | None:
        row = self._db.execute(
            "SELECT record_json FROM applications WHERE application_id=?",
            (application_id,),
        ).fetchone()
        return ApplicationRecord.model_validate(json.loads(row[0])) if row else None

    def list_for_email(self, email: str) -> list[ApplicationRecord]:
        rows = self._db.execute(
            "SELECT record_json FROM applications WHERE email=? ORDER BY rowid",
            (email.lower(),),
        ).fetchall()
        return [ApplicationRecord.model_validate(json.loads(r[0])) for r in rows]
