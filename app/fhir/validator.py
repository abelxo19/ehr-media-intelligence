"""Validate FHIR Bundles and produce a human-readable report.

Three layers of checks per Bundle:
1. **Schema** - every resource (and the Bundle itself) is parsed by ``fhir.resources``
   (R4B model set; the resource types used here are structurally identical in R4 4.0.1
   and R4B 4.3.0). The library enforces cardinality, datatypes, required elements and
   primitive formats (date / dateTime / instant / code patterns).
   It does *not* enforce terminology bindings, so ``REQUIRED_BINDINGS`` below checks the
   codes bound with strength "required" in the R4 spec for every element this project emits.
2. **Referential integrity** - every ``reference`` must resolve to a ``fullUrl`` inside
   the Bundle, and ids must match their ``fullUrl``.
3. **Profile-style business rules** - every clinical resource points at this Bundle's
   Patient (``subject``) and an Encounter; data-quality warnings (missing DOB, unknown
   gender, missing dates, flagged identifiers, non-LOINC codes).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from fhir.resources.R4B import get_fhir_model_class
from pydantic import BaseModel, Field, ValidationError

Severity = Literal["error", "warning"]
FHIR_VERSION = "4.0.1-compatible (validated with fhir.resources R4B models)"


class ValidationIssue(BaseModel):
    severity: Severity
    resource_type: str
    resource_id: str | None = None
    location: str
    message: str


class BundleValidationResult(BaseModel):
    patient_id: str
    bundle_id: str
    valid: bool
    resource_counts: dict[str, int]
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


def _schema_issues(resource: dict[str, Any], prefix: str) -> list[ValidationIssue]:
    rtype = resource.get("resourceType", "Unknown")
    try:
        model = get_fhir_model_class(rtype)
    except (KeyError, ValueError):
        return [ValidationIssue(severity="error", resource_type=rtype, resource_id=resource.get("id"),
                                location=prefix, message=f"Unknown FHIR resource type '{rtype}'")]
    try:
        model.model_validate(resource)
    except ValidationError as exc:
        return [
            ValidationIssue(
                severity="error", resource_type=rtype, resource_id=resource.get("id"),
                location=prefix + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err["loc"]),
                message=err["msg"],
            )
            for err in exc.errors()
        ]
    return []


# Element -> allowed codes for value sets bound with strength "required" in FHIR R4.
REQUIRED_BINDINGS: dict[str, dict[str, set[str]]] = {
    "Patient": {"gender": {"male", "female", "other", "unknown"}},
    "Encounter": {"status": {"planned", "arrived", "triaged", "in-progress", "onleave", "finished", "cancelled",
                             "entered-in-error", "unknown"}},
    "DocumentReference": {"status": {"current", "superseded", "entered-in-error"},
                          "docStatus": {"preliminary", "final", "amended", "entered-in-error"}},
    "DiagnosticReport": {"status": {"registered", "partial", "preliminary", "final", "amended", "corrected",
                                    "appended", "cancelled", "entered-in-error", "unknown"}},
    "Observation": {"status": {"registered", "preliminary", "final", "amended", "corrected", "cancelled",
                               "entered-in-error", "unknown"}},
    "Bundle": {"type": {"document", "message", "transaction", "transaction-response", "batch", "batch-response",
                        "history", "searchset", "collection"}},
}
QUANTITY_COMPARATORS = {"<", "<=", ">=", ">"}


def _binding_issues(resource: dict[str, Any], prefix: str) -> list[ValidationIssue]:
    rtype = resource.get("resourceType", "Unknown")
    issues = []
    for element, allowed in REQUIRED_BINDINGS.get(rtype, {}).items():
        value = resource.get(element)
        if value is not None and value not in allowed:
            issues.append(ValidationIssue(severity="error", resource_type=rtype, resource_id=resource.get("id"),
                                          location=f"{prefix}.{element}",
                                          message=f"'{value}' is not in the required value set for {rtype}.{element}"))
    comparator = (resource.get("valueQuantity") or {}).get("comparator")
    if comparator is not None and comparator not in QUANTITY_COMPARATORS:
        issues.append(ValidationIssue(severity="error", resource_type=rtype, resource_id=resource.get("id"),
                                      location=f"{prefix}.valueQuantity.comparator",
                                      message=f"'{comparator}' is not a valid QuantityComparator"))
    return issues


def _walk_references(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference" and isinstance(v, str):
                yield path + ".reference", v
            else:
                yield from _walk_references(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_references(v, f"{path}[{i}]")


def validate_bundle(bundle: dict[str, Any], patient_id: str | None = None) -> BundleValidationResult:
    issues: list[ValidationIssue] = []
    entries = bundle.get("entry") or []
    full_urls = [e.get("fullUrl") for e in entries]
    url_set = set(full_urls)
    counts = Counter(e.get("resource", {}).get("resourceType", "Unknown") for e in entries)

    # 1. Schema - each resource individually (precise locations) ...
    for i, entry in enumerate(entries):
        issues += _schema_issues(entry.get("resource", {}), f"Bundle.entry[{i}].resource")
        issues += _binding_issues(entry.get("resource", {}), f"Bundle.entry[{i}].resource")
    # ... and the Bundle envelope itself (type, fullUrl formats, etc.).
    envelope = {k: v for k, v in bundle.items() if k != "entry"}
    envelope["entry"] = [{"fullUrl": e.get("fullUrl")} for e in entries]
    issues += _schema_issues(envelope, "Bundle")
    issues += _binding_issues(envelope, "Bundle")

    # 2. Referential integrity
    if len(url_set) != len(full_urls):
        dupes = [u for u, n in Counter(full_urls).items() if n > 1]
        issues.append(ValidationIssue(severity="error", resource_type="Bundle", location="Bundle.entry.fullUrl",
                                      message=f"Duplicate fullUrl(s): {dupes}"))
    for i, entry in enumerate(entries):
        res = entry.get("resource", {})
        rtype, rid = res.get("resourceType", "Unknown"), res.get("id")
        if entry.get("fullUrl") != f"urn:uuid:{rid}":
            issues.append(ValidationIssue(severity="error", resource_type=rtype, resource_id=rid,
                                          location=f"Bundle.entry[{i}].fullUrl",
                                          message="fullUrl does not match resource id"))
        for loc, reference in _walk_references(res, f"Bundle.entry[{i}].resource"):
            if reference not in url_set:
                issues.append(ValidationIssue(severity="error", resource_type=rtype, resource_id=rid, location=loc,
                                              message=f"Unresolved reference '{reference}'"))

    # 3. Business rules
    patients = [e["resource"] for e in entries if e.get("resource", {}).get("resourceType") == "Patient"]
    if len(patients) != 1:
        issues.append(ValidationIssue(severity="error", resource_type="Bundle", location="Bundle.entry",
                                      message=f"Expected exactly one Patient, found {len(patients)}"))
    patient_url = f"urn:uuid:{patients[0]['id']}" if patients else None
    for p in patients:
        if not p.get("birthDate"):
            issues.append(ValidationIssue(severity="warning", resource_type="Patient", resource_id=p.get("id"),
                                          location="Patient.birthDate", message="Birth date missing"))
        if p.get("gender") == "unknown":
            issues.append(ValidationIssue(severity="warning", resource_type="Patient", resource_id=p.get("id"),
                                          location="Patient.gender", message="Gender unknown in all sources"))
        for tag in (p.get("meta") or {}).get("tag", []):
            issues.append(ValidationIssue(severity="warning", resource_type="Patient", resource_id=p.get("id"),
                                          location="Patient.meta.tag",
                                          message=f"Data-quality flag '{tag['code']}' requires HIM review"))

    for i, entry in enumerate(entries):
        res = entry.get("resource", {})
        rtype, rid = res.get("resourceType"), res.get("id")
        base = f"Bundle.entry[{i}].resource"
        if rtype in ("Encounter", "DocumentReference", "DiagnosticReport", "Observation"):
            if (res.get("subject") or {}).get("reference") != patient_url:
                issues.append(ValidationIssue(severity="error", resource_type=rtype, resource_id=rid,
                                              location=f"{base}.subject",
                                              message="subject must reference this Bundle's Patient"))
        if rtype in ("DiagnosticReport", "Observation") and not res.get("encounter"):
            issues.append(ValidationIssue(severity="error", resource_type=rtype, resource_id=rid,
                                          location=f"{base}.encounter", message="encounter reference missing"))
        if rtype == "DocumentReference" and not (res.get("context") or {}).get("encounter"):
            issues.append(ValidationIssue(severity="error", resource_type=rtype, resource_id=rid,
                                          location=f"{base}.context.encounter",
                                          message="context.encounter reference missing"))
        if rtype == "DocumentReference" and not res.get("date"):
            issues.append(ValidationIssue(severity="warning", resource_type=rtype, resource_id=rid,
                                          location=f"{base}.date", message="Document date missing"))
        if rtype == "DiagnosticReport" and not res.get("effectiveDateTime"):
            issues.append(ValidationIssue(severity="warning", resource_type=rtype, resource_id=rid,
                                          location=f"{base}.effective[x]", message="Report effective date missing"))
        if rtype == "Observation" and not any(c.get("system") == "http://loinc.org"
                                             for c in (res.get("code") or {}).get("coding", [])):
            issues.append(ValidationIssue(severity="warning", resource_type=rtype, resource_id=rid,
                                          location=f"{base}.code", message="Observation has no LOINC code"))

    pid = patient_id or (bundle.get("identifier") or {}).get("value", "unknown")
    return BundleValidationResult(
        patient_id=pid, bundle_id=bundle.get("id", ""), valid=not any(i.severity == "error" for i in issues),
        resource_counts=dict(counts), issues=issues,
    )


def build_report(results: list[BundleValidationResult]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fhir_version": FHIR_VERSION,
        "bundles": len(results),
        "valid_bundles": sum(r.valid for r in results),
        "total_errors": sum(len(r.errors) for r in results),
        "total_warnings": sum(len(r.warnings) for r in results),
        "resource_totals": dict(sum((Counter(r.resource_counts) for r in results), Counter())),
        "results": [r.model_dump() for r in results],
    }


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "validation_report.json"
    md_path = out_dir / "validation_report.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "# FHIR Bundle Validation Report",
        "",
        f"Generated: {report['generated_at']}  ",
        f"FHIR version: {report['fhir_version']}",
        "",
        f"**{report['valid_bundles']} / {report['bundles']} bundles valid** - "
        f"{report['total_errors']} error(s), {report['total_warnings']} warning(s)",
        "",
        "Resource totals: " + ", ".join(f"{k}: {v}" for k, v in sorted(report["resource_totals"].items())),
        "",
        "| Patient | Status | Resources | Errors | Warnings |",
        "|---|---|---|---|---|",
    ]
    for r in report["results"]:
        n_err = sum(i["severity"] == "error" for i in r["issues"])
        n_warn = sum(i["severity"] == "warning" for i in r["issues"])
        lines.append(f"| {r['patient_id']} | {'PASS' if r['valid'] else 'FAIL'} | "
                     f"{sum(r['resource_counts'].values())} | {n_err} | {n_warn} |")
    for r in report["results"]:
        if not r["issues"]:
            continue
        lines += ["", f"## {r['patient_id']}", ""]
        for i in r["issues"]:
            lines.append(f"- **{i['severity'].upper()}** `{i['location']}` ({i['resource_type']}): {i['message']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
