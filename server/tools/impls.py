"""Tool implementations.

Every tool takes `(args: dict, ctx: Context) -> Awaitable[str]` and
returns a stringified result the tool-calling loop feeds back to the
model. Side-effecting tools return a proposal string; the actual DOM
write happens in the extension after the user confirms.

Split from the registry so schema definitions and implementations can
be scanned independently.
"""

from __future__ import annotations

import json

from ..llm.ollama_client import CHUNK_CHARS, MAX_PAGE_CHARS, _ollama_generate, _ollama_json
from .context import Context


# ---------- read-only tools ----------

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


async def _detect_form_fields(_args: dict, ctx: Context) -> str:
    """Route each detected field through the form-answering pipeline
    (DIRECT + hybrid-retrieval + batched-Qwen)."""
    print(
        f"[FORM DEBUG] detect_form_fields tool entered — "
        f"ctx.form_fields length={len(ctx.form_fields) if ctx.form_fields else 0}",
        flush=True,
    )
    if not ctx.form_fields:
        return "No form fields detected on the active tab."
    # Local import — form_answering pulls chromadb via retrieval.hybrid,
    # which we don't want to force-load on Gmail / YouTube flows.
    from ..workflows.form_answering import answer_form
    result = await answer_form(ctx.form_fields)
    return json.dumps(result, indent=2)


async def _fill_form_field(args: dict, ctx: Context) -> str:
    selector = (args.get("selector") or "").strip()
    value = args.get("value")
    if not selector or value is None:
        return "fill_form_field requires 'selector' and 'value'."
    # This tool is requires_confirmation: the /ask loop stops here and hands
    # the panel a single-field proposal. The actual DOM write happens when
    # the user clicks Fill in the panel, which routes through the content
    # script's fillField() — no server-driven writes.
    return json.dumps({"selector": selector, "value": str(value)}, indent=2)


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
