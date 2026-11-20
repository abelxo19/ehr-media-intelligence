"""Write docs/summary_review.md: the per-patient record of my manual summary review.

    python scripts/review_summaries.py            # rebuild the review from committed files
    python scripts/review_summaries.py --export   # first copy the latest Claude summaries from var/ehr.db

The Claude summaries reviewed here are committed verbatim in docs/claude_summaries.json
(synthetic data only). Word counts, diagnosis counts and grounding are recomputed from that
file and the current code, so the table cannot drift from the repository. The verdicts and
notes are mine, written after reading each summary against its source records.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.fhir.mapper import build_all_bundles  # noqa: E402
from app.ingestion.pipeline import run_ingestion  # noqa: E402
from app.summarization.prompt import MAX_SUMMARY_WORDS, render_bundle  # noqa: E402
from app.summarization.summarizer import (  # noqa: E402
    ClinicalSummary,
    RuleBasedSummarizer,
    SummaryContent,
    check_grounding,
    count_words,
)

TODAY = date(2026, 11, 20)
OUT = ROOT / "docs" / "summary_review.md"
CLAUDE_FILE = ROOT / "docs" / "claude_summaries.json"

# Verdicts: ok | minor (small wording/classification issue, no wrong facts) | gap | limitation | fixed
CLAUDE_NOTES: dict[str, tuple[str, str]] = {
    "pt-0012345": ("ok", "All values match (glucose 412, creatinine 2.4 to 1.7, eGFR 27, A1c 10.8, UACR 480); "
                         "6 diagnoses, 5 anomalies, both at the limit."),
    "pt-0023456": ("minor", "Facts correct (troponin 88 to 240, LAD 90%, LVEF 45%). \"Reduced LVEF 45%\" is a "
                            "finding listed as a diagnosis; \"DAPT for 12 months\" is a plan listed as an anomaly."),
    "pt-0034567": ("minor", "Says eosinophils 8.5% \"on both dates\"; the pulmonology note most likely restates the "
                            "same CBC value, so this over-reads the record. Everything else correct."),
    "pt-0045678": ("minor", "Correct, including LVEF 25% then 30% and NYHA II; duplicate MRN cited as PATIENT. "
                            "\"Pulmonary edema\" (a CXR finding) listed as a diagnosis."),
    "pt-0056789": ("ok", "Correct; normal MRI not flagged as an anomaly."),
    "pt-0067890": ("ok", "Chief concern is the PET-avid lung malignancy (the offline summary wrongly led with COPD); "
                         "nodule growth 9 to 13 mm and SUV values exact."),
    "pt-0078901": ("ok", "Correct; age 24 matches DOB."),
    "pt-0089012": ("ok", "Uses the source's terms (\"potassium 6.3 mmol/L (critical high)\"), no relabelling; "
                         "notes the ED repeat potassium is missing."),
    "pt-0099012": ("ok", "Receptor status, grade, Ki-67, BI-RADS 4C and node findings exact."),
    "pt-0101123": ("minor", "Captures the carotid endarterectomy the offline summary missed; NIHSS 8 to 3 correct. "
                            "\"Right arm weakness\" (a symptom) listed as a diagnosis."),
    "pt-0112234": ("ok", "Doses and DEXA values exact; notes the TSH recheck is missing."),
    "pt-0123345": ("ok", "Correct; flags that the planned 2-week X-ray never appears in the records."),
}

RULE_NOTES: dict[str, tuple[str, str]] = {
    "pt-0012345": ("ok", "DM2 + CKD + AKI + retinopathy all present; eGFR 27 and HbA1c 10.8 flagged."),
    "pt-0023456": ("fixed", "Note said \"52-year-old\" but DOB gives 53 on 2026-05-18. Data corrected; "
                            "guarded by `test_ages_stated_in_notes_match_date_of_birth`."),
    "pt-0034567": ("ok", "Asthma exacerbation; eosinophils 8.5% flagged."),
    "pt-0045678": ("ok", "HFrEF; duplicate-MRN identity flag listed first among anomalies."),
    "pt-0056789": ("ok", "Short (single problem); normal MRI correctly not flagged."),
    "pt-0067890": ("limitation", "Chief concern reads \"COPD GOLD 2\" although the dominant issue is the suspected "
                                 "lung malignancy (listed under media and anomalies). Rules can't rank priority."),
    "pt-0078901": ("ok", "Appendicitis; CT finding and WBC 16.4 / neutrophils 86% flagged."),
    "pt-0089012": ("ok", "Critical K 6.3 listed first; renal artery stenosis captured."),
    "pt-0099012": ("fixed", "Mammogram finding was its first sentence, \"Diagnostic mammogram, left breast.\" "
                            "Now selects the BI-RADS 4C sentence; guarded by "
                            "`test_mammogram_finding_uses_the_significant_sentence`."),
    "pt-0101123": ("gap", "Misses the carotid endarterectomy (operative note) and repeats \"MRI Brain: MRI brain\" "
                          "in one anomaly."),
    "pt-0112234": ("ok", "Hypothyroidism, osteoporosis, vitamin D deficiency captured."),
    "pt-0123345": ("ok", "Scaphoid fracture; normal creatinine correctly not flagged."),
}


def export_from_store() -> None:
    from app.config import get_settings
    from app.store import Store

    store = Store(get_settings().db_path)
    out = {}
    for p in store.list_patients():
        bundle = store.get_bundle(p["patient_id"])
        summary = store.latest_summary(p["patient_id"], bundle["content_hash"])
        if not summary or not summary["generator"].startswith("claude:"):
            sys.exit(f"{p['patient_id']} has no Claude summary in the store; run the pipeline with Claude first")
        out[p["patient_id"]] = summary
    CLAUDE_FILE.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {CLAUDE_FILE}")


def _table(rows: list[str]) -> list[str]:
    return ["| Patient | Words | Dx | Grounding | Invalid ids | Retried | Verdict | Reviewer note |",
            "|---|---|---|---|---|---|---|---|", *rows]


def main() -> None:
    if "--export" in sys.argv:
        export_from_store()
    dataset = run_ingestion(ROOT / "data" / "raw", today=TODAY)
    bundles = build_all_bundles(dataset)
    names = {p.patient_id: p.display_name for p in dataset.patients}
    rendered = {pid: render_bundle(b, today=TODAY) for pid, b in bundles.items()}

    claude = {pid: ClinicalSummary.model_validate(s)
              for pid, s in json.loads(CLAUDE_FILE.read_text(encoding="utf-8")).items()}
    c_rows, c_words, c_retries = [], [], []
    for pid in sorted(claude):
        s = claude[pid]
        text, ids = rendered[pid]
        report = check_grounding(SummaryContent.model_validate(s.model_dump()), text, ids)
        verdict, note = CLAUDE_NOTES.get(pid, ("-", "not reviewed"))
        c_words.append(s.word_count)
        if s.retry_reason:
            c_retries.append(f"- {names[pid]}: {s.retry_reason}")
        c_rows.append(f"| {names[pid]} (`{pid}`) | {s.word_count} | {len(s.key_diagnoses)} | {report.score:.0%} | "
                      f"{len(report.invalid_citations)} | {'yes' if s.retry_reason else 'no'} | {verdict} | {note} |")
    model = next(iter(claude.values())).generator

    r_rows, r_words = [], []
    for pid in sorted(bundles):
        text, ids = rendered[pid]
        content = RuleBasedSummarizer().generate(text, bundles[pid])
        report = check_grounding(content, text, ids)
        verdict, note = RULE_NOTES.get(pid, ("-", "not reviewed"))
        r_words.append(count_words(content))
        r_rows.append(f"| {names[pid]} (`{pid}`) | {count_words(content)} | {len(content.key_diagnoses)} | "
                      f"{report.score:.0%} | {len(report.invalid_citations)} | n/a | {verdict} | {note} |")

    lines = [
        "# Summary review record",
        "",
        "Generated by `scripts/review_summaries.py`. Grounding = share of diagnoses / findings / anomalies whose",
        "numbers and key words appear in the source records; invalid = cited record ids that don't exist.",
        f"Word limit: {MAX_SUMMARY_WORDS}.",
        "",
        f"## Claude summaries (shipped): `{model}`",
        "",
        f"Stored verbatim in `docs/claude_summaries.json` (prompt `{next(iter(claude.values())).prompt_version}`). "
        f"Words {min(c_words)}-{max(c_words)}. No invented values, dates or plans found; "
        "issues are listed as *minor*.",
        "",
        *_table(c_rows),
        "",
        "Retries in the shipped run (the checker rejected the first draft and the model rewrote it):",
        "",
        *(c_retries or ["- none"]),
        "",
        "Both were checker false positives on absence statements (\"; no newer values\"). The checker now",
        "word-checks only the part before \"; no ...\" while still checking every number",
        "(`test_absence_clause_still_checks_numbers`).",
        "",
        "### How the prompt and checker got here",
        "",
        "| Run | Prompt | Retries | What I found | Change |",
        "|---|---|---|---|---|",
        "| 1 | v3 | 11 / 12 | No invented facts. Five correct paraphrases flagged (\"elevated creatinine 1.9\", "
        "\"MRI planned; no result\"). `** HIGH **` markers leaked into output. Lab values relabelled as "
        "diagnoses (\"hyperkalemia\"); smoking and family history listed as diagnoses. | Paraphrase words ignored "
        "by the checker (numbers still strict); plain-text lab flags; prompt rules 6-7 (source terms only, no "
        "markdown); diagnoses = conditions only. Tests: `test_faithful_paraphrase_is_not_flagged`. |",
        "| 2 | v4 | 2 / 12 | One more paraphrase (\"HbA1c\" vs \"Hemoglobin A1c\"). One real model error: cited "
        "`DATA-QUALITY FLAGS` as a record id; the retry fixed it. | Abbreviation map; patient-level flags get the "
        "citable id `PATIENT`. Test: `test_patient_level_flag_can_be_cited`. |",
        "| 3 | v5 | 2 / 12 | Shipped. Both retries were absence-statement false positives (above). | Absence-clause "
        "handling in the checker. |",
        "",
        "Not caught automatically: mild over-reading (\"on both dates\") and findings or symptoms listed as",
        "diagnoses. The checker verifies facts against the source, not clinical judgement, so clinician review",
        "stays in the loop.",
        "",
        f"## Offline fallback: `{RuleBasedSummarizer.name}`",
        "",
        f"Used when Claude is unavailable. Words {min(r_words)}-{max(r_words)}.",
        "",
        *_table(r_rows),
        "",
        "## Earlier failed run",
        "",
        "My first Claude run (2026-11-20, before the account had credit) returned HTTP 400 *\"Your credit balance",
        "is too low\"* for every request; all 12 patients fell back to the offline summarizer as designed.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
