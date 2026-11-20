"""AI summarization: schema, word limit, grounding, cache and fallback (Task 3).

The Claude client is replaced by fakes so these tests are free, offline and deterministic.
"""

import json
from types import SimpleNamespace

import pytest

from app.store import Store
from app.summarization.prompt import MAX_SUMMARY_WORDS, render_bundle
from app.summarization.summarizer import (
    DISCLAIMER,
    ClaudeSummarizer,
    RuleBasedSummarizer,
    SummaryContent,
    SummaryError,
    SummaryService,
    check_grounding,
    count_words,
)

PID = "pt-0089012"  # Samuel Adeyemi: critical potassium + renal artery stenosis


@pytest.fixture()
def bundle(bundles):
    return bundles[PID]


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "s.db")


GOOD = SummaryContent.model_validate({
    "chief_concern": "Poorly controlled hypertension with critical hyperkalemia.",
    "key_diagnoses": ["Hypertension", "Renal artery stenosis"],
    "recent_media": [
        {"date": "2026-07-18", "type": "Renal artery duplex", "finding": "Greater than 60% right renal artery stenosis",
         "record_id": "SCN-eb7d189579"},
        {"date": "2026-07-11", "type": "BMP", "finding": "Potassium 6.3 mmol/L critical high; creatinine 1.9 mg/dL",
         "record_id": "LAB-ACC-5501"},
    ],
    "flagged_anomalies": [{"finding": "Potassium 6.3 mmol/L (critical)", "record_id": "LAB-ACC-5501"}],
    "narrative": "Resistant hypertension with hyperkalemia on spironolactone; duplex shows renal artery stenosis.",
})


class FakeSummarizer:
    """Stands in for ClaudeSummarizer; returns queued responses and records calls."""

    name = "claude:fake"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, rendered, bundle, feedback=None):
        self.calls.append(feedback)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def test_render_bundle_includes_ids_text_and_flags(bundles):
    text, ids = render_bundle(bundles["pt-0045678"])
    assert "PATIENT: Robert Williams" in text and "possible_duplicate_mrn" in text
    assert "[LAB-ACC-5301]" in text and "NT-proBNP: 6800 pg/mL (ref <450), high" in text and "**" not in text
    assert "Pulmonary edema" in text
    assert set(ids) >= {"LAB-ACC-5301", "D-4001-A"}


def test_summary_has_required_fields_disclaimer_and_word_limit(bundle, store):
    summary = SummaryService(store, FakeSummarizer(GOOD)).summarize(PID, bundle, "h1")
    assert summary.chief_concern and summary.key_diagnoses and summary.recent_media and summary.flagged_anomalies
    assert summary.disclaimer == DISCLAIMER and "not a clinical decision" in summary.disclaimer
    assert summary.word_count <= MAX_SUMMARY_WORDS
    assert summary.confidence == "high" and summary.grounding.score == 1.0


def test_cache_prevents_redundant_api_calls(bundle, store):
    fake = FakeSummarizer(GOOD)
    service = SummaryService(store, fake)
    first = service.summarize(PID, bundle, "hash-A")
    second = service.summarize(PID, bundle, "hash-A")
    assert len(fake.calls) == 1 and service.stats["cache_hits"] == 1
    assert first == second
    # A new record hash (records changed) must trigger regeneration
    service.summarize(PID, bundle, "hash-B")
    assert len(fake.calls) == 2


def test_cache_survives_process_restart(bundle, store):
    SummaryService(store, FakeSummarizer(GOOD)).summarize(PID, bundle, "h")
    fake = FakeSummarizer(GOOD)
    SummaryService(store, fake).summarize(PID, bundle, "h")
    assert fake.calls == []


def test_over_long_summary_is_retried_then_trimmed(bundle, store):
    long = GOOD.model_copy(update={"narrative": "word " * 250})
    fake = FakeSummarizer(long, long)
    summary = SummaryService(store, fake).summarize(PID, bundle, "h")
    assert len(fake.calls) == 2 and "limit is 200" in fake.calls[1]
    assert "limit is 200" in summary.retry_reason
    assert summary.word_count <= MAX_SUMMARY_WORDS
    assert summary.confidence != "high"


