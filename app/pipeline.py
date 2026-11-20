"""End-to-end build: raw files -> clean IR -> FHIR Bundles -> validation report -> SQLite
-> AI summaries -> vector index.

    python -m app.pipeline                 # summarizer chosen by EHR_SUMMARIZER (default: auto)
    python -m app.pipeline --summarizer claude --force-summaries
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.fhir.mapper import build_all_bundles, bundle_content_hash, fhir_id
from app.fhir.validator import build_report, validate_bundle, write_report
from app.ingestion.models import CleanDataset
from app.ingestion.pipeline import run_ingestion
from app.search.embedder import Embedder, get_embedder
from app.search.index import IndexedDoc, VectorIndex
from app.store import Store
from app.summarization.summarizer import ClinicalSummary, Summarizer, SummaryService, build_summarizer

log = logging.getLogger(__name__)


def write_audit_log(dataset: CleanDataset, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "audit_log.json"
    payload = {
        "stats": dataset.stats,
        "pipeline_events": [a.model_dump() for a in dataset.pipeline_audit],
        "patients": {p.patient_id: {"flags": p.flags, "alternate_mrns": p.alternate_mrns,
                                    "audit": [a.model_dump() for a in p.audit]} for p in dataset.patients},
        "records": {r.record_id: [a.model_dump() for a in r.audit] for r in dataset.records},
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def summary_index_text(s: ClinicalSummary) -> str:
    parts = [f"Chief concern: {s.chief_concern}", "Diagnoses: " + "; ".join(s.key_diagnoses)]
    parts += [f"{m.type}: {m.finding}" for m in s.recent_media]
    parts += [f"Flag: {a.finding}" for a in s.flagged_anomalies]
    parts.append(s.narrative)
    return " ".join(parts)


def run_pipeline(settings: Settings | None = None, *, embedder: Embedder | None = None,
                 summarizer: Summarizer | None = None, today: date | None = None,
                 force_summaries: bool = False) -> dict[str, Any]:
    settings = settings or get_settings()
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    # 1. Ingest + clean
    dataset = run_ingestion(settings.raw_data_dir, today=today)
    audit_path = write_audit_log(dataset, settings.reports_dir)
    timings["ingest_s"] = time.perf_counter() - t0

    # 2. FHIR mapping + validation
    t = time.perf_counter()
    bundles = build_all_bundles(dataset)
    results = {pid: validate_bundle(b, pid) for pid, b in bundles.items()}
    report = build_report(list(results.values()))
    report_json, report_md = write_report(report, settings.reports_dir)
    timings["fhir_s"] = time.perf_counter() - t

    # 3. Persist
    store = Store(settings.db_path)
    store.replace_clinical_data(
        patients=[{
            "patient_id": p.patient_id, "mrn": p.mrn, "display_name": p.display_name,
            "birth_date": p.birth_date.isoformat() if p.birth_date else None, "gender": p.gender,
            "flags": json.dumps(p.flags), "data": p.model_dump_json(),
        } for p in dataset.patients],
        records=[{
            "record_id": r.record_id, "patient_id": r.patient_id,
            "fhir_id": fhir_id(r.fhir_resource_type, r.record_id), "resource_type": r.fhir_resource_type,
            "category": r.category, "doc_type": r.doc_type, "title": r.title,
            "record_date": r.record_date.isoformat() if r.record_date else None, "text": r.text,
            "data": r.model_dump_json(),
        } for r in dataset.records],
    )
    hashes = {}
    for p in dataset.patients:
        hashes[p.patient_id] = bundle_content_hash(p, dataset.records_for(p.patient_id))
        store.save_bundle(p.patient_id, bundles[p.patient_id], hashes[p.patient_id],
                          results[p.patient_id].model_dump())

    # 4. Summaries (cached by patient + content hash)
    t = time.perf_counter()
    service = SummaryService(store, summarizer or build_summarizer(settings.summarizer, settings.claude_model),
                             today=today)
    summaries = {pid: service.summarize(pid, bundles[pid], hashes[pid], force=force_summaries) for pid in bundles}
    timings["summaries_s"] = time.perf_counter() - t

    # 5. Vector index over record text + AI summaries
    t = time.perf_counter()
    embedder = embedder or get_embedder(settings.embedding_model)
    last_date = {p.patient_id: max((r.record_date.date().isoformat() for r in dataset.records_for(p.patient_id)
                                    if r.record_date), default=None) for p in dataset.patients}
    docs = [IndexedDoc(
        doc_id=r.record_id, kind="record", resource_type=r.fhir_resource_type,
        fhir_id=fhir_id(r.fhir_resource_type, r.record_id), patient_id=r.patient_id, title=r.title,
        category=r.category, record_date=r.record_date.date().isoformat() if r.record_date else None, text=r.text,
    ) for r in dataset.records]
    docs += [IndexedDoc(
        doc_id=f"summary:{pid}", kind="ai_summary", resource_type="Patient", fhir_id=fhir_id("Patient", pid),
        patient_id=pid, title="AI clinical summary", category="summary", record_date=last_date[pid],
        text=summary_index_text(s),
    ) for pid, s in summaries.items()]
    index = VectorIndex(embedder)
    index.build(docs)
    index.save(settings.index_dir)
    timings["index_s"] = time.perf_counter() - t
    timings["total_s"] = time.perf_counter() - t0

    result = {
        "ingestion": dataset.stats,
        "fhir": {k: report[k] for k in ("bundles", "valid_bundles", "total_errors", "total_warnings",
                                        "resource_totals")},
        "summaries": service.stats | {"generator": service.summarizer.name},
        "index": {"documents": index.size, "chunks": len(index.chunks), "embedder": embedder.name},
        "reports": {"audit_log": str(audit_path), "validation_json": str(report_json),
                    "validation_md": str(report_md)},
        "timings": {k: round(v, 3) for k, v in timings.items()},
    }
    store.set_meta("last_run", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the EHR media intelligence data store.")
    parser.add_argument("--summarizer", choices=["auto", "claude", "rule-based"], default=None)
    parser.add_argument("--force-summaries", action="store_true", help="Ignore the summary cache")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    if args.summarizer:
        settings = Settings(summarizer=args.summarizer)
    print(json.dumps(run_pipeline(settings, force_summaries=args.force_summaries), indent=2))


if __name__ == "__main__":
    main()
