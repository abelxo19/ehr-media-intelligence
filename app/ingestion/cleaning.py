"""Pure normalization functions. Each returns ``(value, [AuditEntry, ...])`` so the
caller can attach exactly what changed to the record it is building.

Keeping these side-effect free makes the edge cases easy to unit test.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from app.ingestion.models import AuditEntry, Gender, Interpretation, LabResult, RecordCategory

# ---------------------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------------------
# Order matters: unambiguous ISO-style formats first, then US formats.
_DATETIME_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%m/%d/%Y %H:%M",
]
_DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m/%d/%y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
]
_US_SLASH = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")
MIN_PLAUSIBLE_YEAR = 1900


def parse_datetime(
    value: str | None,
    *,
    field: str,
    today: date | None = None,
    source: str | None = None,
    allow_future: bool = False,
) -> tuple[datetime | None, list[AuditEntry]]:
    """Parse the many date spellings found in EHR exports into a ``datetime``.

    * US ``MM/DD`` is assumed for slash dates (Epic default); if both parts are <= 12
      the interpretation is recorded in the audit log, if the first part is > 12 the
      date is re-read as ``DD/MM`` and flagged.
    * Dates in the future or before 1900 are cleared and flagged rather than trusted.
    """
    audit: list[AuditEntry] = []
    if value is None or not str(value).strip():
        return None, audit

    raw = " ".join(str(value).split())
    today = today or date.today()
    parsed: datetime | None = None

    for fmt in _DATETIME_FORMATS + _DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue

    us = _US_SLASH.match(raw)
    if parsed is None and us and int(us.group(1)) > 12 and int(us.group(2)) <= 12:
        # Looks like DD/MM/YYYY - swap and flag.
        swapped = f"{us.group(2)}/{us.group(1)}/{us.group(3)}"
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                parsed = datetime.strptime(swapped, fmt)
                audit.append(
                    AuditEntry(
                        field=field, action="flagged", before=raw, after=parsed.date().isoformat(),
                        reason="Day > 12 in month position; interpreted as DD/MM/YYYY", source=source,
                    )
                )
                break
            except ValueError:
                continue

    if parsed is None:
        audit.append(
            AuditEntry(field=field, action="flagged", before=raw, after=None,
                       reason="Unparseable date cleared", source=source)
        )
        return None, audit

    if us and int(us.group(1)) <= 12 and int(us.group(2)) <= 12 and us.group(1) != us.group(2):
        audit.append(
            AuditEntry(
                field=field, action="normalized", before=raw, after=parsed.date().isoformat(),
                reason="Ambiguous slash date interpreted as US MM/DD/YYYY", source=source,
            )
        )

    if parsed.year < MIN_PLAUSIBLE_YEAR:
        audit.append(AuditEntry(field=field, action="flagged", before=raw, after=None,
                                reason=f"Implausible year {parsed.year}; cleared", source=source))
        return None, audit
    if not allow_future and parsed.date() > today + timedelta(days=1):
        audit.append(AuditEntry(field=field, action="flagged", before=raw, after=None,
                                reason="Date is in the future (likely a typo); cleared", source=source))
        return None, audit

    canonical = parsed.isoformat() if (parsed.hour or parsed.minute) else parsed.date().isoformat()
    if raw != canonical and not any(a.action == "normalized" for a in audit):
        audit.append(AuditEntry(field=field, action="normalized", before=raw, after=canonical,
                                reason="Date converted to ISO 8601", source=source))
    return parsed, audit


def parse_birth_date(value: str | None, *, today: date | None = None,
                     source: str | None = None) -> tuple[date | None, list[AuditEntry]]:
    parsed, audit = parse_datetime(value, field="birth_date", today=today, source=source)
    return (parsed.date() if parsed else None), audit


# ---------------------------------------------------------------------------------------
# Gender
# ---------------------------------------------------------------------------------------
_GENDER_MAP: dict[str, Gender] = {
    "m": "male", "male": "male", "man": "male", "1": "male",
    "f": "female", "female": "female", "woman": "female", "2": "female",
    "o": "other", "other": "other", "x": "other", "nonbinary": "other", "non-binary": "other",
    "u": "unknown", "unk": "unknown", "unknown": "unknown", "9": "unknown",
}


def normalize_gender(value: str | None, *, source: str | None = None) -> tuple[Gender, list[AuditEntry]]:
    """Map local sex/gender codes (M/F, 1/2 HL7 v2 style, words) to FHIR AdministrativeGender."""
    if value is None or not str(value).strip():
        return "unknown", [AuditEntry(field="gender", action="filled", before=None, after="unknown",
                                      reason="Missing gender defaulted to 'unknown'", source=source)]
    raw = str(value).strip()
    mapped = _GENDER_MAP.get(raw.lower())
    if mapped is None:
        return "unknown", [AuditEntry(field="gender", action="flagged", before=raw, after="unknown",
                                      reason="Unrecognised gender code", source=source)]
    if raw != mapped:
        return mapped, [AuditEntry(field="gender", action="normalized", before=raw, after=mapped,
                                   reason="Mapped to FHIR AdministrativeGender", source=source)]
    return mapped, []


# ---------------------------------------------------------------------------------------
# MRN
# ---------------------------------------------------------------------------------------
MRN_LENGTH = 7


def normalize_mrn(value: str | None, *, source: str | None = None) -> tuple[str | None, list[AuditEntry]]:
    """Canonical MRN = 7 digits, zero padded. Strips prefixes like 'MRN-', 'mrn ', '00-'."""
    if value is None or not str(value).strip():
        return None, [AuditEntry(field="mrn", action="flagged", before=None, after=None,
                                 reason="Missing MRN; identity must be resolved by name + DOB", source=source)]
    raw = str(value).strip()
    digits = re.sub(r"\D", "", raw)
    if not digits or len(digits.lstrip("0")) > MRN_LENGTH or len(digits) > MRN_LENGTH + 2:
        return None, [AuditEntry(field="mrn", action="flagged", before=raw, after=None,
                                 reason="MRN is not a recognisable numeric identifier", source=source)]
    canonical = digits.lstrip("0").zfill(MRN_LENGTH)
    if canonical != raw:
        return canonical, [AuditEntry(field="mrn", action="normalized", before=raw, after=canonical,
                                      reason="MRN stripped of prefix/punctuation and zero-padded", source=source)]
    return canonical, []


# ---------------------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------------------
def _title(part: str) -> str:
    """Re-case ALL-CAPS / all-lower names ("O'BRIEN" -> "O'Brien"); leave mixed case alone."""
    part = " ".join(part.split())
    if part.isupper() or part.islower():
        return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), part)
    return part


def normalize_name(value: Any, *, source: str | None = None) -> tuple[str | None, str | None, list[AuditEntry]]:
    """Accept {'given','family'}, 'FAMILY, GIVEN' or 'Given Family' and return (given, family)."""
    audit: list[AuditEntry] = []
    if isinstance(value, dict):
        given, family = value.get("given"), value.get("family")
        raw = f"{given} {family}"
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if "," in raw:
            family, given = (s.strip() for s in raw.split(",", 1))
        else:
            *given_parts, family = raw.split()
            given = " ".join(given_parts) or None
    else:
        return None, None, [AuditEntry(field="name", action="flagged", before=None, after=None,
                                       reason="Missing patient name", source=source)]
    given_n = _title(given) if given else None
    family_n = _title(family) if family else None
    normalized = f"{given_n} {family_n}"
    if normalized != raw:
        audit.append(AuditEntry(field="name", action="normalized", before=raw, after=normalized,
                                reason="Name split into given/family and re-cased", source=source))
    return given_n, family_n, audit


def name_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------------------------------------------------------------------------------------
# Free text
# ---------------------------------------------------------------------------------------
# "hypo-\nthyroidism" -> "hypothyroidism", but keep "ICS-\nformoterol" -> "ICS-formoterol"
_HYPHEN_BREAK = re.compile(r"([a-z])-\s*\n\s*([a-z])")
_UPPER_HYPHEN_BREAK = re.compile(r"(\w)-\s*\n\s*(\w)")


def clean_text(text: str | None, *, source: str | None = None) -> tuple[str, list[AuditEntry]]:
    if not text:
        return "", []
    original = text
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _UPPER_HYPHEN_BREAK.sub(r"\1-\2", text)
    text = " ".join(text.split())
    audit = []
    if text != " ".join(original.split()):
        audit.append(AuditEntry(field="text", action="normalized", before=None, after=None,
                                reason="Re-joined words hyphenated across OCR line breaks", source=source))
    return text, audit


def content_hash(*parts: str | None) -> str:
    joined = "\x1f".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------
# Document type -> category / LOINC
# ---------------------------------------------------------------------------------------
# (keywords, canonical type, category, LOINC code, LOINC display)
_DOC_TYPES: list[tuple[tuple[str, ...], str, RecordCategory, str, str]] = [
    (("discharge",), "Discharge Summary", "clinical_note", "18842-5", "Discharge summary"),
    (("operative", "op note", "procedure note"), "Operative Note", "clinical_note", "11504-8", "Surgical operation note"),
    (("consult",), "Consult Note", "clinical_note", "11488-4", "Consult note"),
    (("ed note", "emergency"), "Emergency Department Note", "clinical_note", "34111-5", "Emergency department Note"),
    (("progress",), "Progress Note", "clinical_note", "11506-3", "Progress note"),
    (("pathology", "biopsy"), "Pathology Report", "pathology", "11526-1", "Pathology study"),
    (("catheterization", "cath "), "Cardiac Catheterization Report", "cardiology", "18745-0", "Cardiac catheterization study"),
    (("echo",), "Echocardiogram", "cardiology", "18748-4", "Diagnostic imaging study"),
    (("x-ray", "xray", "radiograph", " ct", "ct ", "mri", "ultrasound", "mammogram", "pet", "dexa",
      "duplex", "imaging", "sonogram"),
     "Imaging Report", "imaging", "18748-4", "Diagnostic imaging study"),
    (("retinal", "fundus"), "Ophthalmology Exam Note", "clinical_note", "34109-9", "Note"),
    (("lab", "panel"), "Laboratory Report", "lab", "11502-2", "Laboratory report"),
]
_GENERIC_NOTE = ("Clinical Note", "clinical_note", "34109-9", "Note")


def classify_document(doc_type: str | None, text: str, *, source: str | None = None
                      ) -> tuple[str, RecordCategory, str, str, str, list[AuditEntry]]:
    """Return (canonical_type, category, loinc, loinc_display, title, audit)."""
    audit: list[AuditEntry] = []
    title = " ".join(doc_type.split()) if doc_type else None
    haystack = f" {title.lower()} " if title else None
    if haystack is None or "scanned document" in haystack:
        # Infer from the body when the type is missing / uninformative.
        haystack = f" {text[:200].lower()} "
        reason_before = title
    else:
        reason_before = None

    for keywords, canonical, category, loinc, display in _DOC_TYPES:
        if any(k in haystack for k in keywords):
            if reason_before is not None or title is None:
                audit.append(AuditEntry(field="doc_type", action="inferred", before=reason_before, after=canonical,
                                        reason="Document type missing; inferred from note text", source=source))
            return canonical, category, loinc, display, title or canonical, audit

    canonical, category, loinc, display = _GENERIC_NOTE
    if title is None:
        audit.append(AuditEntry(field="doc_type", action="inferred", before=None, after=canonical,
                                reason="Document type missing and could not be inferred", source=source))
    return canonical, category, loinc, display, title or canonical, audit


# ---------------------------------------------------------------------------------------
# Lab values
# ---------------------------------------------------------------------------------------
_NUMERIC = re.compile(r"^\s*(<=|>=|<|>)?\s*(-?\d+(?:\.\d+)?)\s*$")
_RANGE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*$")
_BOUND = re.compile(r"^\s*(<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)\s*$")
_FLAG_MAP: dict[str, Interpretation] = {
    "h": "H", "high": "H", "hi": "H", "l": "L", "low": "L", "lo": "L",
    "n": "N", "normal": "N", "a": "A", "abnormal": "A",
    "hh": "HH", "ll": "LL", "crit": "A", "critical": "A", "c": "A",
}


def _range_interpretation(value: float, ref_range: str | None) -> Interpretation | None:
    if not ref_range:
        return None
    if m := _RANGE.match(ref_range):
        low, high = float(m.group(1)), float(m.group(2))
        return "L" if value < low else "H" if value > high else "N"
    if m := _BOUND.match(ref_range):
        op, bound = m.group(1), float(m.group(2))
        if op.startswith("<"):
            return "H" if value >= bound and not (op == "<=" and value == bound) else "N"
        return "L" if value <= bound and not (op == ">=" and value == bound) else "N"
    return None


def parse_lab_result(test: str, loinc: str | None, result: str | None, unit: str | None,
                     ref_range: str | None, flag: str | None, *, source: str | None = None
                     ) -> tuple[LabResult | None, list[AuditEntry]]:
    audit: list[AuditEntry] = []
    if not result:
        audit.append(AuditEntry(field=f"result[{test}]", action="dropped", reason="Lab row has no result value",
                                source=source))
        return None, audit

    numeric, comparator = None, None
    if m := _NUMERIC.match(result.replace(",", "")):
        comparator = m.group(1)  # type: ignore[assignment]
        numeric = float(m.group(2))

    interpretation: Interpretation | None = None
    computed = _range_interpretation(numeric, ref_range) if numeric is not None and comparator is None else None
    if flag:
        interpretation = _FLAG_MAP.get(flag.strip().lower())
        if interpretation is None:
            audit.append(AuditEntry(field=f"flag[{test}]", action="flagged", before=flag, after=None,
                                    reason="Unrecognised abnormal flag", source=source))
        elif flag.strip().lower() in ("crit", "critical", "c"):
            # Critical: use the reference range to decide direction (HH / LL).
            if computed in ("H", "L"):
                interpretation = "HH" if computed == "H" else "LL"
            audit.append(AuditEntry(field=f"flag[{test}]", action="normalized", before=flag, after=interpretation,
                                    reason="Critical flag mapped to HL7 v3 ObservationInterpretation", source=source))
        elif flag.strip() != interpretation:
            audit.append(AuditEntry(field=f"flag[{test}]", action="normalized", before=flag, after=interpretation,
                                    reason="Flag mapped to HL7 v3 ObservationInterpretation", source=source))
    elif computed is not None:
        interpretation = computed
        if computed != "N":
            audit.append(AuditEntry(field=f"flag[{test}]", action="inferred", before=None, after=computed,
                                    reason="Abnormal flag missing; derived from reference range", source=source))

    return LabResult(
        test_name=test, loinc=loinc or None, value_raw=result, value_numeric=numeric, comparator=comparator,
        unit=unit or None, reference_range=ref_range or None, interpretation=interpretation,
    ), audit


ENCOUNTER_CLASS_MAP = {
    "inpatient": "IMP", "imp": "IMP", "admission": "IMP",
    "emergency": "EMER", "emer": "EMER", "ed": "EMER",
    "ambulatory": "AMB", "amb": "AMB", "outpatient": "AMB", "clinic": "AMB",
}