def test_hallucinated_diagnosis_is_caught_and_retried(bundle, store):
    bad = GOOD.model_copy(update={"key_diagnoses": ["Hypertension", "Pulmonary embolism"]})
    fake = FakeSummarizer(bad, GOOD)
    summary = SummaryService(store, fake).summarize(PID, bundle, "h")
    assert "Pulmonary embolism" in fake.calls[1]
    assert summary.key_diagnoses == GOOD.key_diagnoses and summary.confidence == "medium"


def test_grounding_flags_invented_numbers_and_bad_citations(bundle):
    rendered, ids = render_bundle(bundle)
    bad = SummaryContent.model_validate(GOOD.model_dump() | {"flagged_anomalies": [
        {"finding": "Potassium 7.9 mmol/L", "record_id": "LAB-ACC-5501"},
        {"finding": "Potassium 6.3 mmol/L", "record_id": "LAB-DOES-NOT-EXIST"}]})
    report = check_grounding(bad, rendered, ids)
    assert "Potassium 7.9 mmol/L" in report.ungrounded
    assert report.invalid_citations == ["LAB-DOES-NOT-EXIST"]
    assert report.score < 1


def test_llm_failure_falls_back_to_rule_based(bundle, store):
    service = SummaryService(store, FakeSummarizer(SummaryError("API down")))
    summary = service.summarize(PID, bundle, "h")
    assert summary.generator == RuleBasedSummarizer.name and service.stats["fallbacks"] == 1
    assert service.stats["by_generator"] == {RuleBasedSummarizer.name: 1}
    assert service.stats["last_error"] == "API down"
    # The fallback is not cached under the LLM's key, so the next run retries the LLM.
    fake = FakeSummarizer(GOOD)
    SummaryService(store, fake).summarize(PID, bundle, "h")
    assert len(fake.calls) == 1


ALL_PATIENTS = ["pt-0012345", "pt-0023456", "pt-0034567", "pt-0045678", "pt-0056789", "pt-0067890",
                "pt-0078901", "pt-0089012", "pt-0099012", "pt-0101123", "pt-0112234", "pt-0123345"]


@pytest.mark.parametrize("pid", ALL_PATIENTS)
def test_rule_based_summaries_are_grounded_and_short(bundles, pid):
    """Backs the write-up claim: all 12 summaries are fully grounded, under 200 words, <= 6 diagnoses
    (per-patient numbers are recorded in docs/summary_review.md)."""
    rendered, ids = render_bundle(bundles[pid])
    content = RuleBasedSummarizer().generate(rendered, bundles[pid])
    assert count_words(content) <= MAX_SUMMARY_WORDS
    assert len(content.key_diagnoses) <= 6
    report = check_grounding(content, rendered, ids)
    assert report.score == 1.0 and not report.invalid_citations, report


def test_mammogram_finding_uses_the_significant_sentence(bundles):
    """Regression from manual review: the mammogram report has no IMPRESSION section and its first
    sentence ("Diagnostic mammogram, left breast.") says nothing; the BI-RADS sentence must be used."""
    content = RuleBasedSummarizer().generate(render_bundle(bundles["pt-0099012"])[0], bundles["pt-0099012"])
    mammo = next(m for m in content.recent_media if m.record_id == "D-9001-A")
    assert "BI-RADS 4C" in mammo.finding
    assert mammo.finding != "Diagnostic mammogram, left breast."


def test_rule_based_flags_critical_lab_first(bundle):
    content = RuleBasedSummarizer().generate(*render_bundle(bundle)[:1], bundle)
    assert "CRITICAL" in content.flagged_anomalies[0].finding
    assert "Renal artery stenosis" in content.key_diagnoses


# ---------------------------------------------------------------- Claude adapter (mocked HTTP client)
class _FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _client(stop_reason="end_turn", text=None):
    block = SimpleNamespace(type="text", text=text if text is not None else GOOD.model_dump_json())
    messages = _FakeMessages(SimpleNamespace(stop_reason=stop_reason, content=[block]))
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_claude_adapter_sends_structured_output_request_and_parses():
    client, messages = _client()
    result = ClaudeSummarizer("claude-sonnet-5-5", client=client).generate("[X] record", {})
    assert result == GOOD
    kw = messages.kwargs
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert set(kw["output_config"]["format"]["schema"]["required"]) == set(SummaryContent.model_fields)
    assert "<records>" in kw["messages"][0]["content"] and "ONLY facts" in kw["system"]


