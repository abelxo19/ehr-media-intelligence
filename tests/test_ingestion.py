"""Loader + end-to-end cleaning pipeline tests against the synthetic messy export."""

import json
from datetime import date, datetime

from app.ingestion.loaders import load_csv, load_directory, load_file, load_json, load_text
from app.ingestion.models import RawBatch, RawDocument, RawPatient
from app.ingestion.pipeline import clean_batch
from tests.conftest import RAW_DIR


def _patient(dataset, pid):
    return next(p for p in dataset.patients if p.patient_id == pid)


def _record(dataset, rid):
    return next(r for r in dataset.records if r.record_id == rid)


# ---------------------------------------------------------------- loaders (>= 2 formats)
def test_loaders_parse_all_three_formats():
    j, c, t = load_json(RAW_DIR / "ehr_export.json"), load_csv(RAW_DIR / "lab_results.csv"), load_text(
        RAW_DIR / "scanned_notes.txt")
    assert len(j.patients) == 10 and len(j.documents) == 27
    assert len(c.lab_rows) == 32 and c.lab_rows[0].loinc == "2345-7"
    assert len(t.documents) == 13 and t.documents[0].patient.mrn == "112234"
    assert not (j.errors or c.errors or t.errors)


def test_loader_reports_bad_input_instead_of_crashing(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert "invalid JSON" in load_json(bad).errors[0]
    assert "unsupported" in load_file(tmp_path / "scan.pdf").errors[0]


def test_json_loader_accepts_key_aliases(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"patients": [{"MRN": "7", "patient_name": "A B", "birthDate": "2000-01-01",
                                              "Sex": "F"}]}), encoding="utf-8")
    p = load_json(path).patients[0]
    assert (p.mrn, p.name, p.dob, p.gender) == ("7", "A B", "2000-01-01", "F")


# ---------------------------------------------------------------- dataset-level
def test_dataset_shape(dataset):
    assert len(dataset.patients) == 12
    assert len(dataset.records) >= 50
    assert all(p.mrn.isdigit() and len(p.mrn) == 7 for p in dataset.patients)
    enc_ids = {e.encounter_id for e in dataset.encounters}
    assert all(r.encounter_id in enc_ids for r in dataset.records)


def test_duplicate_patient_export_is_merged(dataset):
    marias = [p for p in dataset.patients if p.family_name == "Gonzalez"]
    assert len(marias) == 1
    assert any(a.action == "deduplicated" for a in marias[0].audit)
    assert len(marias[0].sources) >= 3  # json x2, csv, txt


def test_conflicting_mrn_is_linked_as_alternate_and_flagged(dataset):
    robert = _patient(dataset, "pt-0045678")
    assert robert.alternate_mrns == ["0045687"]
    assert "possible_duplicate_mrn" in robert.flags
    # Labs filed under the typo MRN still end up on the right patient
    assert _record(dataset, "LAB-ACC-5301").patient_id == "pt-0045678"


def test_lab_row_without_mrn_is_matched_on_name_and_dob(dataset):
    assert _record(dataset, "LAB-ACC-5902").patient_id == "pt-0123345"


def test_name_variant_for_same_mrn_is_audited_not_overwritten(dataset):
    linda = _patient(dataset, "pt-0034567")
    assert linda.given_name == "Linda"
    assert any(a.action == "flagged" and "Name variant" in a.reason for a in linda.audit)


def test_missing_gender_filled_from_another_source(dataset):
    grace = _patient(dataset, "pt-0099012")
    assert grace.gender == "female"
    assert any(a.field == "gender" and a.action == "filled" for a in grace.audit)


def test_duplicate_records_and_rows_are_removed(dataset):
    ids = [r.record_id for r in dataset.records]
    assert len(ids) == len(set(ids))
    assert ids.count("D-1002-A") == 1
    a1c = _record(dataset, "LAB-ACC-5002")
    assert len(a1c.results) == 1
    assert dataset.stats["duplicates_removed"] == 2


def test_empty_document_is_dropped(dataset):
    assert "D-5001-C" not in {r.record_id for r in dataset.records}
    assert any(a.action == "dropped" and "Empty" in a.reason for a in dataset.pipeline_audit)


def test_future_date_cleared_and_filled_from_encounter(dataset):
    rec = _record(dataset, "D-7001-C")
    assert rec.record_date == datetime(2026, 8, 9)
    actions = [(a.field, a.action) for a in rec.audit]
    assert ("record_date", "flagged") in actions and ("record_date", "filled") in actions


def test_missing_date_filled_from_encounter_end(dataset):
    rec = _record(dataset, "D-4001-A")
    assert rec.record_date == datetime(2025, 12, 6)


def test_missing_type_inferred(dataset):
    rec = _record(dataset, "D-5001-A")
    assert rec.category == "imaging"
    assert any(a.field == "doc_type" and a.action == "inferred" for a in rec.audit)


def test_records_without_encounter_get_linked_or_synthesized(dataset):
    lab = _record(dataset, "LAB-ACC-5001")  # collected during Maria's admission
    assert lab.encounter_id == "ENC-1001"
    scan = next(r for r in dataset.records if r.doc_type == "Imaging Report" and r.title == "DEXA Scan")
    enc = next(e for e in dataset.encounters if e.encounter_id == scan.encounter_id)
    assert enc.synthesized and enc.class_code == "AMB"


def test_every_audit_entry_is_attached_to_a_source(dataset):
    for r in dataset.records:
        for a in r.audit:
            assert a.source, (r.record_id, a)


def test_same_mrn_with_different_dob_is_flagged_as_identifier_conflict():
    p1 = RawPatient(source="a", mrn="1", name="Doe, Jane", dob="1990-01-01", gender="F")
    p2 = RawPatient(source="b", mrn="0000001", name="Doe, Jane", dob="1991-02-02", gender="F")
    batch = RawBatch(patients=[p1, p2], documents=[RawDocument(source="b#1", patient=p2, doc_type="Progress Note",
                                                               date="2026-01-01", text="Seen in clinic.")])
    ds = clean_batch(batch, today=date(2026, 11, 20))
    assert len(ds.patients) == 1
    assert "identifier_conflict" in ds.patients[0].flags


def test_unidentifiable_patient_is_dropped_not_guessed():
    p = RawPatient(source="x", mrn=None, name="Nobody, Known", dob="1990-01-01")
    batch = RawBatch(patients=[p], documents=[RawDocument(source="x#1", patient=p, text="note", date="2026-01-01")])
    ds = clean_batch(batch, today=date(2026, 11, 20))
    assert ds.patients == [] and ds.records == []
    assert ds.stats["items_dropped"] >= 1


def test_load_directory_orders_json_first():
    batch = load_directory(RAW_DIR)
    assert batch.patients[0].source.startswith("ehr_export.json")
