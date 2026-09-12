"""SEARCH / REASON evidence packaging.

Takes a field, runs hybrid retrieval, thresholds hits by score, and
dedupes them via `related_ids` so the batched Qwen call sees at most
one representative doc per overlapping cluster. The actual Qwen prompt
is built in `prompt_builder.py` and dispatched by the orchestrator.
"""

from __future__ import annotations

import json


def dedupe_by_related(hits: list[dict]) -> list[dict]:
    """When two hits are related_ids of each other (e.g. inroads_ai_engineer
    ↔ nl_to_sql_bi_engine), keep the higher-scoring one and drop the other
    from evidence to avoid feeding duplicate signal into Qwen."""
    dropped: set[str] = set()
    kept: list[dict] = []
    for h in hits:
        if h["id"] in dropped:
            continue
        rel_json = (h.get("metadata") or {}).get("related_ids_json")
        rel_ids: list[str] = []
        if rel_json:
            try:
                rel_ids = json.loads(rel_json)
            except Exception:
                rel_ids = []
        for other in hits:
            if other is h or other["id"] in dropped:
                continue
            if other["id"] in rel_ids and other["hybrid_score"] < h["hybrid_score"]:
                dropped.add(other["id"])
        kept.append(h)
    return kept
