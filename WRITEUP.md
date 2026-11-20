# EHR Media Intelligence: Write-up

Abel Atkelet · github.com/abelxo19/ehr-media-intelligence

## Tradeoffs

- **Rules over ML for cleaning.** Every change is explainable and logged with its source file and line. → `test_every_audit_entry_is_attached_to_a_source`
- **Flag, don't merge.** A second MRN with the same name and DOB becomes an alternate identifier for review. → `test_conflicting_mrn_is_linked_as_alternate_and_flagged`
- **R4B models.** `fhir.resources` 8 ships only R4B and STU3; my five resource types are the same in R4. It skips value-set checks (it accepted `gender: "M"`), so I added them. → `test_validator_reports_schema_errors_with_location`
- **Hybrid search.** Pure MiniLM ranked a sodium panel first for "critical potassium"; a 20% keyword term fixes it. → `test_keyword_boost_fixes_critical_potassium_ranking`
- **Offline fallback.** The same schema when Claude is unavailable. Reviewing it fixed its mammogram finding (now the BI-RADS 4C sentence). → `test_mammogram_finding_uses_the_significant_sentence`

## With more time

- BM25 + embedding fusion and a clinical embedding model
- A terminology service and US Core validation with the HL7 validator
- OCR for PDFs and images; extract `Condition` and `MedicationStatement`
- A clinician-labelled evaluation set for summaries
- Auth, access audit, encryption at rest, a BAA-covered LLM

## FHIR and clinical concepts I researched

- Bundle types and `urn:uuid` references
- `DocumentReference` vs `DiagnosticReport` + `Observation`
- `instant` vs `dateTime`; `Encounter.class` in R4
- v2-0074 categories, ObservationInterpretation, LOINC, UCUM
- BI-RADS and Lung-RADS, critical lab values, NSTEMI, CKD staging

## Validating AI summary quality

- **Constrained output.** A JSON schema, and every finding cites a record id. → `app/summarization/prompt.py`
- **Grounding check.** Numbers, key words and record ids must exist in the source; one corrective retry. → `check_grounding` in `summarizer.py`
- **Adversarial tests.** An invented diagnosis, a made-up K 7.9, a fake record id and a 250-word summary: all caught. → `tests/test_summarizer.py`
- **Real output reviewed.** Claude Sonnet 5.5 summarized all 12 patients: no invented values, 81–178 words, every citation valid. → `docs/summary_review.md`
- **Review drove fixes.** Run 1 needed 11 retries: mostly my checker rejecting correct paraphrases, plus leaked markdown and lab values relabelled as diagnoses. A tighter prompt and checker brought it to 2. → `test_faithful_paraphrase_is_not_flagged`
- **Still open.** Mild over-reading ("on both dates") and symptoms listed as diagnoses: the checker verifies facts, not judgement. → `docs/summary_review.md`
