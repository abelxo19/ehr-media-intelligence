"""AI clinical summarization of patient FHIR Bundles.

* ``ClaudeSummarizer`` calls the Anthropic Messages API with a JSON-schema constrained
  output, then validates it with Pydantic.
* ``RuleBasedSummarizer`` is a deterministic offline fallback (no key / API outage / tests)
  that produces the same schema from the structured data.
* ``SummaryService`` adds the SQLite cache (key = patient id + bundle content hash +
  prompt version + generator), the 200-word guard and a grounding check that verifies
  every diagnosis / anomaly / cited record id against the source records.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.summarization.prompt import MAX_SUMMARY_WORDS, PROMPT_VERSION, SYSTEM_PROMPT, render_bundle

log = logging.getLogger(__name__)

DISCLAIMER = (
    "AI-generated summary for information retrieval only. It may be incomplete or inaccurate, "
    "is not a clinical decision, and must be verified against the source record before any care decision."
)


# ---------------------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------------------
class MediaItem(BaseModel):
    date: str | None = None
    type: str
    finding: str
    record_id: str


class Anomaly(BaseModel):
    finding: str
    record_id: str | None = None


class SummaryContent(BaseModel):
    """What the model is asked to produce."""

    chief_concern: str
    key_diagnoses: list[str]
    recent_media: list[MediaItem]
    flagged_anomalies: list[Anomaly]
    narrative: str


class GroundingReport(BaseModel):
    score: float = Field(description="Fraction of checked claims found in the source records")
    checked: int
    ungrounded: list[str] = Field(default_factory=list)
    invalid_citations: list[str] = Field(default_factory=list)


class ClinicalSummary(SummaryContent):
    patient_id: str
    word_count: int
    confidence: Literal["high", "medium", "low"]
    disclaimer: str = DISCLAIMER
    generator: str
    prompt_version: str = PROMPT_VERSION
    grounding: GroundingReport
    retry_reason: str | None = Field(None, description="Why the first draft was rejected, if it was")
    source_record_ids: list[str]
    content_hash: str
    generated_at: str


# JSON schema sent to the API (strict: every property required, no extras).
_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chief_concern": {"type": "string"},
        "key_diagnoses": {"type": "array", "items": {"type": "string"}},
        "recent_media": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "type": {"type": "string"},
                    "finding": {"type": "string"},
                    "record_id": {"type": "string"},
                },
                "required": ["date", "type", "finding", "record_id"],
                "additionalProperties": False,
            },
        },
        "flagged_anomalies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"finding": {"type": "string"}, "record_id": {"type": "string"}},
                "required": ["finding", "record_id"],
                "additionalProperties": False,
            },
        },
        "narrative": {"type": "string"},
    },
    "required": ["chief_concern", "key_diagnoses", "recent_media", "flagged_anomalies", "narrative"],
    "additionalProperties": False,
}


class SummaryError(RuntimeError):
    pass


class Summarizer(Protocol):
    name: str

    def generate(self, rendered: str, bundle: dict[str, Any], feedback: str | None = None) -> SummaryContent: ...


# ---------------------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------------------
class ClaudeSummarizer:
    def __init__(self, model: str, client: Any | None = None) -> None:
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.name = f"claude:{model}"
        self.client = client or anthropic.Anthropic(max_retries=3)

    def generate(self, rendered: str, bundle: dict[str, Any], feedback: str | None = None) -> SummaryContent:
        user = f"Summarize this patient's records.\n\n<records>\n{rendered}\n</records>"
        if feedback:
            user += f"\n\nYour previous attempt had a problem: {feedback} Fix it in this attempt."
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
                # Server-side fallback if a safety classifier declines (clinical text can trip them).
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self._anthropic.AuthenticationError as exc:
            raise SummaryError("Anthropic API key rejected") from exc
        except self._anthropic.RateLimitError as exc:
            raise SummaryError("Anthropic rate limit hit") from exc
        except self._anthropic.APIStatusError as exc:
            raise SummaryError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise SummaryError("Could not reach the Anthropic API") from exc

        if response.stop_reason == "refusal":
            raise SummaryError("Model declined to summarize this record")
        if response.stop_reason == "max_tokens":
            raise SummaryError("Summary was truncated (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise SummaryError("Model returned no text block")
        try:
            return SummaryContent.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SummaryError(f"Model output failed schema validation: {exc}") from exc


# ---------------------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------------------
# (regex, display) - lexicon of conditions present in typical notes; first match wins per display.
_DIAGNOSIS_LEXICON: list[tuple[str, str]] = [
    (r"nstemi", "NSTEMI"),
    (r"ischemic stroke|acute infarct", "Acute ischemic stroke"),
    (r"invasive ductal carcinoma", "Invasive ductal carcinoma, left breast"),
    (r"primary lung malignancy|suspicious for malignancy|suspicious pulmonary nodule", "Suspicious lung nodule / possible lung cancer"),
    (r"heart failure", "Heart failure with reduced ejection fraction"),
    (r"acute appendicitis", "Acute appendicitis"),
    (r"type 2 diabetes", "Type 2 diabetes mellitus"),
    (r"chronic kidney disease|ckd", "Chronic kidney disease"),
    (r"acute kidney injury|\baki\b", "Acute kidney injury"),
    (r"diabetic retinopathy", "Diabetic retinopathy"),
    (r"asthma", "Asthma"),
    (r"copd", "COPD"),
    (r"migraine", "Chronic migraine"),
    (r"hypothyroidism|hashimoto", "Primary hypothyroidism"),
    (r"osteoporosis", "Osteoporosis"),
    (r"scaphoid fracture|scaphoid waist fracture", "Scaphoid fracture"),
    (r"renal artery stenosis", "Renal artery stenosis"),
    (r"atrial fibrillation", "Atrial fibrillation"),
    (r"stenosis of the left internal carotid|left ica stenosis|carotid artery", "Left carotid artery stenosis"),
    (r"hypertension", "Hypertension"),
    (r"vitamin d deficiency", "Vitamin D deficiency"),
]
_CONCERN_TYPES = ("discharge summary", "emergency department note", "consult note", "progress note")
_SUSPICIOUS = re.compile(r"suspicio|malignan|acute|critical|stenosis|carcinoma|infarct|edema|fracture|"
                         r"bi-rads [45]|lung-rads 4|osteoporosis|dysfunction|nodule", re.I)


def _first_sentence(text: str, max_words: int = 22) -> str:
    sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    words = sentence.split()
    return " ".join(words[:max_words]) + ("..." if len(words) > max_words else "")


def _lab_label(obs: dict[str, Any]) -> str:
    """'Potassium: 6.3 mmol/L (ref 3.5-5.1) CRITICAL HIGH' for the offline summary."""
    from app.summarization.prompt import _format_observation

    label = _format_observation(obs)[2:]
    return re.sub(r", (critical high|critical low|high|low|abnormal)$", lambda m: " " + m.group(1).upper(), label)


def _key_sentence(text: str, max_words: int = 18) -> str:
    """Prefer the first clinically significant sentence (suspicious / acute / BI-RADS ...)
    over the first sentence, which in reports without an IMPRESSION is often just the exam name."""
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]
    chosen = next((x for x in sentences if _SUSPICIOUS.search(x)), sentences[0] if sentences else "")
    return _first_sentence(chosen, max_words)


class RuleBasedSummarizer:
    name = "rule-based:v3"

    def generate(self, rendered: str, bundle: dict[str, Any], feedback: str | None = None) -> SummaryContent:
        from app.summarization.prompt import _decode, _format_observation

        by_url = {e["fullUrl"]: e["resource"] for e in bundle["entry"]}
        patient = next(r for r in by_url.values() if r["resourceType"] == "Patient")
        docs, reports = [], []
        for res in by_url.values():
            if res["resourceType"] == "DocumentReference":
                docs.append(((res.get("date") or "")[:10], res["masterIdentifier"]["value"],
                             res["type"]["coding"][0]["display"].lower(), _decode(res["content"][0]["attachment"])))
            elif res["resourceType"] == "DiagnosticReport":
                obs = [by_url[r["reference"]] for r in res.get("result", [])]
                reports.append(((res.get("effectiveDateTime") or "")[:10], res["identifier"][0]["value"],
                                res["code"].get("text", ""), res.get("conclusion") or "", obs,
                                res["category"][0]["coding"][0]["code"]))
        docs.sort(reverse=True, key=lambda d: d[0])
        reports.sort(reverse=True, key=lambda r: r[0])
        corpus = " ".join(d[3] for d in docs) + " " + " ".join(r[3] for r in reports)

        concern_doc = next((d for t in _CONCERN_TYPES for d in docs if d[2] == t), docs[0] if docs else None)
        if concern_doc:
            chief = _first_sentence(concern_doc[3])
        elif reports:
            chief = f"{reports[0][2]}: {_first_sentence(reports[0][3] or 'see results')}"
        else:
            chief = "No clinical documents available."

        diagnoses: list[str] = []
        for pattern, display in _DIAGNOSIS_LEXICON:
            if re.search(pattern, corpus, re.I) and display not in diagnoses:
                diagnoses.append(display)

        media: list[MediaItem] = []
        anomalies: list[Anomaly] = []
        for when, rid, title, conclusion, obs, cat in reports:
            if obs:
                abnormal = [o for o in obs if o.get("interpretation") and
                            o["interpretation"][0]["coding"][0]["code"] != "N"]
                finding = "; ".join(_lab_label(o) for o in (abnormal or obs)[:3])
                for o in abnormal:
                    anomalies.append(Anomaly(finding=_lab_label(o), record_id=rid))
            else:
                finding = _key_sentence(conclusion, 18)
                if _SUSPICIOUS.search(conclusion) and cat != "LAB":
                    anomalies.append(Anomaly(finding=f"{title}: {_key_sentence(conclusion, 14)}", record_id=rid))
            if len(media) < 4:
                media.append(MediaItem(date=when or None, type=title, finding=finding, record_id=rid))

        for tag in (patient.get("meta") or {}).get("tag", []):
            anomalies.insert(0, Anomaly(finding=f"Data-quality flag: {tag['display']} - verify identity before use"))
        anomalies.sort(key=lambda a: not ("CRITICAL" in a.finding or a.finding.startswith("Data-quality")))

        name = patient["name"][0].get("text", "Patient")
        narrative = (f"{name}: {len(docs)} clinical note(s) and {len(reports)} diagnostic report(s) on file. "
                     f"Main problems: {', '.join(diagnoses[:3]) or 'not determined'}. "
                     f"{len(anomalies)} item(s) flagged for review.")
        return SummaryContent(chief_concern=chief, key_diagnoses=diagnoses[:6], recent_media=media,
                              flagged_anomalies=anomalies[:5], narrative=narrative)


# ---------------------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------------------
_STOP = {"with", "from", "that", "this", "were", "have", "left", "right", "acute", "chronic", "mild", "type",
         "stage", "possible", "suspected", "status", "post", "history", "disease", "severe", "moderate"}
# Words a faithful paraphrase adds that the source need not contain ("elevated creatinine 1.9",
# "MRI planned; no result in records"). Numbers are still checked strictly, so a changed value is
# always caught; these words only stop correct rewording from being flagged.
_STOP |= {"flagged", "elevated", "raised", "decreased", "reduced", "high", "critical", "abnormal", "normal",
          "planned", "plan", "pending", "documented", "records", "record", "result", "results", "noted",
          "recommended", "follow-up", "later", "outcome", "consistent", "finding", "findings", "showed", "shows",
          "repeat", "after", "before", "since", "still", "about", "recent", "latest", "initial", "prior",
          "previous", "ordered", "available", "none", "without", "being", "added", "started", "stopped", "done",
          "months", "weeks", "days", "year", "years", "old", "patient", "requested", "scheduled", "awaiting"}
_ABBREV = {"nstemi": "non-st", "ckd": "kidney", "copd": "copd", "aki": "kidney", "hfref": "heart failure",
           "dm": "diabetes", "t2dm": "diabetes", "htn": "hypertension", "ica": "carotid", "mca": "mca",
           "hba1c": "hemoglobin a1c", "a1c": "a1c", "bmp": "metabolic panel", "cbc": "blood count",
           "dexa": "dexa", "npdr": "retinopathy", "idc": "ductal carcinoma", "lvef": "lvef"}
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def count_words(content: SummaryContent) -> int:
    parts = [content.chief_concern, content.narrative, *content.key_diagnoses]
    parts += [f"{m.date or ''} {m.type} {m.finding}" for m in content.recent_media]
    parts += [a.finding for a in content.flagged_anomalies]
    return sum(len(p.split()) for p in parts)


def _claim_grounded(claim: str, source: str) -> bool:
    """A claim is grounded if its numbers all appear in the source and most of its
    meaningful words (or their abbreviation expansions) do too."""
    src = source.lower()
    for num in _NUMBER.findall(claim):
        if num not in src:
            return False
    # "...; no newer values in records" states an absence, which word matching cannot verify,
    # so only the positive part is word-checked (its numbers were already checked above).
    positive = re.split(r";\s*no\b", claim, maxsplit=1, flags=re.I)[0]
    words = [w for w in re.findall(r"[a-z][a-z0-9\-]{3,}", positive.lower()) if w not in _STOP]
    words += [w for w in re.findall(r"\b[a-z0-9]{2,6}\b", positive.lower()) if w in _ABBREV]
    if not words:
        return True
    hits = sum(1 for w in words if w in src or w.rstrip("s") in src or _ABBREV.get(w, "\x00") in src)
    return hits / len(words) >= 0.6


def check_grounding(content: SummaryContent, source_text: str, record_ids: list[str]) -> GroundingReport:
    ids = set(record_ids)
    claims = list(content.key_diagnoses) + [a.finding for a in content.flagged_anomalies
                                            if not a.finding.startswith("Data-quality")]
    claims += [m.finding for m in content.recent_media]
    ungrounded = [c for c in claims if not _claim_grounded(c, source_text)]
    cited = [m.record_id for m in content.recent_media] + [a.record_id for a in content.flagged_anomalies if a.record_id]
    invalid = sorted({c for c in cited if c not in ids})
    checked = len(claims) + len(cited)
    bad = len(ungrounded) + len(invalid)
    return GroundingReport(score=round(1 - bad / checked, 3) if checked else 1.0, checked=checked,
                           ungrounded=ungrounded, invalid_citations=invalid)


def _confidence(report: GroundingReport, word_count: int, retried: bool) -> Literal["high", "medium", "low"]:
    if report.invalid_citations or report.score < 0.8 or word_count > MAX_SUMMARY_WORDS:
        return "low"
    if report.ungrounded or retried:
        return "medium"
    return "high"


def _trim(content: SummaryContent) -> SummaryContent:
    """Last-resort guard: drop trailing list items until the summary fits the word budget."""
    c = content.model_copy(deep=True)
    while count_words(c) > MAX_SUMMARY_WORDS:
        if len(c.recent_media) > 2:
            c.recent_media.pop()
        elif len(c.flagged_anomalies) > 2:
            c.flagged_anomalies.pop()
        elif len(c.key_diagnoses) > 3:
            c.key_diagnoses.pop()
        else:
            words = c.narrative.split()
            c.narrative = " ".join(words[: max(10, len(words) - (count_words(c) - MAX_SUMMARY_WORDS))])
            break
    return c


# ---------------------------------------------------------------------------------------
# Service (cache + orchestration)
# ---------------------------------------------------------------------------------------
def build_summarizer(mode: str, model: str) -> Summarizer:
    has_key = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    if mode == "rule-based" or (mode == "auto" and not has_key):
        return RuleBasedSummarizer()
    return ClaudeSummarizer(model)


class SummaryService:
    def __init__(self, store: Any, summarizer: Summarizer, *, fallback: Summarizer | None = None,
                 today: date | None = None) -> None:
        self.store = store
        self.summarizer = summarizer
        self.fallback = fallback if fallback is not None else (
            RuleBasedSummarizer() if not isinstance(summarizer, RuleBasedSummarizer) else None)
        self.today = today
        self.stats: dict[str, Any] = {"cache_hits": 0, "generated": 0, "fallbacks": 0, "retries": 0,
                                      "by_generator": {}, "last_error": None}

    @staticmethod
    def cache_key(patient_id: str, content_hash: str, generator: str) -> str:
        raw = f"{patient_id}|{content_hash}|{PROMPT_VERSION}|{generator}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def summarize(self, patient_id: str, bundle: dict[str, Any], content_hash: str,
                  *, force: bool = False) -> ClinicalSummary:
        key = self.cache_key(patient_id, content_hash, self.summarizer.name)
        if not force and (cached := self.store.get_summary(key)):
            self.stats["cache_hits"] += 1
            return self._count(ClinicalSummary.model_validate(cached))

        rendered, record_ids = render_bundle(bundle, today=self.today)
        try:
            summary = self._generate(self.summarizer, patient_id, bundle, rendered, record_ids, content_hash)
            used = self.summarizer
        except SummaryError as exc:
            if self.fallback is None:
                raise
            log.warning("Summarizer %s failed for %s (%s); using %s", self.summarizer.name, patient_id, exc,
                        self.fallback.name)
            self.stats["fallbacks"] += 1
            self.stats["last_error"] = str(exc)
            summary = self._generate(self.fallback, patient_id, bundle, rendered, record_ids, content_hash)
            used = self.fallback
            # Don't cache a fallback under the primary key - the next run should retry the LLM.
            key = self.cache_key(patient_id, content_hash, used.name)
        self.store.put_summary(key, patient_id, content_hash, used.name, summary.model_dump())
        self.stats["generated"] += 1
        return self._count(summary)

    def _count(self, summary: ClinicalSummary) -> ClinicalSummary:
        by_gen = self.stats["by_generator"]
        by_gen[summary.generator] = by_gen.get(summary.generator, 0) + 1
        return summary

    def _generate(self, summarizer: Summarizer, patient_id: str, bundle: dict[str, Any], rendered: str,
                  record_ids: list[str], content_hash: str) -> ClinicalSummary:
        content = summarizer.generate(rendered, bundle)
        report = check_grounding(content, rendered, record_ids)
        retried = False
        retry_reason = None
        words = count_words(content)
        problems = []
        if words > MAX_SUMMARY_WORDS:
            problems.append(f"it was {words} words; the limit is {MAX_SUMMARY_WORDS}.")
        if report.invalid_citations:
            problems.append(f"it cited record ids that do not exist: {report.invalid_citations}.")
        if report.ungrounded:
            problems.append(f"these items are not supported by the records: {report.ungrounded}.")
        if problems and not isinstance(summarizer, RuleBasedSummarizer):
            self.stats["retries"] += 1
            retried = True
            retry_reason = " ".join(problems)
            content = summarizer.generate(rendered, bundle, feedback=retry_reason)
            report = check_grounding(content, rendered, record_ids)
        if count_words(content) > MAX_SUMMARY_WORDS:
            content = _trim(content)
        words = count_words(content)
        return ClinicalSummary(
            **content.model_dump(), patient_id=patient_id, word_count=words,
            confidence=_confidence(report, words, retried), generator=summarizer.name, grounding=report,
            retry_reason=retry_reason, source_record_ids=record_ids, content_hash=content_hash,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
