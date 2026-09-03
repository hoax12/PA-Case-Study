"""Small SQLite repository for jobs, UI events, and reviewer corrections."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models import (
    CorrectionRequest,
    ExtractionResult,
    OcrBatchResult,
    ReviewerDecisionRequest,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ReviewRepository:
    """Jobs, events, corrections, and reviewer decisions."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    provider_mode TEXT NOT NULL,
                    session_id TEXT,
                    files_json TEXT NOT NULL,
                    ocr_json TEXT,
                    extraction_json TEXT,
                    adjudication_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_job_events_job_id
                ON job_events(job_id, id);

                CREATE TABLE IF NOT EXISTS corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    entity_id TEXT NOT NULL,
                    original_value TEXT,
                    corrected_value TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    verified INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS reviewer_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    criterion_id TEXT,
                    system_status TEXT,
                    reviewer_status TEXT,
                    final_outcome TEXT,
                    reason_code TEXT,
                    note TEXT,
                    reviewer TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_reviewer_decisions_job
                ON reviewer_decisions(job_id, id);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "adjudication_json" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN adjudication_json TEXT"
                )

    def create_job(
        self, job_id: str, provider_mode: str, files: list[dict[str, Any]]
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, status, provider_mode, files_json, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?)
                """,
                (job_id, provider_mode, json.dumps(files), now, now),
            )

    def set_session_id(self, job_id: str, session_id: str) -> None:
        self.update_job(job_id, session_id=session_id)

    def update_job(self, job_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "session_id",
            "ocr_json",
            "extraction_json",
            "adjudication_json",
            "error",
        }
        unexpected = set(values) - allowed
        if unexpected:
            raise ValueError(f"Unsupported job columns: {sorted(unexpected)}")
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        parameters = [values[key] for key in values]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",  # noqa: S608
                [*parameters, job_id],
            )

    def save_ocr(self, job_id: str, result: OcrBatchResult) -> None:
        self.update_job(job_id, ocr_json=result.model_dump_json())

    def save_extraction(self, job_id: str, result: ExtractionResult) -> None:
        self.update_job(job_id, extraction_json=result.model_dump_json())

    def save_adjudication(self, job_id: str, result: object) -> None:
        self.update_job(job_id, adjudication_json=result.model_dump_json())  # type: ignore[attr-defined]

    def add_reviewer_decision(
        self, job_id: str, decision: "ReviewerDecisionRequest"
    ) -> dict[str, Any]:
        """Record what the reviewer concluded.

        `final_outcome` is free text written by a person and may be a denial. The
        system's own Outcome type still has no such value; a denial exists in this
        table only because a licensed reviewer put it there.
        """

        if self.get_job(job_id) is None:
            raise KeyError("Job was not found")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO reviewer_decisions (
                    job_id, criterion_id, system_status, reviewer_status,
                    final_outcome, reason_code, note, reviewer, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    decision.criterion_id,
                    decision.system_status,
                    decision.reviewer_status,
                    decision.final_outcome,
                    decision.reason_code,
                    decision.note,
                    decision.reviewer,
                    utc_now(),
                ),
            )
        return self.get_job(job_id) or {}

    def append_event(
        self, job_id: str, event_type: str, stage: str, payload: dict[str, Any]
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO job_events (
                    job_id, event_type, stage, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    event_type,
                    stage,
                    json.dumps(payload, default=str),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_events_after(self, job_id: str, after_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, job_id, event_type, stage, payload_json, created_at
                FROM job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (job_id, after_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "event_type": row["event_type"],
                "stage": row["stage"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            decisions = connection.execute(
                """
                SELECT criterion_id, system_status, reviewer_status, final_outcome,
                       reason_code, note, reviewer, created_at
                FROM reviewer_decisions WHERE job_id = ? ORDER BY id ASC
                """,
                (job_id,),
            ).fetchall()
            corrections = connection.execute(
                """
                SELECT entity_id, original_value, corrected_value, reviewer,
                       verified, created_at
                FROM corrections
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (job_id,),
            ).fetchall()

        result = dict(row)
        result["files"] = json.loads(result.pop("files_json"))
        result["ocr"] = json.loads(result.pop("ocr_json")) if result["ocr_json"] else None
        result["extraction"] = (
            json.loads(result.pop("extraction_json"))
            if result["extraction_json"]
            else None
        )
        result["adjudication"] = (
            json.loads(result.pop("adjudication_json"))
            if result["adjudication_json"]
            else None
        )
        result["corrections"] = [dict(correction) for correction in corrections]
        result["reviewer_decisions"] = [dict(decision) for decision in decisions]
        self._overlay_corrections(result)
        return result

    def add_correction(
        self, job_id: str, request: CorrectionRequest
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None or not job.get("extraction"):
            raise KeyError("Job or extraction was not found")
        entity = next(
            (
                item
                for item in job["extraction"]["entities"]
                if item["entity_id"] == request.entity_id
            ),
            None,
        )
        if entity is None:
            raise KeyError("Entity was not found")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO corrections (
                    job_id, entity_id, original_value, corrected_value,
                    reviewer, verified, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request.entity_id,
                    entity.get("value"),
                    request.corrected_value,
                    request.reviewer,
                    int(request.verified),
                    utc_now(),
                ),
            )
        return self.get_job(job_id) or {}

    @staticmethod
    def _overlay_corrections(job: dict[str, Any]) -> None:
        extraction = job.get("extraction")
        if not extraction:
            return
        latest = {item["entity_id"]: item for item in job["corrections"]}
        for entity in extraction["entities"]:
            correction = latest.get(entity["entity_id"])
            if not correction:
                continue
            entity["original_value"] = entity.get("value")
            entity["value"] = correction["corrected_value"]
            entity["normalized_value"] = correction["corrected_value"]
            entity["verification_status"] = (
                "corrected" if correction["verified"] else "needs_review"
            )
            entity["reviewed_by"] = correction["reviewer"]
