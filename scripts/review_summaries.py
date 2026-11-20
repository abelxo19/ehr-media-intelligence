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
    "pt-0012345": ("fixed", "Run 4 wrote \"admitted 2026-02-14\", which is the discharge-summary date. The prompt now "
                            "shows each record's encounter span, and run 5 says \"Admitted 2026-02-10 to 2026-02-14\" "
                            "(`test_render_includes_encounter_period`). Lab values and plans exact."),
    "pt-0023456": ("minor", "Facts correct (troponin 88 to 240, LAD 90%, LVEF 45%). \"LDL still above goal\" is an "
                            "inference: the note gives LDL 112 but no goal."),
    "pt-0034567": ("minor", "Correct; the earlier \"on both dates\" over-reading is gone. \"Reactive airway disease\" "
                            "(a CXR impression) is listed as a diagnosis."),
    "pt-0045678": ("minor", "Admission 2025-12-01 to 2025-12-06, LVEF 25% then 30% correct; duplicate MRN cited as "
                            "PATIENT. \"Pulmonary edema\" (a CXR finding) is listed as a diagnosis."),
    "pt-0056789": ("ok", "Correct; normal MRI not flagged as an anomaly."),
    "pt-0067890": ("ok", "Chief concern is the PET-avid lung malignancy (the offline summary led with COPD); "
                         "nodule growth 9 to 13 mm, SUV values and 30 pack-year history exact."),
    "pt-0078901": ("ok", "Correct; WBC 16.4 and neutrophils 86% flagged."),
    "pt-0089012": ("ok", "Uses the source's terms (\"potassium 6.3 mmol/L (critical high)\"); notes the missing ED "
                         "repeat potassium."),
    "pt-0099012": ("ok", "Receptor status, grade, Ki-67, BI-RADS 4C and node findings exact."),
    "pt-0101123": ("minor", "Endarterectomy and NIHSS 8 to 3 captured. \"Expressive aphasia\" (a symptom) is listed "
                            "as a diagnosis."),
    "pt-0112234": ("ok", "Doses and DEXA values exact; notes the TSH recheck is missing."),
    "pt-0123345": ("minor", "Correct, but the last narrative sentence (\"...listed with reference ranges\") says "
                            "nothing. The checker flags it, so confidence is medium."),
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
        "### How the prompt and checker got here",
        "",
        "| Run | Prompt | Retries | What I found | Change |",
        "|---|---|---|---|---|",
        "| 1 | v3 | 11 / 12 | No invented facts. Five correct paraphrases flagged (\"elevated creatinine 1.9\"). "
        "`** HIGH **` markers leaked into output; lab values relabelled as diagnoses (\"hyperkalemia\"). | Paraphrase "
        "words ignored by the checker (numbers still strict); plain-text lab flags; prompt rules 6-7. |",
        "| 2 | v4 | 2 / 12 | One real model error: cited `DATA-QUALITY FLAGS` as a record id (the retry fixed it). "
        "| Patient-level flags get the citable id `PATIENT`; abbreviation map. |",
        "| 3 | v5 | 2 / 12 | Both retries were absence statements (\"; no newer values\"). | Absence clauses are not "
        "word-checked (numbers still are). |",
        "| 4 | v6 | 2 / 12 | An external review showed `chief_concern` and `narrative` were never checked, `6.3` matched "
        "inside `16.3`, and a long chief concern could exceed 200 words. Once fixed, the checker caught two real "
        "narrative problems (\"on both dates\", advice to \"need verification\"). My read-through then found "
        "\"admitted 2026-02-14\" for a 2026-02-10 admission. | Every field checked; whole-number matching; strict "
        "length cap; prompt rule 8 (no inferences or advice in the narrative). |",
        "| 5 | v7 | 1 / 12 | Shipped. Admission dates now correct. | Each record header shows its encounter span. |",
        "",
        "Still not caught automatically: a real date attached to the wrong event (Maria, run 4), small",
        "inferences (\"above goal\"), and findings or symptoms listed as diagnoses. The checker verifies that",
        "values exist in the source, not that they are used correctly, so clinician review stays in the loop.",
        "",
        f"## Offline fallback: `{RuleBasedSummarizer.name}`",
        "",
        f"Used when Claude is unavailable. Words {min(r_words)}-{max(r_words)}.",
        "",
        *_table(r_rows),
        "",
        "## Earlier failed run",
        "",
        "My first Claude run (2026-11-20, before my account had credit) returned HTTP 400 *\"Your credit balance",
        "is too low\"* for every request; all 12 patients fell back to the offline summarizer as designed.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
