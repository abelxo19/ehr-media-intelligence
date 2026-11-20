"""FHIR R4 mapping, validation report and SQLite store (Task 2)."""

import base64
import copy
import json

import pytest

from app.fhir.mapper import build_all_bundles, bundle_content_hash, resource_index
from app.fhir.validator import build_report, validate_bundle, write_report
from app.store import Store


def _entries(bundle, rtype):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == rtype]


def test_every_bundle_is_valid_r4(bundles):
    for pid, bundle in bundles.items():
        result = validate_bundle(bundle, pid)
        assert result.valid, [i.model_dump() for i in result.errors]


def test_required_resource_types_present(bundles):
    types = {e["resource"]["resourceType"] for b in bundles.values() for e in b["entry"]}
    assert {"Patient", "DocumentReference", "DiagnosticReport", "Encounter", "Observation"} <= types


def test_one_bundle_per_patient_with_single_patient_resource(dataset, bundles):
    assert set(bundles) == {p.patient_id for p in dataset.patients}
    for b in bundles.values():
        assert b["resourceType"] == "Bundle" and b["type"] == "collection"
        assert len(_entries(b, "Patient")) == 1


def test_subject_and_encounter_references_resolve(bundles):
    for b in bundles.values():
        idx = resource_index(b)
        patient_url = f"urn:uuid:{_entries(b, 'Patient')[0]['id']}"
        for r in _entries(b, "DiagnosticReport") + _entries(b, "Observation"):
            assert r["subject"]["reference"] == patient_url
            assert idx[r["encounter"]["reference"]]["resourceType"] == "Encounter"
        for d in _entries(b, "DocumentReference"):
            assert d["subject"]["reference"] == patient_url
            assert idx[d["context"]["encounter"][0]["reference"]]["resourceType"] == "Encounter"


def test_lab_report_links_observations_with_loinc_and_interpretation(bundles):
    b = bundles["pt-0089012"]
    idx = resource_index(b)
    lab = next(r for r in _entries(b, "DiagnosticReport") if r["identifier"][0]["value"] == "LAB-ACC-5501")
    assert lab["category"][0]["coding"][0]["code"] == "LAB"
    obs = [idx[r["reference"]] for r in lab["result"]]
    potassium = next(o for o in obs if o["code"]["coding"][0]["code"] == "2823-3")
    assert potassium["valueQuantity"] == {"value": 6.3, "unit": "mmol/L", "system": "http://unitsofmeasure.org",
                                          "code": "mmol/L"}
    assert potassium["interpretation"][0]["coding"][0]["code"] == "HH"


def test_document_reference_carries_note_text_and_loinc_type(bundles):
    doc = next(d for d in _entries(bundles["pt-0012345"], "DocumentReference")
               if d["masterIdentifier"]["value"] == "D-1001-A")
    assert doc["type"]["coding"][0]["code"] == "18842-5"
    text = base64.b64decode(doc["content"][0]["attachment"]["data"]).decode()
    assert "hyperglycemia" in text
    assert doc["date"].endswith(("-05:00", "-04:00"))  # instant requires a zone


def test_imaging_report_has_conclusion(bundles):
    ct = next(r for r in _entries(bundles["pt-0067890"], "DiagnosticReport")
              if r["identifier"][0]["value"] == "D-6002-A")
    assert ct["category"][0]["coding"][0]["code"] == "RAD"
    assert ct["conclusion"].startswith("Interval growth")


def test_patient_identifiers_and_flags(bundles):
    patient = _entries(bundles["pt-0045678"], "Patient")[0]
    assert [i["value"] for i in patient["identifier"]] == ["0045678", "0045687"]
    assert patient["identifier"][1]["use"] == "secondary"
    assert patient["meta"]["tag"][0]["code"] == "possible_duplicate_mrn"


def test_bundles_are_deterministic(dataset):
    a, b = build_all_bundles(dataset), build_all_bundles(dataset)
    strip = lambda x: [e["fullUrl"] for e in x["entry"]]  # noqa: E731
    assert all(strip(a[k]) == strip(b[k]) for k in a)


# ---------------------------------------------------------------- validator catches problems
@pytest.fixture()
def broken(bundles):
    return copy.deepcopy(bundles["pt-0023456"])


def test_validator_reports_schema_errors_with_location(broken):
    patient = _entries(broken, "Patient")[0]
    patient["gender"] = "M"  # not in AdministrativeGender
    report = validate_bundle(broken)
    assert not report.valid
    err = report.errors[0]
    assert err.resource_type == "Patient" and "gender" in err.location


def test_validator_reports_bad_primitive_format(broken):
    _entries(broken, "DocumentReference")[0]["date"] = "14/02/2026"  # instant must be ISO 8601 with zone
    report = validate_bundle(broken)
    assert any(e.resource_type == "DocumentReference" and "date" in e.location for e in report.errors)


def test_validator_reports_missing_required_element(broken):
    dr = _entries(broken, "DiagnosticReport")[0]
    del dr["status"]
    report = validate_bundle(broken)
    assert any(e.resource_type == "DiagnosticReport" and "status" in e.location for e in report.errors)


def test_validator_reports_dangling_reference(broken):
    _entries(broken, "Observation")[0]["encounter"]["reference"] = "urn:uuid:does-not-exist"
    report = validate_bundle(broken)
    assert any("Unresolved reference" in e.message for e in report.errors)


def test_validator_reports_wrong_subject(broken):
    _entries(broken, "DocumentReference")[0]["subject"]["reference"] = _entries(broken, "Encounter")[0]["id"]
    report = validate_bundle(broken)
    assert any("subject must reference" in e.message for e in report.errors)


def test_validator_warns_on_data_quality_flags(bundles):
    report = validate_bundle(bundles["pt-0045678"])
    assert report.valid and any("possible_duplicate_mrn" in w.message for w in report.warnings)


def test_report_written_as_json_and_markdown(bundles, tmp_path):
    results = [validate_bundle(b, pid) for pid, b in bundles.items()]
    report = build_report(results)
    assert report["valid_bundles"] == report["bundles"] == 12 and report["total_errors"] == 0
    json_path, md_path = write_report(report, tmp_path)
    assert json.loads(json_path.read_text())["bundles"] == 12
    assert "12 / 12 bundles valid" in md_path.read_text()


# ---------------------------------------------------------------- store
def test_store_roundtrip(dataset, bundles, tmp_path):
    store = Store(tmp_path / "t.db")
    p = dataset.patients[0]
    store.replace_clinical_data(
        [{"patient_id": p.patient_id, "mrn": p.mrn, "display_name": p.display_name, "birth_date": None,
          "gender": p.gender, "flags": "[]", "data": p.model_dump_json()}], [])
    h = bundle_content_hash(p, dataset.records_for(p.patient_id))
    store.save_bundle(p.patient_id, bundles[p.patient_id], h, {"valid": True, "issues": []})
    row = store.get_bundle(p.patient_id)
    assert row["bundle"] == bundles[p.patient_id] and row["content_hash"] == h and row["valid"]
    assert store.is_populated()


def test_content_hash_changes_when_records_change(dataset):
    p = dataset.patients[0]
    recs = dataset.records_for(p.patient_id)
    assert bundle_content_hash(p, recs) == bundle_content_hash(p, list(reversed(recs)))
    assert bundle_content_hash(p, recs) != bundle_content_hash(p, recs[:-1])
