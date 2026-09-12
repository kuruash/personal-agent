"""FastAPI application entry point.

Deliberately thin:
  - load .env
  - initialise Langfuse
  - construct the FastAPI app
  - warm up Ollama in the background so the first form-fill doesn't
    pay the model-load penalty
  - mount route modules

Business logic lives in `api/routes/*` and `workflows/form_answering/*`.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).parent / ".env")

from langfuse import Langfuse  # noqa: E402

Langfuse(
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    host=os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000"),
)

from .api.routes import ask as ask_route  # noqa: E402
from .api.routes import form_stream as form_stream_route  # noqa: E402
from .api.routes import health as health_route  # noqa: E402
from .llm.ollama_client import (  # noqa: E402
    MODEL,
    OLLAMA_GENERATE_URL,
    OLLAMA_KEEP_ALIVE,
)


async def _warmup_ollama() -> None:
    """One-off tiny /api/generate call at server startup to load
    qwen2.5:7b into memory and register keep_alive.

    Non-blocking: fired from a background task in the FastAPI lifespan,
    so uvicorn's readiness signal doesn't wait for it. The first real
    form-fill after startup avoids the ~3.6 s cold-load penalty.
    """
    try:
        import time as _t
        t0 = _t.perf_counter()
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                OLLAMA_GENERATE_URL,
                json={
                    "model": MODEL,
                    "prompt": "ok",
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                    "options": {"num_predict": 1},
                },
            )
            r.raise_for_status()
        print(
            f"[WARMUP] qwen2.5 loaded in {(_t.perf_counter()-t0)*1000:.0f} ms "
            f"(keep_alive={OLLAMA_KEEP_ALIVE})",
            flush=True,
        )
    except Exception as e:
        print(f"[WARMUP] skipped: {e}", flush=True)


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(health_route.router)
app.include_router(ask_route.router)
app.include_router(form_stream_route.router)


@app.on_event("startup")
async def _on_startup() -> None:
    # Fire and forget — don't block readiness on model load.
    import asyncio as _asyncio
    _asyncio.create_task(_warmup_ollama())
