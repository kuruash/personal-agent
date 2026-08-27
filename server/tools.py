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

import asyncio
import json

import httpx
from langfuse import get_client, observe

from .memory import embed
from .profile import (
    get_profile_aliases,
    get_profile_descriptions,
    get_profile_embeddings,
    load_profile,
)


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

# Similarity bands over cosine(profile-key description, canonical label).
# Calibrated empirically against extension/fixtures/form.html — see the
# fixture harness for the raw score distribution.
SIM_HIGH = 0.72
SIM_MEDIUM = 0.62
SIM_LOW = 0.52

# Ambiguity gate: if top match beats runner-up by less than this, the field
# is AMBIGUOUS and both candidates are surfaced for user choice instead of
# silently picking one. Same distributional caveats as SIM_HIGH — tune empirically.
SIM_AMBIG_GAP = 0.06

# Field-type descriptions that formdetect.js could NOT strip because they
# came in via label/aria in a way the strategy chain accepted. Defense in
# depth — the same set (case-insensitive) is stripped when normalizing the
# canonical label here too.
_GENERIC_LABEL_PHRASES = [
    "single line text",
    "enter your answer",
    "your answer",
    "type your answer",
]


def _normalize_label(text: str) -> str:
    """Lowercase, collapse whitespace, and strip generic type-hint phrases.

    Returns "" if nothing usable remains. Callers should treat empty as
    NO-MATCH.
    """
    if not text:
        return ""
    s = text.lower().strip()
    for phrase in _GENERIC_LABEL_PHRASES:
        s = s.replace(phrase, " ")
    # Collapse punctuation and whitespace runs into single spaces so
    # "First name*" and "First name ?" both normalize to "first name".
    out_chars = []
    for ch in s:
        out_chars.append(ch if (ch.isalnum() or ch == " ") else " ")
    return " ".join("".join(out_chars).split())


def _canonical_label(field: dict) -> str:
    """Return the canonical label text used for matching.

    Uses only the label produced by formdetect.js's winning strategy —
    NOT a concatenation of label + aria_label + placeholder + name + id.
    The winning strategy already ran a generic-label reject, so anything
    label_source != "none" is question text, not type hint. If the fallback
    chain also failed (label_source == "none"), there is no signal and this
    returns "".
    """
    src = (field.get("label_source") or "").strip().lower()
    if not src or src == "none":
        return ""
    return _normalize_label(field.get("label") or "")


def _band(sim: float) -> str:
    if sim >= SIM_HIGH: return "high"
    if sim >= SIM_MEDIUM: return "medium"
    if sim >= SIM_LOW: return "low"
    return "none"


def _alias_hit(label: str, aliases_by_key: dict[str, list[str]]) -> tuple[str, str] | None:
    """Try exact alias equality first, then whole-word alias-in-label.

    Returns (key, matched_alias) if exactly one profile key hits at the
    highest available tier, else None. When multiple keys tie we deliberately
    escalate to the embedding stage rather than picking arbitrarily — that
    lets the ambiguity gate weigh in.
    """
    if not label:
        return None
    exact: list[tuple[str, str]] = []
    for key, aliases in aliases_by_key.items():
        for a in aliases:
            if a == label:
                exact.append((key, a))
                break
    # Dedupe by key — multiple aliases exact-matching for the same key still
    # counts as one hit for that key.
    keys_exact = {k for k, _ in exact}
    if len(keys_exact) == 1:
        return exact[0]
    if len(keys_exact) > 1:
        return None  # true tie: hand off to embedding stage

    tokens = label.split()
    token_set = set(tokens)
    label_padded = f" {label} "
    substring: list[tuple[str, str]] = []
    for key, aliases in aliases_by_key.items():
        best_alias_for_key: str | None = None
        for a in aliases:
            if " " in a:
                # multi-word alias: phrase must appear contiguously
                if f" {a} " in label_padded:
                    if best_alias_for_key is None or len(a) > len(best_alias_for_key):
                        best_alias_for_key = a
            else:
                if a in token_set:
                    if best_alias_for_key is None or len(a) > len(best_alias_for_key):
                        best_alias_for_key = a
        if best_alias_for_key is not None:
            substring.append((key, best_alias_for_key))

    if len(substring) == 1:
        return substring[0]
    # Multiple keys hit at substring level — could still be unambiguous if
    # one match is strictly longer (more specific). Take the strictly-longest
    # alias if there's a clear winner; else escalate.
    if len(substring) > 1:
        substring.sort(key=lambda kv: len(kv[1]), reverse=True)
        if len(substring[0][1]) > len(substring[1][1]):
            return substring[0]
    return None


