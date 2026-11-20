"""FastAPI endpoints (Task 4) using a lightweight hashing embedder, plus an end-to-end
test with the real all-MiniLM-L6-v2 model for relevance and the <2 s latency target."""

import time

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.summarization.summarizer import RuleBasedSummarizer


def search(client, query, **params):
    return client.post("/search", params=params, json={"query": query})


def test_search_returns_top5_ranked_with_scores(client):
    r = search(client, "potassium creatinine")
    assert r.status_code == 200
    body = r.json()
    results = body["results"]
    assert 1 <= len(results) <= 5
    assert [x["rank"] for x in results] == list(range(1, len(results) + 1))
    scores = [x["score"] for x in results]
    assert scores == sorted(scores, reverse=True)
    first = results[0]
    for key in ("patient_name", "mrn", "record_date", "resource_type", "score", "snippet", "summary_snippet"):
        assert key in first
    assert body["took_ms"] < 2000


def test_filter_by_resource_type(client):
    for rtype in ("DiagnosticReport", "DocumentReference", "Patient"):
        results = search(client, "heart", resource_type=rtype).json()["results"]
        assert results and all(x["resource_type"] == rtype for x in results)


def test_multiple_resource_types(client):
    r = client.post("/search", params=[("resource_type", "DiagnosticReport"), ("resource_type", "Patient")],
                    json={"query": "kidney"})
    assert {x["resource_type"] for x in r.json()["results"]} <= {"DiagnosticReport", "Patient"}


def test_filter_by_date_range(client):
    results = search(client, "imaging report", date_from="2026-04-01", date_to="2026-06-30").json()["results"]
    assert results
    assert all("2026-04-01" <= x["record_date"] <= "2026-06-30" for x in results)


def test_date_range_with_no_matches_returns_empty(client):
    body = search(client, "anything", date_from="1990-01-01", date_to="1990-12-31").json()
    assert body["results"] == [] and body["candidates"] == 0


def test_top_k_parameter(client):
    assert len(search(client, "note", top_k=10).json()["results"]) == 10


@pytest.mark.parametrize("params, body", [
    ({"date_from": "2026-05-01", "date_to": "2026-01-01"}, {"query": "x"}),
    ({}, {"query": ""}),
    ({}, {"query": "   "}),
    ({"resource_type": "Banana"}, {"query": "x"}),
    ({"top_k": 0}, {"query": "x"}),
    ({"date_from": "not-a-date"}, {"query": "x"}),
])
def test_invalid_requests_are_rejected(client, params, body):
    assert client.post("/search", params=params, json=body).status_code == 422


def test_patients_list(client):
    patients = client.get("/patients").json()
    assert len(patients) == 12
    assert all({"patient_id", "mrn", "display_name", "record_count"} <= set(p) for p in patients)


def test_patient_detail_has_summary_and_linked_resources(client):
    body = client.get("/patients/pt-0045678").json()
    assert body["patient"]["alternate_mrns"] == ["0045687"]
    summary = body["summary"]
    assert summary["disclaimer"] and summary["chief_concern"] and summary["word_count"] <= 200
    types = {r["resource_type"] for r in body["resources"]}
    assert {"Patient", "Encounter", "DocumentReference", "DiagnosticReport", "Observation"} <= types
    assert body["validation"]["valid"] is True


def test_patient_bundle_and_single_resource(client):
    r = client.get("/patients/pt-0023456/bundle")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/fhir+json")
    bundle = r.json()
    rid = bundle["entry"][1]["resource"]["id"]
    res = client.get(f"/patients/pt-0023456/resources/{rid}")
    assert res.json()["id"] == rid


def test_summary_endpoint_uses_cache(client):
    a = client.get("/patients/pt-0012345/summary").json()
    b = client.get("/patients/pt-0012345/summary").json()
    assert a["generated_at"] == b["generated_at"]


@pytest.mark.parametrize("path", ["/patients/nope", "/patients/nope/bundle", "/patients/nope/summary",
                                  "/patients/pt-0023456/resources/nope"])
def test_not_found(client, path):
    assert client.get(path).status_code == 404


def test_reports_and_health(client):
    report = client.get("/reports/validation").json()
    assert report["valid_bundles"] == 12 and report["total_errors"] == 0
    audit = client.get("/reports/audit").json()
    assert audit["stats"]["records"] >= 50
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["indexed_documents"] >= 60


# ---------------------------------------------------------------- real model (end to end)
@pytest.fixture(scope="module")
def real_client(tmp_path_factory):
    from app.config import Settings
    from app.search.embedder import get_embedder
    from tests.conftest import RAW_DIR

    settings = Settings(raw_data_dir=RAW_DIR, var_dir=tmp_path_factory.mktemp("real"), summarizer="rule-based")
    try:
        embedder = get_embedder(settings.embedding_model)
    except Exception as exc:  # pragma: no cover - offline machine without the cached model
        pytest.skip(f"sentence-transformers model unavailable: {exc}")
    with TestClient(create_app(settings, embedder=embedder, summarizer=RuleBasedSummarizer())) as c:
        yield c


@pytest.mark.parametrize("query, patient", [
    ("heart attack", "pt-0023456"),            # NSTEMI - no shared keyword
    ("critical potassium", "pt-0089012"),
    ("lung cancer screening", "pt-0067890"),
    ("breast cancer biopsy", "pt-0099012"),
    ("broken wrist", "pt-0123345"),
    ("thyroid", "pt-0112234"),
])
def test_semantic_relevance(real_client, query, patient):
    results = search(real_client, query).json()["results"]
    assert results[0]["patient_id"] == patient, [(r["patient_id"], r["title"], r["score"]) for r in results]


def test_latency_under_two_seconds_for_50_plus_records(real_client):
    assert real_client.get("/health").json()["indexed_documents"] >= 50
    search(real_client, "warm up")
    worst = 0.0
    for q in ["chest pain", "kidney injury", "abnormal mammogram", "asthma exacerbation", "stroke"]:
        t = time.perf_counter()
        r = search(real_client, q, resource_type="DiagnosticReport", date_from="2025-01-01")
        worst = max(worst, time.perf_counter() - t)
        assert r.status_code == 200
    assert worst < 2.0, f"slowest request took {worst:.3f}s"
