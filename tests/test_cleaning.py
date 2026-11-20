"""Unit tests for the pure normalization functions (Task 1 edge cases)."""

from datetime import date, datetime

import pytest

from app.ingestion import cleaning as c

TODAY = date(2026, 11, 20)


# ---------------------------------------------------------------- dates
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-02-11", datetime(2026, 2, 11)),
        ("02/14/2026", datetime(2026, 2, 14)),
        ("March 2, 2026", datetime(2026, 3, 2)),
        ("15-Jan-2026", datetime(2026, 1, 15)),
        ("2026/02/10 06:15", datetime(2026, 2, 10, 6, 15)),
        ("2026-05-18T07:42:00", datetime(2026, 5, 18, 7, 42)),
        ("20260211", datetime(2026, 2, 11)),
        ("  02-10-2026 ", datetime(2026, 2, 10)),
    ],
)
def test_inconsistent_date_formats_normalize_to_iso(raw, expected):
    parsed, audit = c.parse_datetime(raw, field="d", today=TODAY)
    assert parsed == expected
    if raw.strip() not in (expected.date().isoformat(), expected.isoformat()):
        assert audit and audit[0].action == "normalized"


def test_ambiguous_slash_date_assumes_us_and_is_audited():
    parsed, audit = c.parse_datetime("04/05/2026", field="d", today=TODAY)
    assert parsed == datetime(2026, 4, 5)
    assert any("Ambiguous" in a.reason for a in audit)


def test_day_first_date_is_swapped_and_flagged():
    parsed, audit = c.parse_datetime("25/12/2025", field="d", today=TODAY)
    assert parsed == datetime(2025, 12, 25)
    assert audit[0].action == "flagged" and "DD/MM" in audit[0].reason


@pytest.mark.parametrize("raw, reason", [("08/10/2062", "future"), ("not a date", "Unparseable"),
                                         ("01/01/1850", "Implausible")])
def test_impossible_dates_are_cleared_and_flagged(raw, reason):
    parsed, audit = c.parse_datetime(raw, field="d", today=TODAY)
    assert parsed is None
    assert audit[-1].action == "flagged" and reason in audit[-1].reason


def test_missing_date_returns_none_without_audit():
    assert c.parse_datetime(None, field="d") == (None, [])
    assert c.parse_datetime("   ", field="d") == (None, [])


# ---------------------------------------------------------------- gender
@pytest.mark.parametrize("raw, expected", [("M", "male"), ("f", "female"), ("1", "male"), ("2", "female"),
                                           ("Female", "female"), ("X", "other"), ("male", "male")])
def test_gender_codes_map_to_fhir_administrative_gender(raw, expected):
    value, audit = c.normalize_gender(raw)
    assert value == expected
    assert bool(audit) == (raw != expected)


def test_missing_gender_defaults_to_unknown_with_audit():
    value, audit = c.normalize_gender("")
    assert value == "unknown" and audit[0].action == "filled"


def test_unrecognised_gender_is_flagged():
    value, audit = c.normalize_gender("q")
    assert value == "unknown" and audit[0].action == "flagged"


# ---------------------------------------------------------------- MRN
@pytest.mark.parametrize("raw", ["MRN-0012345", "12345", "00-12345", "mrn 0012345", "0012345"])
def test_mrn_variants_collapse_to_one_canonical_form(raw):
    mrn, audit = c.normalize_mrn(raw)
    assert mrn == "0012345"
    assert bool(audit) == (raw != "0012345")


@pytest.mark.parametrize("raw", ["ABC", "123456789012"])
def test_invalid_mrn_is_flagged(raw):
    mrn, audit = c.normalize_mrn(raw)
    assert mrn is None and audit[0].action == "flagged"


def test_missing_mrn_is_flagged_for_demographic_matching():
    mrn, audit = c.normalize_mrn(None)
    assert mrn is None and "name + DOB" in audit[0].reason


# ---------------------------------------------------------------- names / text
@pytest.mark.parametrize("raw, given, family", [
    ("GONZALEZ, MARIA", "Maria", "Gonzalez"),
    ({"given": "Linda", "family": "Chen"}, "Linda", "Chen"),
    ("Kevin Nguyen", "Kevin", "Nguyen"),
    ("o'brien, mary-kate", "Mary-Kate", "O'Brien"),
])
def test_name_formats(raw, given, family):
    g, f, _ = c.normalize_name(raw)
    assert (g, f) == (given, family)


def test_ocr_hyphenation_is_rejoined_but_real_hyphens_kept():
    text, audit = c.clean_text("primary hypo-\nthyroidism on ICS-\nformoterol and  snow-\n  boarding")
    assert text == "primary hypothyroidism on ICS-formoterol and snowboarding"
    assert audit and audit[0].field == "text"


# ---------------------------------------------------------------- lab values
def test_critical_flag_uses_reference_range_for_direction():
    result, audit = c.parse_lab_result("Potassium", "2823-3", "6.3", "mmol/L", "3.5-5.1", "CRIT")
    assert result.interpretation == "HH" and result.value_numeric == 6.3
    assert audit[0].after == "HH"


def test_missing_flag_is_derived_from_reference_range():
    result, audit = c.parse_lab_result("eGFR", None, "27", "mL/min", ">60", None)
    assert result.interpretation == "L"
    assert audit[0].action == "inferred"


def test_comparator_and_text_results():
    lt, _ = c.parse_lab_result("Troponin", None, "<5", "ng/L", "<20", None)
    assert lt.comparator == "<" and lt.value_numeric == 5.0
    txt, _ = c.parse_lab_result("hCG", None, "Negative", None, "Negative", "")
    assert txt.value_numeric is None and txt.value_raw == "Negative"


def test_lab_row_without_result_is_dropped():
    result, audit = c.parse_lab_result("Sodium", None, "", "mmol/L", None, None)
    assert result is None and audit[0].action == "dropped"


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("doc_type, category, loinc", [
    ("Discharge Summary", "clinical_note", "18842-5"),
    ("ED Note", "clinical_note", "34111-5"),
    ("CT Chest", "imaging", "18748-4"),
    ("Pathology Report", "pathology", "11526-1"),
    ("Cardiac Catheterization Report", "cardiology", "18745-0"),
])
def test_document_types_map_to_category_and_loinc(doc_type, category, loinc):
    _, cat, code, _, _, audit = c.classify_document(doc_type, "text")
    assert (cat, code) == (category, loinc) and audit == []


def test_missing_document_type_is_inferred_from_text():
    canonical, cat, _, _, _, audit = c.classify_document(None, "MRI brain with contrast. Normal.")
    assert (canonical, cat) == ("Imaging Report", "imaging")
    assert audit[0].action == "inferred"