def _value_fits_options(value: str | None, options: list[dict] | None) -> bool:
    """Return True iff `value` maps onto at least one option in the list.

    Match is case-insensitive and bidirectional-substring: option value OR
    option display text must equal, contain, or be contained in the profile
    value. This is intentionally forgiving so "YES"/"Yes"/"yes" all fit a
    "Yes/No" pair, but strict enough that "George Mason University" won't
    silently pass a college-picker with options like "College of X".
    """
    if not options or not value:
        return False
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


def _none_result(source: str) -> dict:
    return {"profile_key": None, "value": None, "confidence": "none",
            "source": source, "similarity": 0.0}


def _apply_option_guard(result: dict, options: list[dict] | None) -> dict:
    """If the field has a closed option set, require the picked profile value
    to fit one of the options. If nothing fits, downgrade to NONE — the
    question matched a key but the stored answer doesn't apply here.

    Applied AFTER alias/embedding match; upstream unchanged for text/textarea.
    """
    if not options:
        return result
    conf = result.get("confidence")
    if conf in (None, "none"):
        return result

    if conf == "ambiguous":
        cands = result.get("candidates", []) or []
        fit = [c for c in cands if _value_fits_options(c.get("value"), options)]
        if not fit:
            return _none_result(
                source=(
                    f"question matched profile keys but no candidate value "
                    f"fits the field's options "
                    f"({', '.join(c.get('profile_key','?') for c in cands)})"
                ),
            )
        if len(fit) == 1:
            c = fit[0]
            return {
                "profile_key": c["profile_key"],
                "value": c["value"],
                "confidence": "high",
                "source": (
                    f"ambiguous disambiguated by option-fit → {c['profile_key']}"
                    f" (value '{c['value']}' fits an option)"
                ),
                "similarity": c.get("similarity"),
            }
        # multiple candidates still fit — remain ambiguous but only over fits
        result["candidates"] = fit
        return result

    # Single-match path (high/medium/low)
    v = result.get("value")
    if not _value_fits_options(v, options):
        return _none_result(
            source=(
                f"question matched profile key '{result.get('profile_key')}' "
                f"but its value {v!r} is not among the field's options"
            ),
        )
    return result


def _hit_result(key: str, profile: dict, confidence: str, source: str,
                similarity: float | None = None,
                candidates: list[dict] | None = None) -> dict:
    value = profile.get(key)
    result: dict = {
        "profile_key": key,
        "value": value,
        "confidence": confidence,
        "source": source,
    }
    if similarity is not None:
        result["similarity"] = round(similarity, 3)
    if candidates is not None:
        result["candidates"] = candidates
    if value is None:
        # Key known but not populated — still surface it (panel will render
        # an empty input) but downgrade to low so the panel doesn't
        # auto-fill nothing.
        result["confidence"] = "low"
        result["source"] = f"{source} — profile key '{key}' is unset"
    return result


