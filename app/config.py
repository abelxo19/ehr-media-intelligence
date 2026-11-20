"""Central configuration, read from environment variables with sane local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so I don't need python-dotenv just for one key."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    raw_data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("EHR_RAW_DATA_DIR", PROJECT_ROOT / "data" / "raw"))
    )
    var_dir: Path = field(default_factory=lambda: Path(os.getenv("EHR_VAR_DIR", PROJECT_ROOT / "var")))
    # "auto" -> Claude if a key is configured, otherwise the offline rule-based summarizer.
    summarizer: str = field(default_factory=lambda: os.getenv("EHR_SUMMARIZER", "auto"))
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))
    embedding_model: str = field(
        default_factory=lambda: os.getenv("EHR_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    )

    @property
    def db_path(self) -> Path:
        return self.var_dir / "ehr.db"

    @property
    def index_dir(self) -> Path:
        return self.var_dir / "index"

    @property
    def reports_dir(self) -> Path:
        return self.var_dir / "reports"


def get_settings() -> Settings:
    return Settings()
