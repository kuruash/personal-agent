"""In-process BM25 over the same normalized profile documents used by
the vector index. Small enough (~20 docs, thousands of tokens) that
recomputing IDF and scoring every document at query time is cheap.

Rebuilt lazily when the profile fingerprint changes. No new dependency
— rank-bm25 would be one option; a ~40-line custom BM25 is easier to
audit and doesn't pull in extra transitive deps.

BM25 parameters follow the canonical defaults (Robertson & Zaragoza):
  k1 = 1.5    — term-frequency saturation
  b  = 0.75   — length normalization
"""

from __future__ import annotations

import math
from typing import Any

from ..domain.profile.indexer import build_documents
from ..domain.profile.repository import profile_fingerprint
from .normalize import tokenize

K1 = 1.5
B = 0.75


class _BM25Index:
    __slots__ = (
        "fingerprint", "doc_ids", "doc_types", "doc_lengths",
        "avg_dl", "term_freqs", "doc_freq", "idf", "n_docs",
    )

    def __init__(self) -> None:
        self.fingerprint = ""
        self.doc_ids: list[str] = []
        self.doc_types: list[str] = []
        self.doc_lengths: list[int] = []
        self.avg_dl: float = 0.0
        # term_freqs[i] = {term: count} for doc i
        self.term_freqs: list[dict[str, int]] = []
        # doc_freq[term] = number of docs containing term
        self.doc_freq: dict[str, int] = {}
        self.idf: dict[str, float] = {}
        self.n_docs: int = 0


_INDEX: _BM25Index | None = None


def _build() -> _BM25Index:
    docs = build_documents()
    idx = _BM25Index()
    idx.n_docs = len(docs)
    idx.fingerprint = profile_fingerprint()

    total_len = 0
    for d in docs:
        # Tokenize the full text plus a light re-inclusion of the doc's
        # metadata list-values so entity terms count in TF. Without this,
        # skill-group docs (which mostly ARE their metadata) still get a
        # signal but work/project docs stay dominated by prose tokens.
        surface = d["text"]
        md = d.get("metadata") or {}
        for key in ("technologies", "domains", "items", "name",
                    "employer", "title", "school"):
            v = md.get(key)
            if isinstance(v, list):
                surface += " " + " ".join(str(x) for x in v)
            elif isinstance(v, str) and v:
                surface += " " + v

        tokens = tokenize(surface)
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1

        idx.doc_ids.append(d["id"])
        idx.doc_types.append(d["type"])
        idx.doc_lengths.append(len(tokens))
        idx.term_freqs.append(counts)
        total_len += len(tokens)
        for term in counts:
            idx.doc_freq[term] = idx.doc_freq.get(term, 0) + 1

    idx.avg_dl = (total_len / idx.n_docs) if idx.n_docs else 0.0
    # BM25 IDF with the +1 in the log to keep it non-negative for rare terms.
    idx.idf = {
        term: math.log(1 + (idx.n_docs - df + 0.5) / (df + 0.5))
        for term, df in idx.doc_freq.items()
    }
    return idx


def _get_index() -> _BM25Index:
    global _INDEX
    fp = profile_fingerprint()
    if _INDEX is None or _INDEX.fingerprint != fp:
        _INDEX = _build()
    return _INDEX


def score_all(question: str) -> list[dict[str, Any]]:
    """Return one entry per profile doc with its raw BM25 score. Sorted
    highest-first. Zero-score docs are included so hybrid can still see
    them (and metadata boost can still contribute)."""
    idx = _get_index()
    q_tokens = tokenize(question)
    if not q_tokens:
        return [
            {"id": idx.doc_ids[i], "type": idx.doc_types[i], "score": 0.0}
            for i in range(idx.n_docs)
        ]

    results: list[dict[str, Any]] = []
    for i in range(idx.n_docs):
        dl = idx.doc_lengths[i] or 1
        tf = idx.term_freqs[i]
        score = 0.0
        for term in q_tokens:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = idx.idf.get(term, 0.0)
            num = f * (K1 + 1)
            den = f + K1 * (1 - B + B * dl / idx.avg_dl)
            score += idf * num / den
        results.append({
            "id": idx.doc_ids[i],
            "type": idx.doc_types[i],
            "score": round(score, 4),
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def lexical_search(question: str, top_k: int = 5) -> list[dict[str, Any]]:
    return [r for r in score_all(question) if r["score"] > 0][:top_k]


def index_stats() -> dict[str, Any]:
    idx = _get_index()
    return {
        "n_docs": idx.n_docs,
        "avg_doc_length": round(idx.avg_dl, 1),
        "vocab_size": len(idx.doc_freq),
        "fingerprint": idx.fingerprint[:12],
    }
