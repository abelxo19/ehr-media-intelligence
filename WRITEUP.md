# EHR Media Intelligence Platform: Write-up

## Tradeoffs I made and why

- **Auditable rules over "smart" cleaning.** Every normalization is a small pure function that returns the value plus an audit entry with the source file and line. It is less flexible than ML entity resolution, but each change can be explained to a clinician or an HIM analyst.
- **Link and flag, never silently merge.** When two MRNs share a name and DOB, I keep the second as an alternate identifier and flag `possible_duplicate_mrn` instead of merging. A false merge mixes two people's charts, which is worse than a duplicate.
- **R4B models from `fhir.resources`.** v8 has no pure R4 package. The six resources I emit are structurally identical in R4 and R4B. The library also does not check terminology bindings (it accepted `gender: "M"`), so I added explicit required-binding checks.
- **Exact FAISS search with hybrid scoring.** A flat index is exact and fast at this scale, and it lets filters run before the top-5 cut. I added a 20% keyword-overlap term after pure MiniLM ranked a sodium panel above the critical potassium result for "critical potassium".
- **Claude plus a deterministic fallback.** The pipeline, tests and demo never depend on the network. Fallback summaries keep the same schema, are labelled `rule-based`, and are not cached under the Claude key, so the next run retries the LLM.

## What I would improve with more time

- **Search:** real hybrid retrieval (BM25 + embeddings, reciprocal rank fusion), a clinical embedding model, and an approximate (HNSW) index with metadata pre-filtering at scale.
- **Terminology:** a terminology service (LOINC, SNOMED CT, RxNorm, UCUM) instead of local tables, and US Core profile validation with the HL7 Java validator.
- **Inputs:** real OCR on PDFs and images, and NLP extraction of problems and medications into `Condition`/`MedicationStatement`.
- **Summary evaluation:** a clinician-labelled eval set with an LLM-as-judge for faithfulness and completeness, run on every prompt change.
- **Production readiness:** auth with role-based access, access audit trail, encryption at rest, a BAA-covered LLM deployment, and an incremental pipeline instead of full rebuilds.

## FHIR and clinical concepts I had to research

- **FHIR structure:** Bundle `collection` vs `transaction` vs `document`; `urn:uuid` fullUrls and reference resolution.
- **Resource choice:** `DocumentReference` for notes vs `DiagnosticReport` + `Observation` for results.
- **Datatypes:** `instant` vs `dateTime` (timezone rules); R4 `Encounter.class` as a single Coding.
- **Terminology:** v2-0074 report categories, v3 ObservationInterpretation (`H/L/HH/LL`), LOINC note and lab codes, UCUM units, v2-0203 `MR` identifiers.
- **Clinical concepts:** MRN formats and HIM duplicate-record review; BI-RADS and Lung-RADS categories; what makes a lab "critical"; NSTEMI and troponin; CKD staging and eGFR.

## How I validated AI summary quality

1. **Constrained output.** A JSON-schema structured output guarantees all five sections exist. The prompt requires a cited record id for every media item and anomaly, exact numbers, and no recommendations of the model's own.
2. **Automatic grounding check.** Every diagnosis, finding and anomaly is checked against the rendered source:
   - every number must appear in the source;
   - at least 60% of the content words (with abbreviation expansion) must appear;
   - every cited record id must exist.

   On failure, or if the summary is over 200 words, I re-prompt once with the exact problem. Confidence is computed from these checks, not self-reported.
3. **Adversarial tests.** Fake LLM responses with an invented diagnosis ("pulmonary embolism"), an invented value (K 7.9), a non-existent record id, and a 250-word narrative. All are caught, retried or trimmed, and down-graded.
4. **Manual review.** I read all 12 patient summaries against their source notes. All scored 100% grounding, with 43–149 words and at most 5 diagnoses. The review caught three issues:
   - a mammogram summary used the uninformative first sentence, so the summarizer now prefers the BI-RADS 4C sentence;
   - a synthetic note gave an age that didn't match the DOB, which I fixed in the data;
   - for one patient the extractive fallback picked COPD as chief concern rather than the suspected lung cancer. Grounding checks catch invented facts, not wrong priorities, which is why the LLM is the primary summarizer and clinician review stays in the loop.
5. **Live Claude run.** The integration authenticated, but the trial account had no API credit, so every call fell back as designed. The tests cover the Claude request shape and error handling with a mocked client. Re-running `python -m app.pipeline --summarizer claude` after adding credit regenerates the summaries; they pass through the same checks.
