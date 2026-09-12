"""Vector retrieval over normalized profile documents.

Milestone 3-4 scope: embed + Chroma + retrieve(question, top_k). No
reranking, no hybrid scoring, no multi-query — those come later, only
if the eval harness in `scripts/eval_retrieval.py` shows real gaps.

Design notes
------------
Embeddings come from the native Ollama via `llm.ollama_client.embed`.
The nomic-embed-text vectors are already L2-normalized there, so we
can rely on cosine distance in Chroma.

Chroma persists at `server/data/chroma/`. The bind-mount from
`compose.yaml` (./server → /app/server) means the on-disk index
survives container rebuilds without extra config.

Reindex is decided by comparing the profile's SHA-256 fingerprint to
the last one recorded inside the collection's metadata. If they
differ (or the collection is empty), we wipe the collection and
rebuild — cheap at profile scale (~20 docs).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

from ..domain.profile.indexer import build_documents, documents_by_type
from ..domain.profile.repository import profile_fingerprint
from ..llm.ollama_client import embed

# server/data/chroma — anchor to the top of the server package so the
# bind mount from compose.yaml keeps working after the module move.
CHROMA_DIR = Path(__file__).resolve().parent.parent / "data" / "chroma"
COLLECTION = "profile"

# Chroma requires primitive-valued metadata. Lists/dicts must be serialized.
# We stash them here in JSON so retrieval users can re-parse if needed.
_LIST_METADATA_KEYS = ("technologies", "domains", "coursework", "items", "related_ids")


def _client() -> chromadb.PersistentClient:
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False, allow_reset=True),
    )


def _flatten_metadata(md: dict) -> dict:
    """Chroma metadata values must be str | int | float | bool. Lists get
    joined with '|' for filterable containment; the original list is also
    stashed as JSON under the same key + '_json' for reconstruction."""
    import json as _json
    out: dict[str, Any] = {}
    for k, v in md.items():
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif v is None:
            out[k] = ""
        elif isinstance(v, list):
            out[k] = " | ".join(str(x) for x in v)
            out[f"{k}_json"] = _json.dumps(v)
        else:
            out[k] = _json.dumps(v)
    return out


async def _embed_docs(docs: list[dict]) -> list[list[float]]:
    """Embed each document's text. Sequential — chroma default embedding
    fn also runs one call per doc, and Ollama's serial throughput is
    the bottleneck either way."""
    vecs: list[list[float]] = []
    for d in docs:
        v = await embed(d["text"])
        vecs.append(v.tolist())
    return vecs


async def build_index(force: bool = False) -> dict:
    """(Re)build the Chroma collection from the current profile.

    Returns a small stats dict. Skips work when the profile fingerprint
    matches what's already indexed (unless force=True)."""
    t0 = time.perf_counter()
    fp = profile_fingerprint()
    client = _client()
    existing = _get_collection(client)
    current_fp = ""
    if existing is not None:
        current_fp = (existing.metadata or {}).get("profile_fingerprint", "")
        if not force and current_fp == fp and existing.count() > 0:
            return {
                "rebuilt": False,
                "reason": "fingerprint unchanged",
                "documents": existing.count(),
                "fingerprint": fp[:12],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        # Purge before rebuild.
        client.delete_collection(COLLECTION)

    docs = build_documents()
    collection = client.create_collection(
        name=COLLECTION,
        metadata={"profile_fingerprint": fp, "hnsw:space": "cosine"},
    )

    vecs = await _embed_docs(docs)
    collection.add(
        ids=[d["id"] for d in docs],
        embeddings=vecs,
        documents=[d["text"] for d in docs],
        metadatas=[_flatten_metadata(d["metadata"]) for d in docs],
    )

    return {
        "rebuilt": True,
        "reason": "fingerprint changed" if current_fp else "no prior index",
        "documents": len(docs),
        "documents_by_type": documents_by_type(docs),
        "fingerprint": fp[:12],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _get_collection(client: chromadb.PersistentClient):
    try:
        return client.get_collection(COLLECTION)
    except Exception:  # noqa: BLE001 — Chroma raises different errors across versions
        return None


async def retrieve(question: str, top_k: int = 5) -> list[dict]:
    """Return top_k nearest documents to `question`.

    Each hit:
        {
            "id":       "<doc id>",
            "score":    <similarity in [0, 1] — higher is better>,
            "distance": <raw cosine distance from Chroma>,
            "type":     "<doc type>",
            "text":     "<full text — caller decides how much to preview>",
            "metadata": { ... }
        }
    """
    if not question or not question.strip():
        return []
    client = _client()
    collection = _get_collection(client)
    if collection is None or collection.count() == 0:
        await build_index()
        collection = client.get_collection(COLLECTION)

    qvec = (await embed(question)).tolist()
    res = collection.query(
        query_embeddings=[qvec],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )
    ids = res["ids"][0]
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = res["distances"][0]

    out: list[dict] = []
    for i, doc_id in enumerate(ids):
        distance = float(dists[i])
        # Chroma's cosine distance is 1 - cosine_similarity for
        # normalized vectors. Clamp to [0,1] as a defensive floor.
        similarity = max(0.0, min(1.0, 1.0 - distance))
        meta = metas[i] or {}
        out.append({
            "id": doc_id,
            "score": round(similarity, 4),
            "distance": round(distance, 4),
            "type": meta.get("doc_type", ""),
            "text": docs[i] or "",
            "metadata": meta,
        })
    return out


def index_stats() -> dict:
    """Small dict for the eval harness / logging. Never triggers a rebuild."""
    client = _client()
    coll = _get_collection(client)
    if coll is None:
        return {"exists": False, "documents": 0}
    return {
        "exists": True,
        "documents": coll.count(),
        "profile_fingerprint": (coll.metadata or {}).get("profile_fingerprint", "")[:12],
        "path": str(CHROMA_DIR),
    }


if __name__ == "__main__":
    async def _main() -> None:
        stats = await build_index()
        print(stats)
    asyncio.run(_main())
