"""Ollama HTTP client — single owner of communication with the local
Ollama server.

Owns:
  - the endpoint URL, model name, and keep-alive constants,
  - `_ollama_generate`  — plain /api/generate call for prose,
  - `_ollama_json`      — /api/generate with format=json for structured
                          batched form answers,
  - `embed`             — /api/embeddings via nomic-embed-text,
  - `ollama_perf_metadata` — extract Ollama's per-call performance
                            counters for Langfuse generation spans.

Anything higher-level (routing, retrieval, form answering) lives above
this module. This file must not import from workflows/, tools/, or the
domain layer.
"""

from __future__ import annotations

import os

import httpx
import numpy as np

from ..observability import get_client, observe


OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_GENERATE_URL = f"{OLLAMA_BASE_URL}/api/generate"
OLLAMA_CHAT_URL = f"{OLLAMA_BASE_URL}/api/chat"
OLLAMA_EMBEDDINGS_URL = f"{OLLAMA_BASE_URL}/api/embeddings"

MODEL = "qwen2.5:7b"
EMBED_MODEL = "nomic-embed-text"

# Keep the model resident between requests so the second /ask in a session
# doesn't pay the ~5-15s load penalty again. Configurable via env so it can
# be dialed down on machines with less RAM. Ollama accepts "-1" for indefinite,
# "0" for evict-immediately, or a duration string like "30m" / "1h".
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

# Per-tool budgets consumed by tools/impls.py.
MAX_PAGE_CHARS = 12000
CHUNK_CHARS = 1500


def ollama_perf_metadata(data: dict) -> dict:
    """Extract Ollama's per-call performance counters into a flat dict for
    Langfuse generation spans. Splits the total latency into load / prefill /
    generate so a slow request can be diagnosed without adding a second tool.

    Ollama emits `*_duration` fields in nanoseconds and `*_count` fields in
    tokens. Tokens/sec is derived here so the UI shows it without expression
    support.
    """
    total = data.get("total_duration") or 0
    load = data.get("load_duration") or 0
    pe_dur = data.get("prompt_eval_duration") or 0
    ev_dur = data.get("eval_duration") or 0
    pe_count = data.get("prompt_eval_count") or 0
    ev_count = data.get("eval_count") or 0
    ns_to_ms = lambda x: round(x / 1e6, 1)
    md: dict = {
        # Human-readable ms breakdown — the primary diagnostic surface.
        # A large model_load_ms means keep_alive expired and the model
        # was reloaded from disk on this call.
        "model_load_ms": ns_to_ms(load),
        "prompt_eval_ms": ns_to_ms(pe_dur),
        "generation_ms": ns_to_ms(ev_dur),
        "total_ollama_ms": ns_to_ms(total),
        # Raw ns kept for anyone doing custom math in Langfuse.
        "total_duration_ns": total,
        "load_duration_ns": load,
        "prompt_eval_duration_ns": pe_dur,
        "eval_duration_ns": ev_dur,
        "prompt_eval_count": pe_count,
        "eval_count": ev_count,
        "done_reason": data.get("done_reason"),
    }
    # Human-readable derived rates. Guard against divide-by-zero — a cached
    # response with prompt_eval_duration=0 is legitimate.
    if pe_dur > 0:
        md["prompt_tokens_per_sec"] = round(pe_count * 1e9 / pe_dur, 1)
    if ev_dur > 0:
        md["eval_tokens_per_sec"] = round(ev_count * 1e9 / ev_dur, 1)
    return md


@observe(as_type="generation", name="ollama.generate", capture_input=False, capture_output=False)
async def _ollama_generate(prompt: str) -> str:
    """Plain /api/generate call. Returns the raw response string."""
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(
            OLLAMA_GENERATE_URL,
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
            },
        )
        r.raise_for_status()
        data = r.json()
    response = data.get("response", "")
    get_client().update_current_generation(
        model=MODEL,
        input=prompt,
        output=response,
        usage_details={
            "input": data.get("prompt_eval_count", 0),
            "output": data.get("eval_count", 0),
        },
        metadata=ollama_perf_metadata(data),
    )
    return response


@observe(as_type="generation", name="ollama.generate.json", capture_input=False, capture_output=False)
async def _ollama_json(prompt: str, num_predict: int | None = None) -> str:
    """One-shot /api/generate call with format=json. `num_predict`
    caps output tokens — Ollama otherwise generates up to the model's
    context window, which can leak seconds into low-value tail tokens
    on structured outputs.
    """
    payload: dict = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    if num_predict is not None and num_predict > 0:
        payload["options"] = {"num_predict": int(num_predict)}
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(OLLAMA_GENERATE_URL, json=payload)
        r.raise_for_status()
        data = r.json()
    text = data.get("response", "")
    get_client().update_current_generation(
        model=MODEL,
        input=prompt,
        output=text,
        usage_details={
            "input": data.get("prompt_eval_count", 0),
            "output": data.get("eval_count", 0),
        },
        metadata=ollama_perf_metadata(data),
    )
    return text


async def embed(text: str) -> np.ndarray:
    """Return an L2-normalized nomic-embed-text vector for `text`."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            OLLAMA_EMBEDDINGS_URL,
            json={
                "model": EMBED_MODEL,
                "prompt": text,
                "keep_alive": OLLAMA_KEEP_ALIVE,
            },
        )
        r.raise_for_status()
        vec = np.array(r.json()["embedding"], dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec
