"""FAISS vector index over FHIR document text and AI summaries.

Each indexed item is either a clinical record (DocumentReference / DiagnosticReport) or a
patient's AI summary. Long texts are split into overlapping word windows; a record's score
is its best-matching chunk (max-pooling), so a long note is not penalised for covering
several topics.

Ranking is hybrid: ``score = (1 - LEXICAL_WEIGHT) * cosine + LEXICAL_WEIGHT * keyword_overlap``.
Cosine similarity (L2-normalised embeddings, inner-product index) carries the meaning
("heart attack" finds "NSTEMI"); the small keyword term rewards documents that contain the
exact clinical words typed ("critical", "potassium"), which MiniLM alone under-weights.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import faiss
import numpy as np

from app.search.embedder import Embedder

CHUNK_WORDS = 120
CHUNK_OVERLAP = 30
LEXICAL_WEIGHT = 0.2
_STOPWORDS = {"the", "and", "for", "with", "from", "that", "this", "patient", "patients", "any", "all", "show",
              "find", "records", "record", "who", "has", "have", "was", "were", "are", "into", "over", "about"}


@dataclass
class IndexedDoc:
    doc_id: str  # record_id, or "summary:<patient_id>"
    kind: str  # "record" | "ai_summary"
    resource_type: str  # DocumentReference | DiagnosticReport | Patient (for AI summaries)
    fhir_id: str
    patient_id: str
    title: str
    category: str
    record_date: str | None  # ISO date
    text: str


@dataclass
class Hit:
    doc: IndexedDoc
    score: float
    chunk: str
    semantic_score: float = 0.0
    keyword_score: float = 0.0


def query_terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9\-]+", query.lower()) if len(t) >= 3 and t not in _STOPWORDS]


def keyword_overlap(terms: list[str], text: str) -> float:
    """Fraction of query terms that appear in the text as a word prefix ("nodule" ~ "nodules")."""
    if not terms:
        return 0.0
    lowered = text.lower()
    return sum(1 for t in terms if re.search(r"\b" + re.escape(t), lowered)) / len(terms)


def chunk_text(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    words = text.split()
    if len(words) <= size:
        return [text]
    step = size - overlap
    return [" ".join(words[i:i + size]) for i in range(0, max(len(words) - overlap, 1), step)]


class VectorIndex:
    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self.docs: list[IndexedDoc] = []
        self.chunk_doc: list[int] = []  # chunk position -> doc position
        self.chunks: list[str] = []
        self.index: faiss.Index = faiss.IndexFlatIP(embedder.dim)

    # -- build -------------------------------------------------------------------------------
    def build(self, docs: list[IndexedDoc]) -> None:
        self.docs = list(docs)
        self.chunks, self.chunk_doc = [], []
        for i, doc in enumerate(self.docs):
            for chunk in chunk_text(doc.text):
                # Prefix the title so short lab chunks keep their context.
                self.chunks.append(f"{doc.title}. {chunk}")
                self.chunk_doc.append(i)
        self.index = faiss.IndexFlatIP(self.embedder.dim)
        if self.chunks:
            self.index.add(self.embedder.encode(self.chunks))

    @property
    def size(self) -> int:
        return len(self.docs)

    # -- query -------------------------------------------------------------------------------
    def search(self, query: str, *, top_k: int = 5, resource_types: set[str] | None = None,
               date_from: date | None = None, date_to: date | None = None) -> tuple[list[Hit], int, float]:
        """Return (hits, candidates_after_filter, elapsed_ms)."""
        start = time.perf_counter()
        if not self.chunks:
            return [], 0, 0.0
        qvec = self.embedder.encode([query])
        # Exact search over every chunk: at this scale (hundreds of vectors) a flat index is
        # sub-millisecond and lets filters apply *before* the top-k cut, so filtered queries
        # never come back short.
        scores, positions = self.index.search(qvec, len(self.chunks))
        best: dict[int, tuple[float, int]] = {}
        for score, pos in zip(scores[0], positions[0]):
            if pos < 0:
                continue
            d = self.chunk_doc[pos]
            if d not in best or score > best[d][0]:
                best[d] = (float(score), int(pos))

        terms = query_terms(query)
        hits: list[Hit] = []
        for d, (cosine, pos) in best.items():
            doc = self.docs[d]
            if resource_types and doc.resource_type not in resource_types:
                continue
            if date_from or date_to:
                if not doc.record_date:
                    continue
                when = date.fromisoformat(doc.record_date[:10])
                if (date_from and when < date_from) or (date_to and when > date_to):
                    continue
            lexical = keyword_overlap(terms, f"{doc.title} {doc.text}")
            score = (1 - LEXICAL_WEIGHT) * cosine + LEXICAL_WEIGHT * lexical
            hits.append(Hit(doc=doc, score=score, chunk=self.chunks[pos].split(". ", 1)[-1],
                            semantic_score=cosine, keyword_score=lexical))
        candidates = len(hits)
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k], candidates, (time.perf_counter() - start) * 1000

    # -- persistence -------------------------------------------------------------------------
    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(directory / "records.faiss"))
        (directory / "meta.json").write_text(json.dumps({
            "embedder": self.embedder.name,
            "dim": self.embedder.dim,
            "docs": [asdict(d) for d in self.docs],
            "chunks": self.chunks,
            "chunk_doc": self.chunk_doc,
        }), encoding="utf-8")

    @classmethod
    def load(cls, directory: Path, embedder: Embedder) -> "VectorIndex":
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        if meta["embedder"] != embedder.name or meta["dim"] != embedder.dim:
            raise ValueError(f"Index was built with {meta['embedder']}; rebuild it for {embedder.name}")
        idx = cls(embedder)
        idx.docs = [IndexedDoc(**d) for d in meta["docs"]]
        idx.chunks = meta["chunks"]
        idx.chunk_doc = meta["chunk_doc"]
        idx.index = faiss.read_index(str(directory / "records.faiss"))
        return idx
