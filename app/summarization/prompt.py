"""Prompt + rendering of a FHIR Bundle into compact, citation-friendly text for the LLM."""

from __future__ import annotations

import base64
from datetime import date
from typing import Any

PROMPT_VERSION = "2026-11-v5"
MAX_SUMMARY_WORDS = 200
PATIENT_ID = "PATIENT"  # citation id for patient-level data-quality flags

SYSTEM_PROMPT = f"""\
You are a clinical documentation assistant that writes chart summaries for clinicians \
who must review a patient's media records (labs, imaging, pathology, notes) quickly.

You will receive one patient's records extracted from an HL7 FHIR R4 Bundle. Each record \
starts with an id in square brackets, e.g. [LAB-ACC-5001].

Write a structured summary with these fields:
- chief_concern: the main reason the patient is currently receiving care, one sentence.
- key_diagnoses: up to 6 diagnoses that are explicitly documented in the records, most \
important first. Short noun phrases (e.g. "Type 2 diabetes mellitus"). Conditions only: \
no risk factors, family history, procedures or individual lab values.
- recent_media: up to 4 of the most recent labs / imaging / pathology / cardiology reports, \
newest first, each with its date, a short type label, a one-line key finding and the record id.
- flagged_anomalies: up to 5 findings that need attention (critical or abnormal labs, \
suspicious imaging, unresolved follow-ups, identifier/data-quality flags), each with the \
record id it came from.
- narrative: 2-3 sentences tying the picture together for a busy clinician.

Rules - these matter more than style:
1. Use ONLY facts stated in the records. Never add diagnoses, medications, values or dates \
that are not written there. If something is uncertain in the source ("suspected", \
"concerning for"), keep that uncertainty.
2. Quote numeric values exactly as written, with units.
3. Every recent_media and flagged_anomalies item must cite the id of the record it came from; \
for patient-level data-quality flags cite {PATIENT_ID}.
4. The whole summary (all fields together) must be under {MAX_SUMMARY_WORDS} words. Be terse; \
clinicians prefer fragments over full sentences.
5. Do not give treatment recommendations of your own; only report plans documented in the records.
6. Use the records' own clinical terms. Do not relabel a lab value as a new diagnosis \
(write "potassium 6.3 mmol/L (critical high)", not "hyperkalemia", unless a note says so), \
and do not call a finding persistent or worsening unless two dated results show it.
7. Plain text only: no markdown, asterisks or bracketed flags in any field.
"""


def _decode(attachment: dict[str, Any]) -> str:
    data = attachment.get("data")
    if not data:
        return ""
    return base64.b64decode(data).decode("utf-8", errors="replace")


def _age(birth_date: str | None, today: date) -> str:
    if not birth_date:
        return "age unknown"
    b = date.fromisoformat(birth_date)
    years = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    return f"{years}-year-old"


def render_bundle(bundle: dict[str, Any], *, today: date | None = None) -> tuple[str, list[str]]:
    """Return (prompt text, list of record ids) for a patient Bundle.

    Observations are folded into their DiagnosticReport so the model sees each lab panel
    as one record with its values and abnormal flags.
    """
    today = today or date.today()
    by_url = {e["fullUrl"]: e["resource"] for e in bundle.get("entry", [])}
    patient = next(r for r in by_url.values() if r["resourceType"] == "Patient")
    name = (patient.get("name") or [{}])[0].get("text", "Unknown")
    lines = [
        f"PATIENT: {name}, {_age(patient.get('birthDate'), today)} {patient.get('gender', 'unknown')}, "
        f"MRN {patient['identifier'][0]['value']}",
    ]
    flags = [t["code"] for t in (patient.get("meta") or {}).get("tag", [])]
    if flags:
        lines.append(f"[{PATIENT_ID}] DATA-QUALITY FLAGS: {', '.join(flags)} (alternate MRNs: "
                     f"{', '.join(i['value'] for i in patient['identifier'][1:]) or 'none'})")
    lines.append(f"TODAY: {today.isoformat()}")
    lines.append("")

    items: list[tuple[str, str, str]] = []  # (date, record_id, text)
    for res in by_url.values():
        rtype = res["resourceType"]
        if rtype == "DocumentReference":
            rid = res["masterIdentifier"]["value"]
            when = (res.get("date") or "")[:10]
            body = _decode(res["content"][0]["attachment"])
            label = res["type"].get("text") or res["type"]["coding"][0]["display"]
            author = (res.get("author") or [{}])[0].get("display", "")
            items.append((when, rid, f"[{rid}] {when or 'undated'} | NOTE: {label} | {author}\n{body}"))
        elif rtype == "DiagnosticReport":
            rid = res["identifier"][0]["value"]
            when = (res.get("effectiveDateTime") or "")[:10]
            category = res["category"][0]["coding"][0]["display"]
            label = res["code"].get("text", "")
            if res.get("result"):
                values = []
                for r in res["result"]:
                    obs = by_url.get(r["reference"], {})
                    values.append(_format_observation(obs))
                body = "\n".join(values)
            else:
                body = _decode(res["presentedForm"][0])
            items.append((when, rid, f"[{rid}] {when or 'undated'} | {category.upper()}: {label}\n{body}"))
    items.sort(key=lambda x: x[0], reverse=True)
    lines += [text + "\n" for _, _, text in items]
    ids = [rid for _, rid, _ in items] + ([PATIENT_ID] if flags else [])
    return "\n".join(lines).strip(), ids


def _format_observation(obs: dict[str, Any]) -> str:
    name = obs.get("code", {}).get("text", "?")
    if "valueQuantity" in obs:
        q = obs["valueQuantity"]
        value = f"{q.get('comparator', '')}{q['value']:g} {q.get('unit', '')}".strip()
    else:
        value = obs.get("valueString", "")
    ref = f" (ref {obs['referenceRange'][0]['text']})" if obs.get("referenceRange") else ""
    interp = ""
    if obs.get("interpretation"):
        code = obs["interpretation"][0]["coding"][0]
        if code["code"] != "N":
            interp = f", {code['display'].lower()}"
    return f"- {name}: {value}{ref}{interp}"
