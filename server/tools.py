"""Phase 1 tool registry.

Two tools. Both are read-only, no confirmation. Both are parameterless: they
operate on the `context` bundle the extension attached to the /ask request
(page text for article/PDF tabs, transcript for YouTube tabs). The registry
entries follow CLAUDE.md's convention (name, description, parameters,
side_effecting, requires_confirmation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx


OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen2.5:7b"

MAX_PAGE_CHARS = 12000
CHUNK_CHARS = 1500


@dataclass
class Context:
    url: str
    title: str
    page_text: str
    is_youtube: bool
    video_id: str | None
    transcript: list[dict] | None  # [{"start": float, "text": str}, ...]


ToolImpl = Callable[[dict, Context], Awaitable[str]]


# ---------- tool implementations ----------

async def _extract_page_text(_args: dict, ctx: Context) -> str:
    if not ctx.page_text.strip():
        return "No page text available for this tab."
    return ctx.page_text[:MAX_PAGE_CHARS]


async def _summarize_transcript(_args: dict, ctx: Context) -> str:
    if not ctx.is_youtube:
        return "This tab is not a YouTube video."
    if not ctx.transcript:
        return "No transcript available for this video (captions missing or disabled)."

    chunks = _chunk_transcript(ctx.transcript, CHUNK_CHARS)
    partials: list[str] = []
    for start_s, end_s, text in chunks:
        partials.append(
            await _ollama_generate(
                "Summarize this YouTube transcript chunk into 2-4 bullet points. "
                "Prefix each bullet with its approximate timestamp in [MM:SS]. "
                f"Chunk timestamp range: [{_fmt_ts(start_s)}]-[{_fmt_ts(end_s)}].\n\n"
                f"Transcript:\n{text}\n\nBullets:"
            )
        )

    if len(partials) == 1:
        return partials[0].strip()

    combined = "\n\n".join(partials)
    return (await _ollama_generate(
        "Combine these per-chunk bullet summaries of a YouTube video into a "
        "single ordered list of key points. Keep timestamps in [MM:SS] on each "
        "bullet. Deduplicate. Preserve chronological order.\n\n"
        f"{combined}\n\nFinal key points:"
    )).strip()


# ---------- registry ----------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "extract_page_text",
        "description": (
            "Return the readable text of the current browser tab. Use for "
            "questions about the content of an article, blog post, PDF, or "
            "any non-video web page the user is looking at."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
        "side_effecting": False,
        "requires_confirmation": False,
    },
    {
        "name": "summarize_transcript",
        "description": (
            "Return timestamped key points from the transcript of the "
            "currently open YouTube video. Use when the user asks to "
            "summarize, recap, or find something inside a YouTube video."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
        "side_effecting": False,
        "requires_confirmation": False,
    },
]

IMPLS: dict[str, ToolImpl] = {
    "extract_page_text": _extract_page_text,
    "summarize_transcript": _summarize_transcript,
}


def ollama_tool_specs() -> list[dict]:
    """Convert the registry into Ollama's /api/chat `tools` schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOLS
    ]


# ---------- helpers ----------

def _chunk_transcript(
    entries: list[dict], target_chars: int
) -> list[tuple[float, float, str]]:
    chunks: list[tuple[float, float, str]] = []
    buf: list[str] = []
    buf_start: float | None = None
    buf_end: float = 0.0
    size = 0
    for e in entries:
        text = e.get("text", "").strip()
        if not text:
            continue
        start = float(e.get("start", 0.0))
        if buf_start is None:
            buf_start = start
        buf.append(text)
        buf_end = start
        size += len(text) + 1
        if size >= target_chars:
            chunks.append((buf_start, buf_end, " ".join(buf)))
            buf, buf_start, size = [], None, 0
    if buf and buf_start is not None:
        chunks.append((buf_start, buf_end, " ".join(buf)))
    return chunks


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


async def _ollama_generate(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
        )
        r.raise_for_status()
        return r.json().get("response", "")
