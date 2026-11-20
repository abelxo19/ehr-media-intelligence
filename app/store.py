"""SQLite persistence for bundles, record metadata, validation results and the summary cache.

One file (``var/ehr.db``), standard library ``sqlite3`` only. JSON columns hold the
FHIR resources verbatim so downstream consumers get exactly what was validated.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    patient_id   TEXT PRIMARY KEY,
    mrn          TEXT NOT NULL,
    display_name TEXT NOT NULL,
    birth_date   TEXT,
    gender       TEXT,
    flags        TEXT NOT NULL DEFAULT '[]',
    data         TEXT NOT NULL            -- canonical Patient model (incl. audit log)
);
CREATE TABLE IF NOT EXISTS records (
    record_id     TEXT PRIMARY KEY,
    patient_id    TEXT NOT NULL REFERENCES patients(patient_id),
    fhir_id       TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    category      TEXT NOT NULL,
    doc_type      TEXT NOT NULL,
    title         TEXT NOT NULL,
    record_date   TEXT,
    text          TEXT NOT NULL,
    data          TEXT NOT NULL           -- canonical ClinicalRecord model (incl. audit log)
);
CREATE INDEX IF NOT EXISTS idx_records_patient ON records(patient_id);
CREATE TABLE IF NOT EXISTS bundles (
    patient_id   TEXT PRIMARY KEY REFERENCES patients(patient_id),
    bundle_id    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    valid        INTEGER NOT NULL,
    bundle       TEXT NOT NULL,
    validation   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summaries (
    cache_key    TEXT PRIMARY KEY,        -- patient_id + bundle content hash + prompt version + model
    patient_id   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    generator    TEXT NOT NULL,
    summary      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_patient ON summaries(patient_id);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- writes ------------------------------------------------------------------------------
    def replace_clinical_data(self, patients: list[dict], records: list[dict]) -> None:
        """Replace patients/records/bundles atomically (summaries are a cache and are kept)."""
        with self.connect() as conn:
            conn.execute("DELETE FROM bundles")
            conn.execute("DELETE FROM records")
            conn.execute("DELETE FROM patients")
            conn.executemany(
                "INSERT INTO patients VALUES (:patient_id, :mrn, :display_name, :birth_date, :gender, :flags, :data)",
                patients,
            )
            conn.executemany(
                "INSERT INTO records VALUES (:record_id, :patient_id, :fhir_id, :resource_type, :category, "
                ":doc_type, :title, :record_date, :text, :data)",
                records,
            )

    def save_bundle(self, patient_id: str, bundle: dict, content_hash: str, validation: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bundles VALUES (?, ?, ?, ?, ?, ?, ?)",
                (patient_id, bundle["id"], content_hash, int(validation["valid"]), json.dumps(bundle),
                 json.dumps(validation), _now()),
            )

    def set_meta(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    # -- summary cache -----------------------------------------------------------------------
    def get_summary(self, cache_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT summary FROM summaries WHERE cache_key = ?", (cache_key,)).fetchone()
        return json.loads(row["summary"]) if row else None

    def put_summary(self, cache_key: str, patient_id: str, content_hash: str, generator: str, summary: dict) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?, ?, ?)",
                         (cache_key, patient_id, content_hash, generator, json.dumps(summary), _now()))

    def latest_summary(self, patient_id: str, content_hash: str | None = None) -> dict | None:
        query = "SELECT summary FROM summaries WHERE patient_id = ?"
        params: list[Any] = [patient_id]
        if content_hash:
            query += " AND content_hash = ?"
            params.append(content_hash)
        with self.connect() as conn:
            row = conn.execute(query + " ORDER BY created_at DESC LIMIT 1", params).fetchone()
        return json.loads(row["summary"]) if row else None

    # -- reads -------------------------------------------------------------------------------
    def get_meta(self, key: str) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def list_patients(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT p.patient_id, p.mrn, p.display_name, p.birth_date, p.gender, p.flags, "
                "COUNT(r.record_id) AS record_count, MAX(r.record_date) AS last_record_date "
                "FROM patients p LEFT JOIN records r ON r.patient_id = p.patient_id "
                "GROUP BY p.patient_id ORDER BY p.display_name"
            ).fetchall()
        return [dict(r) | {"flags": json.loads(r["flags"])} for r in rows]

    def get_patient(self, patient_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def get_records(self, patient_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM records"
        params: tuple = ()
        if patient_id:
            query += " WHERE patient_id = ?"
            params = (patient_id,)
        with self.connect() as conn:
            rows = conn.execute(query + " ORDER BY record_date DESC", params).fetchall()
        return [dict(r) for r in rows]

    def get_bundle(self, patient_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM bundles WHERE patient_id = ?", (patient_id,)).fetchone()
        if not row:
            return None
        return {"bundle": json.loads(row["bundle"]), "content_hash": row["content_hash"], "valid": bool(row["valid"]),
                "validation": json.loads(row["validation"]), "updated_at": row["updated_at"]}

    def all_bundles(self) -> dict[str, dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT patient_id FROM bundles").fetchall()
        return {r["patient_id"]: self.get_bundle(r["patient_id"]) for r in rows}  # type: ignore[misc]

    def is_populated(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM bundles").fetchone()[0] > 0
