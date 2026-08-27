"""Phase 2 memory.

One SQLite table holds structured metadata (url, title, question, answer,
tools, timestamp) plus a serialized normalized embedding vector. Vector recall
is cosine similarity over all rows via numpy — fine at Phase 2 scale
(thousands of interactions). We use Ollama's `nomic-embed-text` (768-dim) so
no extra embedding model is loaded into memory.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from langfuse import get_client, observe

DB_PATH = Path(__file__).parent / "memory.db"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embeddings"

# Same env var as server/tools.py — read here separately so this module has
# no cross-module dependency on tools.py (would be a cycle: tools -> memory).
_EMBED_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

# Cosine threshold. Nomic embeddings sit high for related content; below this
# the match is usually noise. Keep silent-skip behavior when nothing clears it.
MIN_SIM = 0.55
TOP_K = 3


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            tools TEXT NOT NULL,
            embedding BLOB NOT NULL
        )
        """
    )
    return conn


async def embed(text: str) -> np.ndarray:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            OLLAMA_EMBED_URL,
            json={
                "model": EMBED_MODEL,
                "prompt": text,
                "keep_alive": _EMBED_KEEP_ALIVE,
            },
        )
        r.raise_for_status()
        vec = np.array(r.json()["embedding"], dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


async def log_interaction(
    url: str, title: str, question: str, answer: str, tools: list[str]
) -> None:
    if not answer.strip():
        return
    text = f"{title}\n{question}\n{answer[:1500]}"
    vec = await embed(text)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO interactions (ts, url, title, question, answer, tools, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                url,
                title,
                question,
                answer,
                json.dumps(tools),
                vec.tobytes(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


@observe(as_type="retriever", name="memory.recall")
async def recall(
    query: str, current_url: str = "", top_k: int = TOP_K
) -> list[dict[str, Any]]:
    """Return past interactions most similar to `query`, above MIN_SIM."""
    qvec = await embed(query)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, ts, url, title, question, answer, tools, embedding "
            "FROM interactions"
        ).fetchall()
    finally:
        conn.close()

    all_sims: list[float] = []
    scored: list[tuple[float, dict[str, Any]]] = []
    for id_, ts, url, title, question, answer, tools, blob in rows:
        vec = np.frombuffer(blob, dtype=np.float32)
        if vec.shape != qvec.shape:
            continue
        sim = float(qvec @ vec)
        all_sims.append(sim)
        if sim < MIN_SIM:
            continue
        scored.append((
            sim,
            {
                "id": id_,
                "ts": ts,
                "url": url,
                "title": title,
                "question": question,
                "answer": answer,
                "tools": json.loads(tools),
                "sim": round(sim, 3),
                "same_url": url == current_url,
            },
        ))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [item for _, item in scored[:top_k]]

    top_sim = max(all_sims) if all_sims else 0.0
    get_client().update_current_span(
        metadata={
            "min_sim_threshold": MIN_SIM,
            "rows_scanned": len(rows),
            "top_similarity": round(top_sim, 3),
            "cleared_threshold": bool(scored),
            "all_similarities": [round(s, 3) for s in sorted(all_sims, reverse=True)[:10]],
            "returned_count": len(results),
            "returned_sims": [m["sim"] for m in results],
        },
    )
    return results


def format_recall_for_prompt(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return ""
    lines = ["Relevant past interactions (from your memory):"]
    for m in matches:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"]))
        lines.append(
            f"- [{when}] {m['title']} ({m['url']})\n"
            f"  Q: {m['question']}\n"
            f"  A: {m['answer'][:400]}"
        )
    return "\n".join(lines)
