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
from langfuse import get_client, observe


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
    email_thread: dict | None = None
    # Shape: {"subject": str, "participants": [{"name","email"}...],
    #         "messages": [{"from","email","timestamp","body_text"}...]}


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


async def _read_email_thread(_args: dict, ctx: Context) -> str:
    t = ctx.email_thread
    if not t or not t.get("messages"):
        return "No Gmail thread is open in the active tab."
    lines = [f"Subject: {t.get('subject', '(no subject)')}", ""]
    for i, m in enumerate(t.get("messages", []), start=1):
        who = f"{m.get('from', '')} <{m.get('email', '')}>".strip()
        when = m.get("timestamp", "")
        lines.append(f"--- Message {i} — {who} ({when}) ---")
        lines.append((m.get("body_text") or "").strip())
        lines.append("")
    return "\n".join(lines).strip()


async def _draft_email_reply(args: dict, ctx: Context) -> str:
    intent = (args.get("intent") or "").strip()
    if not intent:
        return "Cannot draft without an 'intent' describing what the user wants to say."
    if not ctx.email_thread or not ctx.email_thread.get("messages"):
        return "No Gmail thread is open in the active tab."

    thread_text = await _read_email_thread({}, ctx)
    last = ctx.email_thread["messages"][-1]
    recipient = last.get("from", "the sender")

    draft = (await _ollama_generate(
        "You are drafting a reply email on behalf of the user. Write only the "
        "reply body — no subject line, no greeting boilerplate beyond a short "
        f"salutation to {recipient}, no signature. Match the register of the "
        "prior messages (professional but conversational). Be concise. Do not "
        "invent facts not present in the thread or the user's intent.\n\n"
        f"--- THREAD ---\n{thread_text}\n--- END THREAD ---\n\n"
        f"User's intent for the reply: {intent}\n\nReply body:"
    )).strip()

    polished = (await _ollama_generate(
        "Improve the grammar and tone of the draft below. Do NOT change the "
        "substance, add new claims, or shift the tone. Keep the author's "
        "voice. Return only the polished text, nothing else.\n\n"
        f"--- DRAFT ---\n{draft}\n--- END DRAFT ---\n\nPolished:"
    )).strip()

    return polished or draft


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
    {
        "name": "read_email_thread",
        "description": (
            "Return the subject, participants, and messages of the Gmail "
            "thread currently open in the active tab. Use when the user "
            "asks about, references, or wants to summarize their current "
            "email conversation."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
        "side_effecting": False,
        "requires_confirmation": False,
    },
    {
        "name": "draft_email_reply",
        "description": (
            "Draft a reply to the Gmail thread currently open in the active "
            "tab, given a short description of what the user wants to say. "
            "Produces a draft only — never sends or inserts. Use when the "
            "user asks to reply, respond, or write back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": (
                        "What the user wants to say, in their own words. "
                        "May be terse — the tool will expand it into a "
                        "well-formed reply."
                    ),
                }
            },
            "required": ["intent"],
        },
        "side_effecting": False,
        "requires_confirmation": True,
    },
]

IMPLS: dict[str, ToolImpl] = {
    "extract_page_text": _extract_page_text,
    "summarize_transcript": _summarize_transcript,
    "read_email_thread": _read_email_thread,
    "draft_email_reply": _draft_email_reply,
}


def tool_by_name(name: str) -> dict | None:
    for t in TOOLS:
        if t["name"] == name:
            return t
    return None


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


@observe(as_type="generation", name="ollama.generate", capture_input=False, capture_output=False)
async def _ollama_generate(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
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
        metadata={
            "total_duration_ns": data.get("total_duration"),
            "eval_duration_ns": data.get("eval_duration"),
        },
    )
    return response
