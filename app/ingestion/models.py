"""Canonical intermediate representation produced by the cleaning pipeline.

Everything downstream (FHIR mapping, summarization, search) consumes these models,
never the raw files. Each patient and each clinical record carries its own audit
log describing every change the pipeline made to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Gender = Literal["male", "female", "other", "unknown"]
RecordCategory = Literal["lab", "imaging", "pathology", "cardiology", "clinical_note"]
EncounterClass = Literal["AMB", "IMP", "EMER"]
Interpretation = Literal["N", "H", "L", "HH", "LL", "A"]
AuditAction = Literal[
    "normalized",  # value re-formatted to the canonical form
    "filled",  # missing value supplied from another field/source
    "inferred",  # value derived heuristically (e.g. document type from text)
    "flagged",  # value kept/cleared but needs human review
    "dropped",  # whole item removed (empty, unusable)
    "deduplicated",  # duplicate removed / merged
    "linked",  # identity resolution linked two identifiers
]


class AuditEntry(BaseModel):
    """One change the pipeline made, kept alongside the record it applies to."""

    model_config = ConfigDict(frozen=True)

    field: str
    action: AuditAction
    before: str | None = None
    after: str | None = None
    reason: str
    source: str | None = None


# ---------------------------------------------------------------------------------------
# Raw items (output of the loaders, input to cleaning). Plain dataclasses on purpose:
# they are *untrusted* and every field is an optional string.
# ---------------------------------------------------------------------------------------
@dataclass
class RawPatient:
    source: str
    mrn: str | None = None
    name: Any = None  # dict {"given","family"} or "FAMILY, GIVEN" or "Given Family"
    dob: str | None = None
    gender: str | None = None
    phone: str | None = None


@dataclass
class RawDocument:
    source: str
    patient: RawPatient
    doc_id: str | None = None
    doc_type: str | None = None
    date: str | None = None
    author: str | None = None
    text: str | None = None
    encounter_id: str | None = None
    encounter_class: str | None = None
    encounter_start: str | None = None
    encounter_end: str | None = None


@dataclass
class RawLabRow:
    source: str
    patient: RawPatient
    accession: str | None = None
    collected: str | None = None
    panel: str | None = None
    test: str | None = None
    loinc: str | None = None
    result: str | None = None
    units: str | None = None
    ref_range: str | None = None
    flag: str | None = None


@dataclass
class RawBatch:
    patients: list[RawPatient] = field(default_factory=list)
    documents: list[RawDocument] = field(default_factory=list)
    lab_rows: list[RawLabRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def extend(self, other: "RawBatch") -> None:
        self.patients.extend(other.patients)
        self.documents.extend(other.documents)
        self.lab_rows.extend(other.lab_rows)
        self.errors.extend(other.errors)


# ---------------------------------------------------------------------------------------
# Clean, validated canonical models
# ---------------------------------------------------------------------------------------
class Patient(BaseModel):
    patient_id: str = Field(description="Stable internal id, 'pt-<canonical MRN>'")
    mrn: str = Field(pattern=r"^\d{7}$", description="Canonical 7-digit MRN")
    alternate_mrns: list[str] = Field(default_factory=list)
    given_name: str | None = None
    family_name: str | None = None
    birth_date: date | None = None
    gender: Gender = "unknown"
    phone: str | None = None
    sources: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    audit: list[AuditEntry] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.given_name, self.family_name) if p]
        return " ".join(parts) or "Unknown patient"


class Encounter(BaseModel):
    encounter_id: str
    patient_id: str
    class_code: EncounterClass = "AMB"
    start: datetime | None = None
    end: datetime | None = None
    synthesized: bool = Field(False, description="True when no source encounter existed")


class LabResult(BaseModel):
    test_name: str
    loinc: str | None = None
    value_raw: str
    value_numeric: float | None = None
    comparator: Literal["<", "<=", ">", ">="] | None = None
    unit: str | None = None
    reference_range: str | None = None
    interpretation: Interpretation | None = None


class ClinicalRecord(BaseModel):
    record_id: str
    patient_id: str
    encounter_id: str
    category: RecordCategory
    doc_type: str = Field(description="Canonical document type, e.g. 'Discharge Summary'")
    title: str
    record_date: datetime | None = None
    author: str | None = None
    text: str
    results: list[LabResult] = Field(default_factory=list)
    source: str
    content_hash: str
    audit: list[AuditEntry] = Field(default_factory=list)

    @property
    def fhir_resource_type(self) -> str:
        return "DocumentReference" if self.category == "clinical_note" else "DiagnosticReport"


class CleanDataset(BaseModel):
    patients: list[Patient]
    encounters: list[Encounter]
    records: list[ClinicalRecord]
    pipeline_audit: list[AuditEntry] = Field(
        default_factory=list, description="Dataset-level events (dropped rows, duplicate removals)"
    )
    stats: dict[str, int] = Field(default_factory=dict)

    def records_for(self, patient_id: str) -> list[ClinicalRecord]:
        return [r for r in self.records if r.patient_id == patient_id]

    def encounters_for(self, patient_id: str) -> list[Encounter]:
        return [e for e in self.encounters if e.patient_id == patient_id]
