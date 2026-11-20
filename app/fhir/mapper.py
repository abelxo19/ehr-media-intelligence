"""Map the cleaned canonical models to HL7 FHIR R4 resources and one Bundle per patient.

Resources produced
* Patient            - demographics, canonical + alternate MRNs, data-quality flags as meta.tag
* Encounter          - every record references one (source or synthesized)
* DocumentReference  - clinical notes (discharge summaries, consults, progress, ED, operative)
* DiagnosticReport   - labs, imaging, pathology, cardiology reports
* Observation        - one per lab analyte, referenced from DiagnosticReport.result

Resources are built as plain FHIR JSON dicts (exactly what goes on the wire) and then
validated with ``fhir.resources`` in :mod:`app.fhir.validator`.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.ingestion import cleaning
from app.ingestion.models import CleanDataset, ClinicalRecord, Encounter, LabResult, Patient

LOCAL_TZ = ZoneInfo("America/New_York")  # source timestamps are local wall-clock times
NAMESPACE = uuid.UUID("6f1c2a52-6f7e-4a0c-9d3b-0e5b8c1f4a10")  # fixed -> deterministic ids

MRN_SYSTEM = "http://hospital.example.org/fhir/sid/mrn"  # example.org: synthetic data
LOINC = "http://loinc.org"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
V3_ACT = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
V3_INTERP = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
US_CORE_DOC_CATEGORY = "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category"
DQ_TAG_SYSTEM = "http://onye.example.org/fhir/CodeSystem/data-quality-flag"
UCUM = "http://unitsofmeasure.org"

_ENCOUNTER_CLASS_DISPLAY = {"AMB": "ambulatory", "IMP": "inpatient encounter", "EMER": "emergency"}
_DR_CATEGORY = {
    "lab": ("LAB", "Laboratory"),
    "imaging": ("RAD", "Radiology"),
    "pathology": ("PAT", "Pathology (gross & histopath, not surgical)"),
    "cardiology": ("CUS", "Cardiac Ultrasound"),
}
_INTERP_DISPLAY = {"N": "Normal", "H": "High", "L": "Low", "HH": "Critical high", "LL": "Critical low",
                   "A": "Abnormal"}
# Units that are already valid UCUM codes (others are sent as display-only units).
_UCUM_SAFE = {"mg/dL", "mmol/L", "%", "ng/L", "g/dL", "pg/mL", "U/mL", "ng/mL", "ng/dL", "mIU/L", "10*3/uL"}


def fhir_id(*parts: str) -> str:
    """Deterministic UUID so re-running the pipeline yields identical Bundles."""
    return str(uuid.uuid5(NAMESPACE, "|".join(parts)))


def ref(resource_type: str, rid: str) -> dict[str, str]:
    return {"reference": f"urn:uuid:{rid}", "type": resource_type}


def _fhir_datetime(value: datetime | None) -> str | None:
    """FHIR dateTime: date-only stays a date; if a time is present a zone is required."""
    if value is None:
        return None
    if value.hour == value.minute == value.second == 0:
        return value.date().isoformat()
    return value.replace(tzinfo=LOCAL_TZ).isoformat()


def _fhir_instant(value: datetime | None) -> str | None:
    """FHIR instant: always full precision with a zone."""
    if value is None:
        return None
    return value.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


_IMPRESSION = re.compile(r"IMPRESSION:\s*(.+)$", re.IGNORECASE | re.DOTALL)


def _conclusion(text: str) -> str:
    m = _IMPRESSION.search(text)
    return (m.group(1) if m else text).strip()


# ---------------------------------------------------------------------------------------
# Resource builders
# ---------------------------------------------------------------------------------------
def map_patient(p: Patient) -> dict[str, Any]:
    identifiers = [{
        "use": "usual",
        "type": {"coding": [{"system": V2_0203, "code": "MR", "display": "Medical record number"}]},
        "system": MRN_SYSTEM,
        "value": p.mrn,
    }]
    for alt in p.alternate_mrns:
        identifiers.append({
            "use": "secondary",
            "type": {"coding": [{"system": V2_0203, "code": "MR", "display": "Medical record number"}],
                     "text": "Alternate MRN linked by name + DOB (pending review)"},
            "system": MRN_SYSTEM,
            "value": alt,
        })
    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": fhir_id("Patient", p.patient_id),
        "identifier": identifiers,
        "active": True,
        "name": [{"use": "official", "family": p.family_name, "given": [p.given_name] if p.given_name else None,
                  "text": p.display_name}],
        "gender": p.gender,
    }
    if p.birth_date:
        resource["birthDate"] = p.birth_date.isoformat()
    if p.phone:
        resource["telecom"] = [{"system": "phone", "value": p.phone, "use": "home"}]
    if p.flags:
        resource["meta"] = {"tag": [{"system": DQ_TAG_SYSTEM, "code": f, "display": f.replace("_", " ")}
                                    for f in p.flags]}
    resource["name"][0] = {k: v for k, v in resource["name"][0].items() if v}
    return resource


def map_encounter(e: Encounter, patient_ref: dict[str, str]) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "Encounter",
        "id": fhir_id("Encounter", e.encounter_id),
        "identifier": [{"system": "http://hospital.example.org/fhir/sid/encounter", "value": e.encounter_id}],
        "status": "finished",
        "class": {"system": V3_ACT, "code": e.class_code, "display": _ENCOUNTER_CLASS_DISPLAY[e.class_code]},
        "subject": patient_ref,
    }
    period = {k: v for k, v in (("start", _fhir_datetime(e.start)), ("end", _fhir_datetime(e.end))) if v}
    if period:
        resource["period"] = period
    return resource


def map_document_reference(r: ClinicalRecord, patient_ref: dict, encounter_ref: dict) -> dict[str, Any]:
    _type, _cat, loinc, display, _title, _ = cleaning.classify_document(r.doc_type, r.text)
    resource: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "id": fhir_id("DocumentReference", r.record_id),
        "masterIdentifier": {"system": "http://hospital.example.org/fhir/sid/document", "value": r.record_id},
        "status": "current",
        "docStatus": "final",
        "type": {"coding": [{"system": LOINC, "code": loinc, "display": display}], "text": r.title},
        "category": [{"coding": [{"system": US_CORE_DOC_CATEGORY, "code": "clinical-note",
                                  "display": "Clinical Note"}]}],
        "subject": patient_ref,
        "content": [{"attachment": {"contentType": "text/plain; charset=utf-8", "language": "en-US",
                                    "data": _b64(r.text), "title": r.title,
                                    "creation": _fhir_datetime(r.record_date)}}],
        "context": {"encounter": [encounter_ref]},
    }
    if r.record_date:
        resource["date"] = _fhir_instant(r.record_date)
        resource["context"]["period"] = {"start": _fhir_datetime(r.record_date)}
    if r.author:
        resource["author"] = [{"display": r.author}]
    attachment = resource["content"][0]["attachment"]
    if attachment["creation"] is None:
        del attachment["creation"]
    return resource


def map_observation(r: ClinicalRecord, lab: LabResult, idx: int, patient_ref: dict, encounter_ref: dict
                    ) -> dict[str, Any]:
    code: dict[str, Any] = {"text": lab.test_name}
    if lab.loinc:
        code["coding"] = [{"system": LOINC, "code": lab.loinc, "display": lab.test_name}]
    resource: dict[str, Any] = {
        "resourceType": "Observation",
        "id": fhir_id("Observation", r.record_id, str(idx), lab.test_name),
        "status": "final",
        "category": [{"coding": [{"system": OBS_CATEGORY, "code": "laboratory", "display": "Laboratory"}]}],
        "code": code,
        "subject": patient_ref,
        "encounter": encounter_ref,
    }
    if r.record_date:
        resource["effectiveDateTime"] = _fhir_datetime(r.record_date)
    if lab.value_numeric is not None:
        qty: dict[str, Any] = {"value": lab.value_numeric}
        if lab.comparator:
            qty["comparator"] = lab.comparator
        if lab.unit:
            qty["unit"] = lab.unit
            if lab.unit in _UCUM_SAFE:
                qty.update({"system": UCUM, "code": lab.unit})
        resource["valueQuantity"] = qty
    else:
        resource["valueString"] = lab.value_raw
    if lab.reference_range:
        resource["referenceRange"] = [{"text": lab.reference_range}]
    if lab.interpretation:
        resource["interpretation"] = [{"coding": [{"system": V3_INTERP, "code": lab.interpretation,
                                                   "display": _INTERP_DISPLAY[lab.interpretation]}]}]
    return resource


def map_diagnostic_report(r: ClinicalRecord, patient_ref: dict, encounter_ref: dict,
                          observation_refs: list[dict]) -> dict[str, Any]:
    _type, _cat, loinc, display, _title, _ = cleaning.classify_document(r.doc_type, r.text)
    cat_code, cat_display = _DR_CATEGORY[r.category]
    resource: dict[str, Any] = {
        "resourceType": "DiagnosticReport",
        "id": fhir_id("DiagnosticReport", r.record_id),
        "identifier": [{"system": "http://hospital.example.org/fhir/sid/report", "value": r.record_id}],
        "status": "final",
        "category": [{"coding": [{"system": V2_0074, "code": cat_code, "display": cat_display}]}],
        "code": {"coding": [{"system": LOINC, "code": loinc, "display": display}], "text": r.title},
        "subject": patient_ref,
        "encounter": encounter_ref,
        "presentedForm": [{"contentType": "text/plain; charset=utf-8", "language": "en-US",
                           "data": _b64(r.text), "title": r.title}],
    }
    if r.record_date:
        resource["effectiveDateTime"] = _fhir_datetime(r.record_date)
        resource["issued"] = _fhir_instant(r.record_date)
    if r.author:
        resource["resultsInterpreter"] = [{"display": r.author}]
    if observation_refs:
        resource["result"] = observation_refs
    if r.category != "lab":
        resource["conclusion"] = _conclusion(r.text)
    return resource


# ---------------------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------------------
def bundle_content_hash(patient: Patient, records: list[ClinicalRecord]) -> str:
    """Hash of everything that would change a summary. Used as the summary cache key."""
    h = hashlib.sha256()
    h.update(patient.model_dump_json(exclude={"audit", "sources"}).encode())
    for r in sorted(records, key=lambda x: x.record_id):
        h.update(r.record_id.encode())
        h.update(r.content_hash.encode())
    return h.hexdigest()


def build_patient_bundle(patient: Patient, encounters: list[Encounter], records: list[ClinicalRecord],
                         *, timestamp: datetime | None = None) -> dict[str, Any]:
    patient_res = map_patient(patient)
    patient_ref = ref("Patient", patient_res["id"]) | {"display": patient.display_name}
    entries: list[dict[str, Any]] = [patient_res]

    enc_refs: dict[str, dict] = {}
    for e in encounters:
        enc_res = map_encounter(e, patient_ref)
        enc_refs[e.encounter_id] = ref("Encounter", enc_res["id"])
        entries.append(enc_res)

    for r in records:
        enc_ref = enc_refs[r.encounter_id]
        if r.fhir_resource_type == "DocumentReference":
            entries.append(map_document_reference(r, patient_ref, enc_ref))
            continue
        observations = [map_observation(r, lab, i, patient_ref, enc_ref) for i, lab in enumerate(r.results)]
        entries.append(map_diagnostic_report(r, patient_ref, enc_ref,
                                             [ref("Observation", o["id"]) for o in observations]))
        entries.extend(observations)

    ts = (timestamp or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "resourceType": "Bundle",
        "id": fhir_id("Bundle", patient.patient_id),
        "meta": {"lastUpdated": ts},
        "identifier": {"system": "http://onye.example.org/fhir/sid/bundle", "value": patient.patient_id},
        "type": "collection",
        "timestamp": ts,
        "entry": [{"fullUrl": f"urn:uuid:{res['id']}", "resource": res} for res in entries],
    }


def build_all_bundles(dataset: CleanDataset, *, timestamp: datetime | None = None) -> dict[str, dict[str, Any]]:
    return {
        p.patient_id: build_patient_bundle(p, dataset.encounters_for(p.patient_id),
                                           dataset.records_for(p.patient_id), timestamp=timestamp)
        for p in dataset.patients
    }


def resource_index(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """fullUrl -> resource, for resolving references inside a bundle."""
    return {e["fullUrl"]: e["resource"] for e in bundle.get("entry", [])}


def as_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])