async def _match_field(
    field: dict,
    prof_vecs: dict,
    aliases_by_key: dict[str, list[str]],
    profile: dict,
) -> dict:
    """Three-stage deterministic matcher.

    Stage 1 — canonical-label extraction:
        pull the winning label from formdetect.js's strategy chain, normalize.
        empty → NO-MATCH.

    Stage 2 — alias match:
        exact or whole-word alias hit → HIGH, no embedding needed.

    Stage 3 — embedding match with ambiguity gate:
        cosine(canonical, description) for every key; if top - runner-up
        gap < SIM_AMBIG_GAP, return AMBIGUOUS with both candidates; else
        return the top match bucketed by SIM_HIGH/MEDIUM/LOW.
    """
    label = _canonical_label(field)
    options = field.get("options")
    if not label:
        return _none_result("field has no usable label after normalization")

    # Stage 2: alias short-circuit.
    hit = _alias_hit(label, aliases_by_key)
    if hit is not None:
        key, alias = hit
        result = _hit_result(
            key, profile, "high",
            f"alias match: label '{label}' hit '{alias}' → {key}",
        )
        return _apply_option_guard(result, options)

    # Stage 3: embedding.
    fvec = await embed(label)
    ranked: list[tuple[float, str, str]] = []
    for key, (desc, kvec) in prof_vecs.items():
        sim = float(fvec @ kvec)
        ranked.append((sim, key, desc))
    ranked.sort(key=lambda x: x[0], reverse=True)
    top_sim, top_key, top_desc = ranked[0]
    runner_sim, runner_key, runner_desc = ranked[1] if len(ranked) > 1 else (0.0, "", "")

    if _band(top_sim) == "none":
        return _none_result(
            f"no profile key above threshold "
            f"(top embedding sim={top_sim:.2f} to '{top_desc}' [{top_key}])"
        )

    gap = top_sim - runner_sim
    if runner_sim > 0 and gap < SIM_AMBIG_GAP:
        # Ambiguous — surface both candidates. Confidence is a distinct band
        # so the panel can render its own picker instead of the default.
        ambig = {
            "profile_key": top_key,
            "value": profile.get(top_key),
            "confidence": "ambiguous",
            "source": (
                f"ambiguous: top={top_sim:.2f} [{top_key}] vs "
                f"runner={runner_sim:.2f} [{runner_key}] (gap {gap:.2f} < {SIM_AMBIG_GAP})"
            ),
            "similarity": round(top_sim, 3),
            "candidates": [
                {"profile_key": top_key, "description": top_desc,
                 "value": profile.get(top_key), "similarity": round(top_sim, 3)},
                {"profile_key": runner_key, "description": runner_desc,
                 "value": profile.get(runner_key), "similarity": round(runner_sim, 3)},
            ],
        }
        return _apply_option_guard(ambig, options)

    source = f"embedding sim={top_sim:.2f} to '{top_desc}' [{top_key}]"
    result = _hit_result(top_key, profile, _band(top_sim), source, similarity=top_sim)
    return _apply_option_guard(result, options)


async def _detect_form_fields(_args: dict, ctx: Context) -> str:
    if not ctx.form_fields:
        return "No form fields detected on the active tab."

    prof_vecs = await get_profile_embeddings()
    aliases_by_key = get_profile_aliases()
    profile = load_profile()

    # Fan out field embeddings — Ollama handles concurrent /api/embeddings
    # requests fine, and this cuts a 15-field detect call from ~O(N*RTT)
    # sequential to a single burst. Fields resolved by alias match skip
    # embedding entirely, so this is usually much less than N calls.
    mappings = await asyncio.gather(*[
        _match_field(f, prof_vecs, aliases_by_key, profile) for f in ctx.form_fields
    ])

    plan = []
    for f, mapping in zip(ctx.form_fields, mappings):
        plan.append({
            "selector": f.get("selector"),
            "label": f.get("label") or f.get("aria_label") or f.get("placeholder") or f.get("name") or f.get("id"),
            "type": f.get("type") or f.get("tag"),
            "required": f.get("required", False),
            "current_value": f.get("current_value") or "",
            "options": f.get("options"),
            **mapping,
        })
    # Return JSON as a string so the model sees structured data. The panel
    # parses this out of the tool trace when rendering the field-by-field
    # preview.
    return json.dumps({"fields": plan}, indent=2)


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
