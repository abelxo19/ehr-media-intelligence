from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings
from app.ingestion.pipeline import run_ingestion

TODAY = date(2026, 11, 20)  # fixed "today" so date-sanity checks are deterministic
RAW_DIR = PROJECT_ROOT / "data" / "raw"


@pytest.fixture(scope="session")
def today() -> date:
    return TODAY


@pytest.fixture(scope="session")
def dataset():
    return run_ingestion(RAW_DIR, today=TODAY)


@pytest.fixture(scope="session")
def bundles(dataset):
    from app.fhir.mapper import build_all_bundles

    return build_all_bundles(dataset)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(raw_data_dir=RAW_DIR, var_dir=tmp_path / "var", summarizer="rule-based")


@pytest.fixture(scope="module")
def module_settings(tmp_path_factory) -> Settings:
    return Settings(raw_data_dir=RAW_DIR, var_dir=tmp_path_factory.mktemp("var"), summarizer="rule-based")


@pytest.fixture(scope="session")
def hashing_embedder():
    from app.search.embedder import HashingEmbedder

    return HashingEmbedder()


@pytest.fixture(scope="module")
def client(module_settings, hashing_embedder):
    """API client over the sample data with a fast hashing embedder and offline summaries."""
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.summarization.summarizer import RuleBasedSummarizer

    app = create_app(module_settings, embedder=hashing_embedder, summarizer=RuleBasedSummarizer())
    with TestClient(app) as c:
        yield c
