"""Format-specific loaders. They only *parse*; they never clean or validate.

Supported inputs:
* ``.json`` - simulated EHR export (patients -> encounters -> documents)
* ``.csv``  - lab results, one row per analyte
* ``.txt``  - OCR'd scanned notes separated by a banner line with a key/value header

Each loader returns a :class:`RawBatch`. Unparseable items are reported in
``RawBatch.errors`` instead of crashing the whole run.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from app.ingestion.models import RawBatch, RawDocument, RawLabRow, RawPatient

# Column aliases seen across exports -> canonical key
_PATIENT_KEY_ALIASES = {
    "mrn": ("mrn", "MRN", "medical_record_number", "patient_mrn", "mrn#"),
    "name": ("name", "patient_name", "Patient Name", "patient"),
    "dob": ("dob", "DOB", "birthDate", "birth_date", "date_of_birth"),
    "gender": ("gender", "sex", "Sex", "Gender"),
    "phone": ("phone", "telecom", "phone_number"),
}


def _pick(data: dict[str, Any], canonical: str) -> Any:
    for alias in _PATIENT_KEY_ALIASES[canonical]:
        if alias in data and data[alias] not in (None, ""):
            return data[alias]
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _raw_patient(data: dict[str, Any], source: str) -> RawPatient:
    return RawPatient(
        source=source,
        mrn=_str_or_none(_pick(data, "mrn")),
        name=_pick(data, "name"),
        dob=_str_or_none(_pick(data, "dob")),
        gender=_str_or_none(_pick(data, "gender")),
        phone=_str_or_none(_pick(data, "phone")),
    )


# ---------------------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------------------
def load_json(path: Path) -> RawBatch:
    batch = RawBatch()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        batch.errors.append(f"{path.name}: invalid JSON ({exc})")
        return batch

    patients = payload.get("patients") if isinstance(payload, dict) else payload
    if not isinstance(patients, list):
        batch.errors.append(f"{path.name}: expected a 'patients' array")
        return batch

    for p_idx, p in enumerate(patients):
        if not isinstance(p, dict):
            batch.errors.append(f"{path.name}: patients[{p_idx}] is not an object")
            continue
        source = f"{path.name}#patients[{p_idx}]"
        patient = _raw_patient(p, source)
        batch.patients.append(patient)
        for e_idx, enc in enumerate(p.get("encounters") or []):
            for d_idx, doc in enumerate(enc.get("documents") or []):
                batch.documents.append(
                    RawDocument(
                        source=f"{source}.encounters[{e_idx}].documents[{d_idx}]",
                        patient=patient,
                        doc_id=_str_or_none(doc.get("doc_id") or doc.get("id")),
                        doc_type=_str_or_none(doc.get("type") or doc.get("doc_type")),
                        date=_str_or_none(doc.get("date")),
                        author=_str_or_none(doc.get("author")),
                        text=doc.get("text") if isinstance(doc.get("text"), str) else None,
                        encounter_id=_str_or_none(enc.get("encounter_id") or enc.get("id")),
                        encounter_class=_str_or_none(enc.get("class")),
                        encounter_start=_str_or_none(enc.get("start")),
                        encounter_end=_str_or_none(enc.get("end")),
                    )
                )
    return batch


# ---------------------------------------------------------------------------------------
# CSV (lab results)
# ---------------------------------------------------------------------------------------
_LAB_COLUMNS = {
    "accession": ("Accession", "accession", "accession_number"),
    "collected": ("Collected", "collected", "collection_date", "date"),
    "panel": ("Panel", "panel", "order_name"),
    "test": ("Test", "test", "test_name", "analyte"),
    "loinc": ("LOINC", "loinc", "loinc_code"),
    "result": ("Result", "result", "value"),
    "units": ("Units", "units", "unit"),
    "ref_range": ("Ref Range", "ref_range", "reference_range"),
    "flag": ("Flag", "flag", "abnormal_flag"),
}


def _col(row: dict[str, Any], key: str) -> str | None:
    for alias in _LAB_COLUMNS[key]:
        if alias in row:
            return _str_or_none(row[alias])
    return None


def load_csv(path: Path) -> RawBatch:
    batch = RawBatch()
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for line_no, row in enumerate(reader, start=2):  # header is line 1
            source = f"{path.name}:{line_no}"
            if not any((v or "").strip() for v in row.values()):
                batch.errors.append(f"{source}: blank row skipped")
                continue
            batch.lab_rows.append(
                RawLabRow(
                    source=source,
                    patient=_raw_patient(row, source),
                    accession=_col(row, "accession"),
                    collected=_col(row, "collected"),
                    panel=_col(row, "panel"),
                    test=_col(row, "test"),
                    loinc=_col(row, "loinc"),
                    result=_col(row, "result"),
                    units=_col(row, "units"),
                    ref_range=_col(row, "ref_range"),
                    flag=_col(row, "flag"),
                )
            )
    return batch


# ---------------------------------------------------------------------------------------
# Plain text (scanned / OCR'd notes)
# ---------------------------------------------------------------------------------------
_BANNER = re.compile(r"^=+\s*SCANNED DOCUMENT\s*=+\s*$", re.MULTILINE)
_BODY_SEPARATOR = re.compile(r"^-{5,}\s*$", re.MULTILINE)
_HEADER_LINE = re.compile(r"^\s*([A-Za-z #]+?)\s*:\s*(.*?)\s*$")

_HEADER_KEYS = {
    "patient": "name",
    "patient name": "name",
    "mrn": "mrn",
    "mrn#": "mrn",
    "mrn #": "mrn",
    "dob": "dob",
    "date of birth": "dob",
    "sex": "gender",
    "gender": "gender",
    "date of service": "date",
    "date": "date",
    "document type": "doc_type",
    "type": "doc_type",
    "author": "author",
    "encounter": "encounter_id",
}


def load_text(path: Path) -> RawBatch:
    batch = RawBatch()
    content = path.read_text(encoding="utf-8")
    blocks = [b for b in _BANNER.split(content) if b.strip()]
    for idx, block in enumerate(blocks, start=1):
        source = f"{path.name}#doc{idx}"
        parts = _BODY_SEPARATOR.split(block, maxsplit=1)
        if len(parts) != 2:
            batch.errors.append(f"{source}: no header/body separator found, skipped")
            continue
        header_text, body = parts
        header: dict[str, str] = {}
        for line in header_text.splitlines():
            match = _HEADER_LINE.match(line)
            if match:
                key = _HEADER_KEYS.get(match.group(1).strip().lower())
                if key:
                    header[key] = match.group(2)
        patient = RawPatient(
            source=source,
            mrn=_str_or_none(header.get("mrn")),
            name=_str_or_none(header.get("name")),
            dob=_str_or_none(header.get("dob")),
            gender=_str_or_none(header.get("gender")),
        )
        batch.patients.append(patient)
        batch.documents.append(
            RawDocument(
                source=source,
                patient=patient,
                doc_type=_str_or_none(header.get("doc_type")),
                date=_str_or_none(header.get("date")),
                author=_str_or_none(header.get("author")),
                text=body,
                encounter_id=_str_or_none(header.get("encounter_id")),
            )
        )
    return batch


LOADERS = {".json": load_json, ".csv": load_csv, ".txt": load_text}


def load_file(path: Path) -> RawBatch:
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        batch = RawBatch()
        batch.errors.append(f"{path.name}: unsupported format '{path.suffix}'")
        return batch
    return loader(path)


def load_directory(directory: Path) -> RawBatch:
    """Load every supported file in ``directory`` in a deterministic order (JSON first,
    because the structured EHR export is the most trusted source for demographics)."""
    order = {".json": 0, ".csv": 1, ".txt": 2}
    files = sorted(
        (p for p in directory.iterdir() if p.suffix.lower() in LOADERS),
        key=lambda p: (order[p.suffix.lower()], p.name),
    )
    batch = RawBatch()
    for path in files:
        batch.extend(load_file(path))
    return batch
