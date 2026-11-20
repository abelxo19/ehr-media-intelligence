# EHR Media Intelligence: Write-up

Abel Atkelet · github.com/abelxo19/ehr-media-intelligence

## Tradeoffs

- **Rules over ML for cleaning.** Every change is explainable and logged with its source file and line. → `test_every_audit_entry_is_attached_to_a_source`
- **Link and flag.** Records under a second MRN with the same name and DOB join that patient; the MRN is kept as an alternate and flagged for HIM review. → `test_conflicting_mrn_is_linked_as_alternate_and_flagged`
- **R4B models.** `fhir.resources` 8 defaults to R5 and has no R4 package, so I used its R4B models; my five resource types are the same in R4. It skips value-set checks (it accepted `gender: "M"`), so I added them. → `test_validator_reports_schema_errors_with_location`
- **Hybrid search.** Pure MiniLM ranked a sodium panel first for "critical potassium"; a 20% keyword term fixes it. → `test_keyword_boost_fixes_critical_potassium_ranking`
- **Offline fallback.** Same schema when Claude is down; review fixed its mammogram finding (now BI-RADS 4C). → `test_mammogram_finding_uses_the_significant_sentence`

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
- **Grounding check.** Every field, narrative included: whole numbers, key words and record ids must exist in the source. One retry, then a strict 200-word cap. → `test_chief_concern_and_narrative_are_checked`
- **Adversarial tests.** Invented diagnosis, made-up K 7.9, fake record id, 250-word summary: all caught. → `tests/test_summarizer.py`
- **Real output reviewed.** Sonnet 5.5 summarized all 12 patients: every number in the source, 84–168 words, valid citations. → `docs/summary_review.md`
- **Review drove fixes.** Five runs cut retries from 11 to 1; reading every summary caught what checks miss, like a discharge date given as the admission. → `test_render_includes_encounter_period`
- **Still open.** Small inferences ("above goal"), symptoms listed as diagnoses: values are checked, not their use. → `docs/summary_review.md`
