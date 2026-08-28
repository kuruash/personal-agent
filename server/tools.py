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

import json
import os
import re

import httpx
from langfuse import get_client, observe

from .concepts import (
    build_prompt,
    match_obvious,
    obvious_value,
    parse_response,
)
from .profile import load_profile


OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen2.5:7b"

# Keep the model resident between requests so the second /ask in a session
# doesn't pay the ~5-15s load penalty again. Configurable via env so it can
# be dialed down on machines with less RAM. Ollama accepts "-1" for indefinite,
# "0" for evict-immediately, or a duration string like "30m" / "1h".
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")


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
    md: dict = {
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
    form_fields: list[dict] | None = None
    # Shape: [{selector, tag, type, name, id, autocomplete, placeholder,
    #          aria_label, label, required, current_value, options?}, ...]


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


# ---------- form-field mapping ----------

def _value_fits_options(value, options: list[dict] | None) -> bool:
    """Return True iff `value` maps onto at least one option in the list.

    Match is case-insensitive and bidirectional-substring: option value OR
    option display text must equal, contain, or be contained in the profile
    value. Booleans are coerced to "yes"/"no" first so a stored
    `"requires_sponsorship": true` maps onto a Yes/No radio group without
    forcing the user to write "yes" as a string in their profile.
    """
    if not options or value is None:
        return False
    if isinstance(value, bool):
        value = "yes" if value else "no"
    v = str(value).strip().lower()
    if not v:
        return False
    for opt in options:
        for candidate in (opt.get("value"), opt.get("text")):
            if not candidate:
                continue
            c = str(candidate).strip().lower()
            if not c:
                continue
            if v == c or v in c or c in v:
                return True
    return False


def _reformat_phone_for_field(value: str, field: dict) -> str | None:
    """If the field carries an explicit phone-format hint, reformat the raw
    digits of `value` to match. Returns None when no hint is detected (caller
    keeps the profile value as-is).

    Hint sources scanned (concatenated, lowercased):
      - label / aria_label / placeholder text — carries hints like
        "Format: 703-993-2999" or "e.g. (703) 993-2999".
      - the input's `pattern` attribute if present.

    Recognized shapes:
      ##########              → 10 raw digits
      ###-###-####            → dashes
      (###) ###-####          → parens + space + dash
      ###.###.####            → dots
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) != 10:
        return None
    hint = " ".join([
        str(field.get("label") or ""),
        str(field.get("aria_label") or ""),
        str(field.get("placeholder") or ""),
        str(field.get("pattern") or ""),
    ]).lower()
    if not hint.strip():
        return None
    # Order matters: check most-specific patterns first.
    if re.search(r"\(\s*\d{3}\s*\)\s*\d{3}[-\s]\d{4}", hint) or "(###) ###-####" in hint:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    if re.search(r"\d{3}\.\d{3}\.\d{4}", hint) or "###.###.####" in hint:
        return f"{digits[0:3]}.{digits[3:6]}.{digits[6:10]}"
    if re.search(r"\d{3}-\d{3}-\d{4}", hint) or "###-###-####" in hint:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    if "##########" in hint or re.search(r"10[- ]digit", hint):
        return digits
    return None


async def _detect_form_fields(_args: dict, ctx: Context) -> str:
    """Two-path resolution.

    1. OBVIOUS: standard fields (name, email, phone, address, links)
       resolved directly from the profile by autocomplete token or
       full-label alias.
    2. Everything else: ONE Qwen call with the profile as context. Qwen
       returns the actual answer string per field.

    The panel gets a flat plan of {label, value, state, source}; the user
    reviews and clicks Fill. Option-fit guard runs on both paths so a
    right-kind-of-answer-wrong-value still stops before autofill.
    """
    print(
        f"[FORM DEBUG] detect_form_fields tool entered — "
        f"ctx.form_fields length={len(ctx.form_fields) if ctx.form_fields else 0}",
        flush=True,
    )
    if not ctx.form_fields:
        return "No form fields detected on the active tab."
    profile = load_profile()
    fields = ctx.form_fields or []
    plan: list[dict] = [None] * len(fields)  # type: ignore[list-item]

    unresolved: list[dict] = []
    n_obvious = 0
    for i, f in enumerate(fields):
        oid = match_obvious(f.get("label") or "", f.get("autocomplete") or "")
        if oid is None:
            unresolved.append({
                "index": i,
                "label": f.get("label") or f.get("aria_label") or f.get("placeholder") or f.get("name") or "",
                "options": [(o.get("text") or o.get("value") or "") for o in (f.get("options") or [])] or None,
            })
            continue

        raw = obvious_value(oid, profile)
        if raw is None:
            plan[i] = _plan_entry(f, "", f"obvious:{oid} — profile value is null", "unknown")
            n_obvious += 1
            continue

        v = "yes" if raw is True else "no" if raw is False else str(raw)
        if oid == "phone":
            fmt = _reformat_phone_for_field(v, f)
            if fmt:
                v = fmt
        options = f.get("options")
        if options and not _value_fits_options(v, options):
            plan[i] = _plan_entry(
                f, "", f"obvious:{oid} but value {v!r} did not fit options", "unknown"
            )
        else:
            plan[i] = _plan_entry(f, v, f"obvious:{oid}", "ready")
        n_obvious += 1

    if unresolved:
        lf = get_client()
        with lf.start_as_current_observation(
            name="form.qwen_autofill",
            as_type="span",
            input={"unresolved_field_count": len(unresolved)},
            metadata={"total_fields": len(fields), "obvious": n_obvious},
        ) as parse_span:
            prompt = build_prompt(unresolved, profile)
            model_text = await _ollama_json(prompt)
            answers = parse_response(model_text, [u["index"] for u in unresolved])
            for u in unresolved:
                idx = u["index"]
                v = answers.get(idx, "")
                f = fields[idx]
                options = f.get("options")
                if not v:
                    plan[idx] = _plan_entry(f, "", "qwen: no answer from profile", "unknown")
                elif options and not _value_fits_options(v, options):
                    plan[idx] = _plan_entry(
                        f, "", f"qwen answered {v!r} but did not fit options", "unknown"
                    )
                else:
                    plan[idx] = _plan_entry(f, v, "qwen", "ready")
            parse_span.update(
                output={"answers": {u["index"]: answers.get(u["index"], "") for u in unresolved}},
            )

    return json.dumps({
        "fields": plan,
        "counts": {
            "total": len(fields),
            "obvious": n_obvious,
            "qwen": len(unresolved),
        },
    }, indent=2)


def _plan_entry(field: dict, value: str, source: str, state: str) -> dict:
    return {
        "selector": field.get("selector"),
        "label": field.get("label") or field.get("aria_label") or field.get("placeholder") or field.get("name") or field.get("id"),
        "type": field.get("type") or field.get("tag"),
        "required": field.get("required", False),
        "current_value": field.get("current_value") or "",
        "options": field.get("options"),
        "value": value,
        "state": state,   # "ready" | "unknown"
        "source": source,
    }


@observe(as_type="generation", name="ollama.generate.json", capture_input=False, capture_output=False)
async def _ollama_json(prompt: str) -> str:
    """One-shot /api/generate call with format=json for the batched
    concept-mapping stage. Kept separate from _ollama_generate so the
    generation-span metadata reflects this specific call site."""
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "keep_alive": OLLAMA_KEEP_ALIVE,
            },
        )
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
        "name": "detect_form_fields",
        "description": (
            "Return the list of form fields on the current tab, each with a "
            "proposed value from the user's profile and a confidence score "
            "(high/medium/low/none). Read-only — does NOT fill anything. Use "
            "when the user asks to fill out, autofill, or apply on a page "
            "with a form."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
        "side_effecting": False,
        "requires_confirmation": False,
    },
    {
        "name": "fill_form_field",
        "description": (
            "Propose writing a single value into a single form field, "
            "identified by CSS selector. Fills one field only — never a "
            "whole form. The write does not happen until the user "
            "explicitly confirms in the side panel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the target field (from detect_form_fields output).",
                },
                "value": {
                    "type": "string",
                    "description": "Value to write into the field.",
                },
            },
            "required": ["selector", "value"],
        },
        "side_effecting": True,
        "requires_confirmation": True,
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
    "detect_form_fields": _detect_form_fields,
    "fill_form_field": _fill_form_field,
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
