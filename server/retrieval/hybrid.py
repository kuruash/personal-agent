"""Hybrid retrieval: vector + BM25 + metadata-entity boost.

Scoring
-------
  hybrid = VECTOR_WEIGHT   * vector_norm
         + LEXICAL_WEIGHT  * lexical_norm
         + METADATA_WEIGHT * metadata_norm

Component scales
  vector_norm    — Chroma cosine similarity is already in [0, 1].
  lexical_norm   — BM25 rescaled to [0, 1] by dividing by the max raw
                   score in the current candidate set. If the max is 0
                   (no query term hit any doc), lexical_norm is 0 for
                   all docs.
  metadata_norm  — count of normalized query tokens that appear as
                   whole-word matches in a doc's entity-bearing metadata
                   fields, capped and rescaled to [0, 1].

Weights are constants but can be overridden via env vars
(PROFILE_VECTOR_WEIGHT / PROFILE_LEXICAL_WEIGHT / PROFILE_METADATA_WEIGHT)
without a code change.

Metadata boost intentionally requires exact whole-word presence. It
must NOT invent semantic bridges — RabbitMQ must not be scored higher
because SQS appears in metadata. Precision > recall for this channel.

Direct lookup still wins outside this module. Callers (form/chat) run
direct_lookup() first and only fall through to retrieve_hybrid() when
direct returns no confident answer.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..domain.profile.indexer import build_documents
from .lexical import score_all as _bm25_score_all
from .normalize import normalize_text, tokenize
from .vector import build_index, retrieve as _vector_retrieve


VECTOR_WEIGHT = float(os.environ.get("PROFILE_VECTOR_WEIGHT", "0.60"))
LEXICAL_WEIGHT = float(os.environ.get("PROFILE_LEXICAL_WEIGHT", "0.30"))
METADATA_WEIGHT = float(os.environ.get("PROFILE_METADATA_WEIGHT", "0.10"))

# Metadata fields that carry entity-level terms worth boosting for.
_ENTITY_META_KEYS = (
    "technologies", "domains", "items",
    "employer", "title", "name", "school", "field",
)

# Cap on the number of query tokens that count toward the metadata
# boost; rescale by this so metadata_norm stays in [0, 1].
_METADATA_MAX_HITS = 4


def _document_map() -> dict[str, dict]:
    """{doc_id: doc}. Cheap to recompute — indexer already caches
    profile.json reads implicitly through the loader."""
    return {d["id"]: d for d in build_documents()}


def _metadata_hit_count(query_tokens: list[str], doc: dict) -> int:
    """How many distinct query tokens appear as whole-word matches in
    the doc's entity-bearing metadata fields (already-normalized)."""
    if not query_tokens:
        return 0
    md = doc.get("metadata") or {}
    haystack_terms: list[str] = []
    for key in _ENTITY_META_KEYS:
        v = md.get(key)
        if isinstance(v, list):
            haystack_terms.extend(str(x) for x in v)
        elif isinstance(v, str) and v:
            haystack_terms.append(v)
    if not haystack_terms:
        return 0
    # Precompute normalized token bags of the metadata values so we can
    # check whole-token membership rather than substring (avoids
    # "amazon" matching "amazon web services" via substr but missing
    # the actual entity: for entity terms whole-word is right).
    haystack_tokens: set[str] = set()
    for term in haystack_terms:
        haystack_tokens.update(normalize_text(term).split())
    return sum(1 for t in set(query_tokens) if t in haystack_tokens)


async def retrieve_hybrid(question: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Vector + BM25 + metadata-entity boost. Debug fields included per
    hit so the eval harness can print each channel's contribution."""
    if not question or not question.strip():
        return []

    # Ensure the vector index exists; build_index is a no-op when the
    # profile fingerprint hasn't changed.
    await build_index()

    docs_by_id = _document_map()
    n = len(docs_by_id)
    if n == 0:
        return []

    # ---- vector: score every document (top_k = full corpus for hybrid) ----
    t_vec = time.perf_counter()
    vec_hits = await _vector_retrieve(question, top_k=n)
    vec_ms = (time.perf_counter() - t_vec) * 1000
    vec_score: dict[str, float] = {h["id"]: float(h["score"]) for h in vec_hits}

    # ---- lexical: BM25 across every document ----
    t_lex = time.perf_counter()
    lex_all = _bm25_score_all(question)
    lex_ms = (time.perf_counter() - t_lex) * 1000
    lex_raw: dict[str, float] = {h["id"]: float(h["score"]) for h in lex_all}
    lex_max = max(lex_raw.values(), default=0.0)
    lex_norm: dict[str, float] = {
        k: (v / lex_max if lex_max > 0 else 0.0) for k, v in lex_raw.items()
    }

    # ---- metadata boost ----
    t_meta = time.perf_counter()
    q_tokens = tokenize(question)
    meta_hits: dict[str, int] = {
        doc_id: _metadata_hit_count(q_tokens, doc)
        for doc_id, doc in docs_by_id.items()
    }
    meta_norm: dict[str, float] = {
        doc_id: min(1.0, hits / _METADATA_MAX_HITS)
        for doc_id, hits in meta_hits.items()
    }
    meta_ms = (time.perf_counter() - t_meta) * 1000

    # ---- combine ----
    combined: list[dict[str, Any]] = []
    for doc_id, doc in docs_by_id.items():
        v = vec_score.get(doc_id, 0.0)
        l = lex_norm.get(doc_id, 0.0)
        m = meta_norm.get(doc_id, 0.0)
        hybrid = VECTOR_WEIGHT * v + LEXICAL_WEIGHT * l + METADATA_WEIGHT * m
        combined.append({
            "id": doc_id,
            "doc_type": doc["type"],
            "vector_score": round(v, 4),
            "lexical_score": round(l, 4),
            "lexical_raw": round(lex_raw.get(doc_id, 0.0), 4),
            "metadata_score": round(m, 4),
            "metadata_hits": meta_hits.get(doc_id, 0),
            "hybrid_score": round(hybrid, 4),
            "text": doc["text"],
        })
    combined.sort(key=lambda x: x["hybrid_score"], reverse=True)

    top = combined[:top_k]
    # Attach per-call latency breakdown to the first result so callers
    # / the eval harness can surface it. Kept out of every row so the
    # returned list stays uniform in shape.
    if top:
        top[0]["_latency_ms"] = {
            "vector": round(vec_ms, 2),
            "lexical": round(lex_ms, 2),
            "metadata": round(meta_ms, 2),
        }
    return top


def hybrid_weights() -> dict[str, float]:
    return {
        "vector": VECTOR_WEIGHT,
        "lexical": LEXICAL_WEIGHT,
        "metadata": METADATA_WEIGHT,
    }
