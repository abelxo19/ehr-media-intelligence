"""FastAPI service: semantic search, patient detail, FHIR bundles and reports.

Run:  uvicorn app.api.main:app --reload     (then open http://127.0.0.1:8000)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT, Settings, get_settings
from app.pipeline import run_pipeline
from app.search.embedder import Embedder, get_embedder
from app.search.index import VectorIndex
from app.store import Store
from app.summarization.summarizer import ClinicalSummary, Summarizer, SummaryService, build_summarizer

log = logging.getLogger(__name__)
FRONTEND_DIR = PROJECT_ROOT / "frontend"
ResourceType = Literal["DocumentReference", "DiagnosticReport", "Patient"]


# ---------------------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500, examples=["critical potassium"])


class SearchResult(BaseModel):
    rank: int
    score: float = Field(description="Hybrid relevance: 0.8 x cosine similarity + 0.2 x keyword overlap (0-1)")
    semantic_score: float = Field(description="Cosine similarity of query and best-matching chunk")
    keyword_score: float = Field(description="Fraction of query terms found in the record")
    kind: Literal["record", "ai_summary"]
    record_id: str
    resource_type: str
    fhir_id: str
    patient_id: str
    patient_name: str
    mrn: str
    record_date: str | None
    title: str
    category: str
    snippet: str
    summary_snippet: str | None


class SearchResponse(BaseModel):
    query: str
    filters: dict[str, Any]
    candidates: int
    took_ms: float
    results: list[SearchResult]


class AppState:
    def __init__(self, settings: Settings, embedder: Embedder, summarizer: Summarizer | None) -> None:
        self.settings = settings
        self.embedder = embedder
        self.summarizer = summarizer
        self.store = Store(settings.db_path)
        self.index: VectorIndex | None = None
        self.patients: dict[str, dict] = {}

    def load(self, rebuild: bool = False) -> None:
        index_ok = (self.settings.index_dir / "meta.json").exists()
        if rebuild or not self.store.is_populated() or not index_ok:
            log.info("Building data store (rebuild=%s)", rebuild)
            run_pipeline(self.settings, embedder=self.embedder, summarizer=self.summarizer)
        try:
            self.index = VectorIndex.load(self.settings.index_dir, self.embedder)
        except ValueError:
            run_pipeline(self.settings, embedder=self.embedder, summarizer=self.summarizer)
            self.index = VectorIndex.load(self.settings.index_dir, self.embedder)
        self.patients = {p["patient_id"]: p for p in self.store.list_patients()}

    def summary_for(self, patient_id: str) -> dict | None:
        bundle = self.store.get_bundle(patient_id)
        return self.store.latest_summary(patient_id, bundle["content_hash"] if bundle else None)


def create_app(settings: Settings | None = None, *, embedder: Embedder | None = None,
               summarizer: Summarizer | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        emb = embedder or get_embedder(settings.embedding_model)
        state = AppState(settings, emb, summarizer)
        state.load()
        app.state.ehr = state
        yield

    app = FastAPI(
        title="EHR Media Intelligence API",
        version="1.0.0",
        description="Search and summarize synthetic EHR media records normalized to HL7 FHIR R4.",
        lifespan=lifespan,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    def st(request: Request) -> AppState:
        return request.app.state.ehr

    # -- search --------------------------------------------------------------------------
    @app.post("/search", response_model=SearchResponse, tags=["search"])
    def search(
        request: Request,
        body: SearchRequest,
        resource_type: Annotated[list[ResourceType] | None, Query(
            description="Filter by FHIR resource type (repeatable). 'Patient' = AI patient summaries")] = None,
        date_from: Annotated[date | None, Query(description="Inclusive, YYYY-MM-DD")] = None,
        date_to: Annotated[date | None, Query(description="Inclusive, YYYY-MM-DD")] = None,
        top_k: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> SearchResponse:
        state = st(request)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        query = body.query.strip()
        if not query:
            raise HTTPException(status_code=422, detail="query must not be blank")
        assert state.index is not None
        hits, candidates, took = state.index.search(
            query, top_k=top_k, resource_types=set(resource_type) if resource_type else None,
            date_from=date_from, date_to=date_to,
        )
        results = []
        for rank, h in enumerate(hits, start=1):
            patient = state.patients.get(h.doc.patient_id, {})
            summary = state.summary_for(h.doc.patient_id)
            results.append(SearchResult(
                rank=rank, score=round(h.score, 4), semantic_score=round(h.semantic_score, 4),
                keyword_score=round(h.keyword_score, 4), kind=h.doc.kind, record_id=h.doc.doc_id,  # type: ignore[arg-type]
                resource_type=h.doc.resource_type, fhir_id=h.doc.fhir_id, patient_id=h.doc.patient_id,
                patient_name=patient.get("display_name", "Unknown"), mrn=patient.get("mrn", ""),
                record_date=h.doc.record_date, title=h.doc.title, category=h.doc.category,
                snippet=_truncate(h.chunk, 60),
                summary_snippet=_truncate(summary["chief_concern"] + " " + summary["narrative"], 45) if summary else None,
            ))
        return SearchResponse(
            query=query,
            filters={"resource_type": resource_type or [], "date_from": date_from, "date_to": date_to},
            candidates=candidates, took_ms=round(took, 2), results=results,
        )

    # -- patients ------------------------------------------------------------------------
    @app.get("/patients", tags=["patients"])
    def list_patients(request: Request) -> list[dict]:
        return list(st(request).patients.values())

    @app.get("/patients/{patient_id}", tags=["patients"])
    def patient_detail(request: Request, patient_id: str) -> dict:
        state = st(request)
        patient = state.store.get_patient(patient_id)
        bundle_row = state.store.get_bundle(patient_id)
        if patient is None or bundle_row is None:
            raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
        return {
            "patient": patient,
            "summary": state.summary_for(patient_id),
            "resources": _linked_resources(bundle_row["bundle"]),
            "validation": bundle_row["validation"],
        }

    @app.get("/patients/{patient_id}/summary", response_model=ClinicalSummary, tags=["patients"])
    def patient_summary(request: Request, patient_id: str, refresh: bool = False) -> ClinicalSummary:
        state = st(request)
        bundle_row = state.store.get_bundle(patient_id)
        if bundle_row is None:
            raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
        summarizer = state.summarizer or build_summarizer(state.settings.summarizer, state.settings.claude_model)
        service = SummaryService(state.store, summarizer)
        return service.summarize(patient_id, bundle_row["bundle"], bundle_row["content_hash"], force=refresh)

    @app.get("/patients/{patient_id}/bundle", tags=["fhir"])
    def patient_bundle(request: Request, patient_id: str) -> JSONResponse:
        row = st(request).store.get_bundle(patient_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
        return JSONResponse(row["bundle"], media_type="application/fhir+json")

    @app.get("/patients/{patient_id}/resources/{resource_id}", tags=["fhir"])
    def patient_resource(request: Request, patient_id: str, resource_id: str) -> JSONResponse:
        row = st(request).store.get_bundle(patient_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
        for entry in row["bundle"]["entry"]:
            if entry["resource"]["id"] == resource_id:
                return JSONResponse(entry["resource"], media_type="application/fhir+json")
        raise HTTPException(status_code=404, detail=f"Resource {resource_id} not in bundle")

    # -- reports / ops -------------------------------------------------------------------
    @app.get("/reports/validation", tags=["reports"])
    def validation_report(request: Request) -> JSONResponse:
        return _json_file(st(request).settings.reports_dir / "validation_report.json")

    @app.get("/reports/audit", tags=["reports"])
    def audit_report(request: Request) -> JSONResponse:
        return _json_file(st(request).settings.reports_dir / "audit_log.json")

    @app.get("/health", tags=["ops"])
    def health(request: Request) -> dict:
        state = st(request)
        return {"status": "ok", "patients": len(state.patients), "indexed_documents": state.index.size if state.index else 0,
                "embedder": state.embedder.name, "last_run": state.store.get_meta("last_run")}

    @app.post("/admin/rebuild", tags=["ops"])
    def rebuild(request: Request) -> dict:
        state = st(request)
        state.load(rebuild=True)
        return state.store.get_meta("last_run")

    # -- frontend ------------------------------------------------------------------------
    if FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

        @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "index.html")

    return app


def _truncate(text: str, words: int) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + ("..." if len(parts) > words else "")


def _json_file(path: Path) -> JSONResponse:
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not generated yet")
    import json
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


def _linked_resources(bundle: dict) -> list[dict]:
    """Flatten a Bundle into a list the UI can render (id, type, title, date, status)."""
    out = []
    for entry in bundle["entry"]:
        r = entry["resource"]
        rtype = r["resourceType"]
        if rtype == "Patient":
            title, when = "Patient demographics", r.get("birthDate")
        elif rtype == "Encounter":
            title = f"{r['class']['display'].title()} encounter ({r['identifier'][0]['value']})"
            when = (r.get("period") or {}).get("start")
        elif rtype == "DocumentReference":
            title, when = r["type"].get("text"), r.get("date")
        elif rtype == "DiagnosticReport":
            title, when = r["code"].get("text"), r.get("effectiveDateTime")
        elif rtype == "Observation":
            value = r.get("valueQuantity")
            shown = f"{value.get('comparator', '')}{value['value']:g} {value.get('unit', '')}" if value else r.get("valueString", "")
            interp = r.get("interpretation", [{}])[0].get("coding", [{}])[0].get("code")
            title = f"{r['code'].get('text')}: {shown.strip()}" + (f" ({interp})" if interp and interp != "N" else "")
            when = r.get("effectiveDateTime")
        else:
            title, when = rtype, None
        out.append({"resource_type": rtype, "id": r["id"], "title": title, "date": (when or "")[:10] or None,
                    "status": r.get("status")})
    return out


app = create_app()
