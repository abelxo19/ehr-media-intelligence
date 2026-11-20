"""Ingestion pipeline: raw files -> :class:`CleanDataset`.

Steps
1. Load every supported file (JSON, CSV, TXT).
2. Resolve patient identity across sources (MRN first, then name + DOB), merging
   duplicates and recording conflicting identifiers.
3. Build clean clinical records (documents + lab reports grouped by accession),
   de-duplicate them and link each one to an encounter.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from app.ingestion import cleaning as c
from app.ingestion.loaders import load_directory
from app.ingestion.models import (
    AuditEntry,
    CleanDataset,
    ClinicalRecord,
    Encounter,
    LabResult,
    Patient,
    RawBatch,
    RawDocument,
    RawLabRow,
    RawPatient,
)

NAME_MATCH_THRESHOLD = 0.75


class PatientRegistry:
    """Resolves raw patient mentions to canonical patients.

    Matching rules (deliberately conservative - in a real EHR, merges go to HIM review):
    * same canonical MRN -> same patient. If the name differs slightly it is recorded
      as a name variant; if the DOB differs the patient is flagged ``identifier_conflict``.
    * no MRN match but same family name + similar given name + same DOB -> same patient,
      and the extra MRN is kept as an *alternate* identifier and flagged
      ``possible_duplicate_mrn`` for review.
    * otherwise a new patient is created.
    """

    def __init__(self, today: date) -> None:
        self.today = today
        self.patients: dict[str, Patient] = {}
        self._by_mrn: dict[str, str] = {}
        self._resolved: dict[int, str | None] = {}
        self.events: list[AuditEntry] = []

    # -- helpers -------------------------------------------------------------------------
    def _find_by_demographics(self, given: str | None, family: str | None, dob: date | None) -> Patient | None:
        if not (family and dob):
            return None
        for p in self.patients.values():
            if (
                p.birth_date == dob
                and p.family_name
                and p.family_name.lower() == family.lower()
                and c.name_similarity(p.given_name, given) >= NAME_MATCH_THRESHOLD
            ):
                return p
        return None

    @staticmethod
    def _add_flag(patient: Patient, flag: str) -> None:
        if flag not in patient.flags:
            patient.flags.append(flag)

    # -- main entry point ----------------------------------------------------------------
    def resolve(self, raw: RawPatient) -> str | None:
        key = id(raw)
        if key in self._resolved:
            return self._resolved[key]

        src = raw.source
        audit: list[AuditEntry] = []
        mrn, a = c.normalize_mrn(raw.mrn, source=src)
        audit += a
        given, family, a = c.normalize_name(raw.name, source=src)
        audit += a
        dob, a = c.parse_birth_date(raw.dob, today=self.today, source=src)
        audit += a
        gender_known = bool(raw.gender and raw.gender.strip())
        gender, a = c.normalize_gender(raw.gender, source=src)
        audit += a

        patient: Patient | None = None
        if mrn and mrn in self._by_mrn:
            patient = self.patients[self._by_mrn[mrn]]
            if patient.birth_date and dob and patient.birth_date != dob:
                self._add_flag(patient, "identifier_conflict")
                audit.append(AuditEntry(field="mrn", action="flagged", before=mrn, after=patient.mrn,
                                        reason=f"Same MRN but different DOB ({dob} vs {patient.birth_date})",
                                        source=src))
            elif given and patient.given_name and given.lower() != patient.given_name.lower():
                audit.append(AuditEntry(field="name", action="flagged", before=f"{given} {family}",
                                        after=patient.display_name,
                                        reason="Name variant for the same MRN; kept the primary-source spelling",
                                        source=src))
            else:
                audit.append(AuditEntry(field="patient", action="deduplicated", before=raw.mrn, after=patient.mrn,
                                        reason="Repeat mention of an existing patient merged", source=src))
        if patient is None:
            patient = self._find_by_demographics(given, family, dob)
            if patient is not None:
                if mrn and mrn != patient.mrn and mrn not in patient.alternate_mrns:
                    patient.alternate_mrns.append(mrn)
                    self._by_mrn[mrn] = patient.patient_id
                    self._add_flag(patient, "possible_duplicate_mrn")
                    audit.append(AuditEntry(field="mrn", action="linked", before=mrn, after=patient.mrn,
                                            reason="Different MRN but same name + DOB; linked as alternate "
                                                   "identifier pending HIM review", source=src))
                else:
                    audit.append(AuditEntry(field="mrn", action="linked", before=raw.mrn, after=patient.mrn,
                                            reason="Matched on name + DOB", source=src))
        if patient is None:
            if mrn is None:
                self.events.append(AuditEntry(field="patient", action="dropped", before=str(raw.name),
                                              reason="No MRN and no demographic match; cannot identify patient",
                                              source=src))
                self._resolved[key] = None
                return None
            patient = Patient(patient_id=f"pt-{mrn}", mrn=mrn, given_name=given, family_name=family,
                              birth_date=dob, gender=gender)
            self.patients[patient.patient_id] = patient
            self._by_mrn[mrn] = patient.patient_id
        else:
            # Fill gaps on the existing patient from this mention.
            if patient.birth_date is None and dob:
                patient.birth_date = dob
                audit.append(AuditEntry(field="birth_date", action="filled", after=dob.isoformat(),
                                        reason="DOB missing on primary record; filled from another source", source=src))
            if patient.gender == "unknown" and gender != "unknown":
                patient.gender = gender
                audit.append(AuditEntry(field="gender", action="filled", before="unknown", after=gender,
                                        reason="Gender missing on primary record; filled from another source",
                                        source=src))
            elif gender_known and gender != "unknown" and gender != patient.gender:
                self._add_flag(patient, "gender_conflict")
                audit.append(AuditEntry(field="gender", action="flagged", before=gender, after=patient.gender,
                                        reason="Conflicting gender across sources", source=src))
        # "Defaulted to unknown" is only recorded once, in finalize(), if no source ever supplied gender.
        audit = [a for a in audit if not (a.field == "gender" and a.action == "filled" and a.after == "unknown")]

        if src not in patient.sources:
            patient.sources.append(src)
        if patient.phone is None and raw.phone:
            patient.phone = raw.phone
        patient.audit.extend(audit)
        self._resolved[key] = patient.patient_id
        return patient.patient_id

    def finalize(self) -> list[Patient]:
        for p in self.patients.values():
            if p.gender == "unknown":
                p.audit.append(AuditEntry(field="gender", action="filled", after="unknown",
                                          reason="No source supplied gender; defaulted to 'unknown'"))
        return sorted(self.patients.values(), key=lambda p: p.patient_id)


class EncounterRegistry:
    def __init__(self) -> None:
        self.encounters: dict[str, Encounter] = {}

    def register(self, encounter_id: str, patient_id: str, class_code: str, start: datetime | None,
                 end: datetime | None) -> Encounter:
        if encounter_id not in self.encounters:
            self.encounters[encounter_id] = Encounter(
                encounter_id=encounter_id, patient_id=patient_id, class_code=class_code,  # type: ignore[arg-type]
                start=start, end=end,
            )
        return self.encounters[encounter_id]

    def find_or_create(self, patient_id: str, when: datetime | None) -> tuple[Encounter, bool]:
        """Attach a record to an existing encounter covering ``when``; otherwise synthesize
        a same-day ambulatory encounter so every resource has a valid encounter reference."""
        if when is not None:
            for enc in self.encounters.values():
                if enc.patient_id != patient_id or enc.start is None:
                    continue
                end = enc.end or enc.start
                if enc.start.date() <= when.date() <= end.date() + timedelta(days=0):
                    return enc, False
        day = when.date().isoformat() if when else "undated"
        enc_id = f"ENC-{patient_id.removeprefix('pt-')}-{day}"
        enc = self.encounters.get(enc_id) or Encounter(
            encounter_id=enc_id, patient_id=patient_id, class_code="AMB",
            start=datetime.combine(when.date(), datetime.min.time()) if when else None, synthesized=True,
        )
        self.encounters[enc_id] = enc
        return enc, True


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def clean_batch(batch: RawBatch, *, today: date | None = None) -> CleanDataset:
    today = today or date.today()
    patients = PatientRegistry(today)
    encounters = EncounterRegistry()
    pipeline_audit: list[AuditEntry] = [
        AuditEntry(field="file", action="dropped", reason=err) for err in batch.errors
    ]

    # Resolve explicit patient entries first (JSON export is the primary demographic source).
    for raw_patient in batch.patients:
        patients.resolve(raw_patient)

    records: list[ClinicalRecord] = []
    seen_ids: set[str] = set()
    seen_hashes: dict[str, str] = {}

    def add_record(rec: ClinicalRecord) -> None:
        dup_of = seen_hashes.get(rec.content_hash)
        if rec.record_id in seen_ids or dup_of:
            pipeline_audit.append(AuditEntry(
                field="record", action="deduplicated", before=rec.record_id, after=dup_of or rec.record_id,
                reason="Duplicate record (same id or identical content) removed", source=rec.source))
            return
        seen_ids.add(rec.record_id)
        seen_hashes[rec.content_hash] = rec.record_id
        records.append(rec)

    # ---- documents --------------------------------------------------------------------
    for doc in batch.documents:
        rec = _build_document(doc, patients, encounters, pipeline_audit, today)
        if rec:
            add_record(rec)

    # ---- labs: group rows into one DiagnosticReport per accession -------------------------
    groups: dict[tuple[str, str], list[RawLabRow]] = defaultdict(list)
    for row in batch.lab_rows:
        pid = patients.resolve(row.patient)
        if pid is None:
            pipeline_audit.append(AuditEntry(field="lab_row", action="dropped", source=row.source,
                                             reason="Lab row could not be linked to a patient"))
            continue
        accession = row.accession or f"NOACC-{_short_hash(pid, row.collected or '', row.panel or '')}"
        groups[(pid, accession)].append(row)
    for (pid, accession), rows in groups.items():
        rec = _build_lab_report(pid, accession, rows, encounters, pipeline_audit, today)
        if rec:
            add_record(rec)

    records.sort(key=lambda r: (r.patient_id, r.record_date or datetime.min, r.record_id))
    patient_list = patients.finalize()
    pipeline_audit.extend(patients.events)
    used_encounters = {r.encounter_id for r in records}
    encounter_list = sorted(
        (e for e in encounters.encounters.values() if e.encounter_id in used_encounters),
        key=lambda e: (e.patient_id, e.start or datetime.min),
    )
    stats = {
        "raw_patient_mentions": len(batch.patients),
        "raw_documents": len(batch.documents),
        "raw_lab_rows": len(batch.lab_rows),
        "patients": len(patient_list),
        "encounters": len(encounter_list),
        "records": len(records),
        "duplicates_removed": sum(1 for a in pipeline_audit if a.action == "deduplicated"),
        "items_dropped": sum(1 for a in pipeline_audit if a.action == "dropped"),
        "patients_flagged": sum(1 for p in patient_list if p.flags),
        "record_audit_entries": sum(len(r.audit) for r in records),
        "patient_audit_entries": sum(len(p.audit) for p in patient_list),
    }
    return CleanDataset(patients=patient_list, encounters=encounter_list, records=records,
                        pipeline_audit=pipeline_audit, stats=stats)


def _build_document(doc: RawDocument, patients: PatientRegistry, encounters: EncounterRegistry,
                    pipeline_audit: list[AuditEntry], today: date) -> ClinicalRecord | None:
    src = doc.source
    pid = patients.resolve(doc.patient)
    if pid is None:
        pipeline_audit.append(AuditEntry(field="document", action="dropped", source=src,
                                         reason="Document could not be linked to a patient"))
        return None
    text, audit = c.clean_text(doc.text, source=src)
    if not text:
        pipeline_audit.append(AuditEntry(field="document", action="dropped", before=doc.doc_id, source=src,
                                         reason="Empty document body"))
        return None

    doc_type, category, _loinc, _display, title, a = c.classify_document(doc.doc_type, text, source=src)
    audit += a

    record_date, a = c.parse_datetime(doc.date, field="record_date", today=today, source=src)
    audit += a
    enc_start, _ = c.parse_datetime(doc.encounter_start, field="encounter_start", today=today, source=src)
    enc_end, _ = c.parse_datetime(doc.encounter_end, field="encounter_end", today=today, source=src)
    if record_date is None:
        fallback = enc_end or enc_start
        if fallback is not None:
            record_date = fallback
            audit.append(AuditEntry(field="record_date", action="filled", after=fallback.date().isoformat(),
                                    reason="Record date missing/invalid; filled from encounter "
                                           f"{'end' if enc_end else 'start'} date", source=src))
        else:
            audit.append(AuditEntry(field="record_date", action="flagged", reason="No usable record date",
                                    source=src))

    if doc.encounter_id:
        class_code = c.ENCOUNTER_CLASS_MAP.get((doc.encounter_class or "").lower(), "AMB")
        enc = encounters.register(doc.encounter_id, pid, class_code, enc_start or record_date, enc_end)
    else:
        enc, created = encounters.find_or_create(pid, record_date)
        audit.append(AuditEntry(
            field="encounter", action="filled" if created else "linked", after=enc.encounter_id,
            reason="No encounter in source; " + ("created same-day ambulatory encounter" if created
                                                 else "linked to encounter covering the record date"),
            source=src))

    record_id = doc.doc_id or f"SCN-{_short_hash(pid, doc.date or '', doc.doc_type or '', text[:64])}"
    return ClinicalRecord(
        record_id=record_id, patient_id=pid, encounter_id=enc.encounter_id, category=category, doc_type=doc_type,
        title=title, record_date=record_date, author=doc.author, text=text, source=src,
        content_hash=c.content_hash(pid, doc_type, text), audit=audit,
    )


def _build_lab_report(pid: str, accession: str, rows: list[RawLabRow], encounters: EncounterRegistry,
                      pipeline_audit: list[AuditEntry], today: date) -> ClinicalRecord | None:
    audit: list[AuditEntry] = []
    results: list[LabResult] = []
    seen: set[tuple] = set()
    collected: datetime | None = None
    sources = []
    for row in rows:
        key = (row.test, row.loinc, row.result, row.collected)
        if key in seen:
            pipeline_audit.append(AuditEntry(field="lab_row", action="deduplicated", before=row.source,
                                             reason=f"Duplicate result row for {row.test} in {accession}",
                                             source=row.source))
            continue
        seen.add(key)
        sources.append(row.source)
        if collected is None:
            collected, a = c.parse_datetime(row.collected, field="record_date", today=today, source=row.source)
            audit += a
        result, a = c.parse_lab_result(row.test or "Unknown test", row.loinc, row.result, row.units,
                                       row.ref_range, row.flag, source=row.source)
        audit += a
        if result:
            results.append(result)
    if not results:
        pipeline_audit.append(AuditEntry(field="lab_report", action="dropped", before=accession,
                                         reason="Lab accession has no usable results"))
        return None

    panel = rows[0].panel or "Laboratory Panel"
    enc, created = encounters.find_or_create(pid, collected)
    audit.append(AuditEntry(
        field="encounter", action="filled" if created else "linked", after=enc.encounter_id,
        reason="Lab has no encounter in source; " + ("created same-day ambulatory encounter" if created
                                                     else "linked to encounter covering the collection date"),
        source=sources[0]))
    text = _lab_narrative(panel, results)
    return ClinicalRecord(
        record_id=f"LAB-{accession}", patient_id=pid, encounter_id=enc.encounter_id, category="lab",
        doc_type="Laboratory Report", title=panel, record_date=collected, text=text, results=results,
        source=", ".join(sources), content_hash=c.content_hash(pid, accession, text), audit=audit,
    )


_INTERP_WORD = {"H": "HIGH", "L": "LOW", "HH": "CRITICAL HIGH", "LL": "CRITICAL LOW", "A": "ABNORMAL", "N": "normal"}


def _lab_narrative(panel: str, results: list[LabResult]) -> str:
    """Human/LLM/embedding friendly text for a lab report."""
    parts = []
    for r in results:
        value = f"{r.value_raw} {r.unit or ''}".strip()
        ref = f" (ref {r.reference_range})" if r.reference_range else ""
        flag = f" [{_INTERP_WORD[r.interpretation]}]" if r.interpretation and r.interpretation != "N" else ""
        parts.append(f"{r.test_name} {value}{ref}{flag}")
    return f"{panel}: " + "; ".join(parts) + "."


def run_ingestion(raw_dir: Path, *, today: date | None = None) -> CleanDataset:
    return clean_batch(load_directory(raw_dir), today=today)