@pytest.mark.parametrize("stop_reason, text, msg", [("refusal", "", "declined"), ("max_tokens", "{}", "truncated"),
                                                     ("end_turn", json.dumps({"x": 1}), "schema")])
def test_claude_adapter_errors_become_summary_errors(stop_reason, text, msg):
    client, _ = _client(stop_reason, text)
    with pytest.raises(SummaryError, match=msg):
        ClaudeSummarizer("claude-sonnet-5-5", client=client).generate("x", {})


@pytest.mark.parametrize("claim", [
    "WBC 16.4 10*3/uL (ref 4.0-11.0) flagged HIGH",   # Claude wording that the first checker rejected
    "Elevated creatinine (1.9 mg/dL)",
    "Breast MRI planned; no result in records",
    "HbA1c 10.8 % (ref 4.0-5.6), high",
    "Repeat BMP in 1 week requested; no result in records",
    "Latest eGFR 27 mL/min/1.73m2 and A1c 10.8 %; no newer values",
])
def test_faithful_paraphrase_is_not_flagged(bundles, claim):
    """False positives found when reviewing real Claude output: correct statements reworded."""
    rendered = "\n".join(render_bundle(bundles[p])[0] for p in ("pt-0078901", "pt-0089012", "pt-0099012",
                                                                       "pt-0012345"))
    content = SummaryContent.model_validate(GOOD.model_dump() | {"key_diagnoses": [claim], "recent_media": [],
                                                                 "flagged_anomalies": []})
    assert check_grounding(content, rendered, []).ungrounded == []


def test_patient_level_flag_can_be_cited(bundles):
    """Robert Williams' duplicate-MRN flag is not a record; the model is told to cite PATIENT,
    which must count as a valid citation (Claude once invented 'DATA-QUALITY FLAGS' instead)."""
    rendered, ids = render_bundle(bundles["pt-0045678"])
    assert "[PATIENT] DATA-QUALITY FLAGS: possible_duplicate_mrn" in rendered and "PATIENT" in ids
    content = SummaryContent.model_validate(GOOD.model_dump() | {"recent_media": [], "flagged_anomalies": [
        {"finding": "possible duplicate MRN", "record_id": "PATIENT"}]})
    assert check_grounding(content, rendered, ids).invalid_citations == []


def test_absence_clause_still_checks_numbers(bundles):
    """Skipping word checks after "; no ..." must not let an invented value through."""
    rendered, ids = render_bundle(bundles["pt-0012345"])
    content = SummaryContent.model_validate(GOOD.model_dump() | {"recent_media": [], "flagged_anomalies": [],
                                                                 "key_diagnoses": ["eGFR 27; no value below 19"]})
    assert check_grounding(content, rendered, ids).ungrounded == ["eGFR 27; no value below 19"]


def test_committed_claude_summaries_pass_quality_checks(bundles):
    """The real Claude output reviewed in docs/summary_review.md (stored in docs/claude_summaries.json)."""
    from app.config import PROJECT_ROOT
    from app.summarization.summarizer import ClinicalSummary

    stored = json.loads((PROJECT_ROOT / "docs" / "claude_summaries.json").read_text(encoding="utf-8"))
    assert set(stored) == set(bundles)
    for pid, raw in stored.items():
        s = ClinicalSummary.model_validate(raw)
        assert s.generator.startswith("claude:") and s.disclaimer == DISCLAIMER
        assert s.word_count <= MAX_SUMMARY_WORDS and len(s.key_diagnoses) <= 6
        rendered, ids = render_bundle(bundles[pid])
        report = check_grounding(SummaryContent.model_validate(s.model_dump()), rendered, ids)
        assert report.score == 1.0 and not report.invalid_citations, (pid, report)
        assert "**" not in json.dumps(raw)
