"""Phase 1: /ask runs a tool-calling loop against Ollama.

Flow: user question + tool specs go to Ollama's /api/chat. If the model returns
tool_calls, we validate the name against the registry, run the implementation
against the request's context bundle, feed the result back as a `tool` message,
and loop. On malformed tool calls (unknown name), we retry once with the
validation error injected. Stops after MAX_ITERS.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).parent / ".env")

from langfuse import Langfuse, get_client, observe  # noqa: E402

Langfuse(
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    host=os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000"),
)

from .memory import (  # noqa: E402
    format_recall_for_prompt,
    log_interaction,
    recall,
)
from .tools import (  # noqa: E402
    IMPLS,
    MODEL,
    OLLAMA_KEEP_ALIVE,
    TOOLS,
    Context,
    ollama_perf_metadata,
    ollama_tool_specs,
    tool_by_name,
)

OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
MAX_ITERS = 4

SYSTEM_PROMPT = (
    "You are a local browser-side assistant. The user is looking at a web page "
    "or a YouTube video. You have tools that read the current tab, and you "
    "may also receive a 'Relevant past interactions' block containing your "
    "memory of prior conversations.\n\n"
    "Decision rule:\n"
    "1. If the user's question refers to something they saw or asked earlier "
    "(phrases like 'that page', 'the video', 'earlier', 'yesterday', 'last "
    "week') AND the memory block already contains the answer, answer directly "
    "from memory. Do NOT call a tool for the current tab in that case — the "
    "current tab may be unrelated.\n"
    "2. Otherwise, if the question is about the current tab's content, call "
    "the appropriate tool.\n"
    "3. Never invent content. If neither memory nor a tool can answer, say so.\n"
    "4. When the user asks to fill out, autofill, or apply on a page with a "
    "form, call detect_form_fields ONCE and then stop — the panel renders "
    "its output as a field-by-field preview and the user approves each field "
    "themselves. Do not call fill_form_field per field in a loop; only call "
    "fill_form_field if the user has singled out one specific field to fill."
)

# ---------- fast intent router ----------
#
# For high-confidence obvious commands we skip Ollama entirely and dispatch
# straight to the target tool. Each entry is:
#   patterns  — compiled regexes matched against the trimmed lowercased
#               question. Kept narrow: anything that isn't unambiguously
#               "run this tool" should fall through to the ReAct path.
#   tool      — name of the tool in IMPLS to invoke directly.
#   applies   — cheap predicate on the request context; if False, this intent
#               can't apply this turn (e.g. no form on the page).
#   answer_fn — builds the static answer string from the tool result. Never
#               calls the model; the panel does the real UX rendering.
#
# Add new intents by appending to FAST_INTENTS. No matcher-code changes.
_FILL_FORM_PATTERNS = [
    re.compile(r"^\s*(please\s+)?fill\s+(this|the)\s+(form|application|page)\s*[.!?]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*(please\s+)?fill\s+(it|this|the\s+form|the\s+application)\s+out\s*[.!?]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*(please\s+)?autofill(\s+(this|the))?(\s+(form|application|page))?\s*[.!?]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*(please\s+)?auto[- ]?fill\s+(this|the)\s+(form|application|page)\s*[.!?]*\s*$", re.IGNORECASE),
]


def _fill_form_answer(fields: list[dict]) -> str:
    n = len(fields)
    if n == 0:
        return "No fillable form fields detected on the current tab."
    return (
        f"Detected {n} form field(s). Review each below before anything is written."
    )


FAST_INTENTS: list[dict[str, Any]] = [
    {
        "name": "form_fill",
        "patterns": _FILL_FORM_PATTERNS,
        "tool": "detect_form_fields",
        "applies": lambda ctx: bool(ctx.form_fields),
        "answer_fn": _fill_form_answer,
    },
]


def _match_fast_intent(question: str, ctx: Context) -> dict[str, Any] | None:
    q = (question or "").strip()
    if not q:
        return None
    for intent in FAST_INTENTS:
        if not intent["applies"](ctx):
            continue
        for pat in intent["patterns"]:
            if pat.match(q):
                return intent
    return None


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


class ContextPayload(BaseModel):
    url: str = ""
    title: str = ""
    page_text: str = ""
    is_youtube: bool = False
    video_id: str | None = None
    transcript: list[dict] | None = None
    email_thread: dict | None = None
    form_fields: list[dict] | None = None


class AskRequest(BaseModel):
    question: str
    context: ContextPayload = Field(default_factory=ContextPayload)


@app.post("/ask")
@observe(name="ask", capture_input=False, capture_output=False)
async def ask(req: AskRequest) -> dict[str, Any]:
    lf = get_client()
    lf.update_current_span(
        input={"question": req.question, "url": req.context.url, "title": req.context.title},
        metadata={
            "is_youtube": req.context.is_youtube,
            "has_email_thread": req.context.email_thread is not None,
        },
    )
    ctx = Context(**req.context.model_dump())

    trace: list[dict[str, Any]] = []
    answer = "Reached tool-call limit without a final answer."
    requires_confirmation = False
    draft: dict[str, Any] | None = None

    # ---- fast-intent router ----
    # For unambiguous commands we skip the ReAct loop entirely — no chat
    # call, no recall (fast intents are deterministic; there's no ambiguity
    # for memory to disambiguate). The tool executes directly and the same
    # response contract is built in Python.
    intent = _match_fast_intent(req.question, ctx)
    if intent is not None:
        lf.update_current_span(metadata={"fast_intent": intent["name"]})
        name = intent["tool"]
        spec = tool_by_name(name) or {}
        with lf.start_as_current_observation(
            name=f"tool:{name}",
            as_type="tool",
            input={},
            metadata={
                "tool_name": name,
                "side_effecting": spec.get("side_effecting", False),
                "requires_confirmation": spec.get("requires_confirmation", False),
                "via": "fast_intent",
            },
        ) as tool_span:
            result = await IMPLS[name]({}, ctx)
            tool_span.update(output=result)
        trace.append({"tool": name, "chars": len(result), "fast_intent": intent["name"]})
        parsed = _safe_json(result)
        fields = parsed.get("fields") if isinstance(parsed, dict) else None
        if fields:
            draft = {"type": "form_fill", "fields": fields}
        answer = intent["answer_fn"](fields or [])

        tools_used = [name]
        await log_interaction(ctx.url, ctx.title, req.question, answer, tools_used)
        lf.update_current_span(
            output={"answer": answer, "requires_confirmation": False},
            metadata={"tools_used": tools_used, "trace": trace, "path": "fast"},
        )
        return {
            "answer": answer,
            "trace": trace,
            "requires_confirmation": False,
            "draft": draft,
        }

    # ---- normal ReAct path ----
    # Retrieve on the question alone. Blending in the current tab title
    # biases recall toward the current tab and drowns out cross-session
    # references like "that pricing page I saw earlier".
    matches = await recall(req.question, current_url=ctx.url)
    memory_block = format_recall_for_prompt(matches)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    if memory_block:
        messages.append({"role": "system", "content": memory_block})
    messages.append({"role": "user", "content": _user_message(req.question, ctx)})

    if matches:
        trace.append({
            "recall": [
                {"sim": m["sim"], "title": m["title"], "url": m["url"]}
                for m in matches
            ]
        })
    tool_error_retried = False
    async with httpx.AsyncClient(timeout=180.0) as client:
        for _ in range(MAX_ITERS):
            resp = await _chat(client, messages)
            msg = resp.get("message", {}) or {}
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                answer = (msg.get("content") or "").strip()
                break

            messages.append(msg)
            call = tool_calls[0]
            fn = (call.get("function") or {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", {}) or {}
            args = raw_args if isinstance(raw_args, dict) else _safe_json(raw_args)

            if name not in IMPLS:
                if tool_error_retried:
                    answer = f"Model kept calling unknown tool '{name}'."
                    break
                tool_error_retried = True
                messages.append({
                    "role": "tool",
                    "content": (
                        f"Error: unknown tool '{name}'. Valid tools: "
                        f"{', '.join(t['name'] for t in TOOLS)}. "
                        "Call one of these or answer directly."
                    ),
                })
                trace.append({"tool": name, "error": "unknown_tool"})
                continue

            spec = tool_by_name(name) or {}
            with lf.start_as_current_observation(
                name=f"tool:{name}",
                as_type="tool",
                input=args,
                metadata={
                    "tool_name": name,
                    "side_effecting": spec.get("side_effecting", False),
                    "requires_confirmation": spec.get("requires_confirmation", False),
                },
            ) as tool_span:
                try:
                    result = await IMPLS[name](args, ctx)
                except Exception as e:  # pragma: no cover
                    result = f"Tool '{name}' failed: {e}"
                tool_span.update(output=result)

            trace.append({"tool": name, "chars": len(result)})

            if name == "detect_form_fields":
                # TERMINAL: the plan is what the panel renders. Sending the
                # JSON back through a second /api/chat call just to have Qwen
                # produce a natural-language wrapper is pure latency — the
                # panel doesn't use that wrapper anyway. Break here and build
                # the {answer, draft} response directly.
                #
                # Per-field writes still go through fill_form_field's
                # requires_confirmation gate via the panel; this only skips
                # a redundant model round-trip, not any safety check.
                parsed = _safe_json(result)
                fields = parsed.get("fields") if isinstance(parsed, dict) else None
                if fields and draft is None:
                    draft = {"type": "form_fill", "fields": fields}
                answer = _fill_form_answer(fields or [])
                break
            if spec.get("requires_confirmation"):
                # Terminal state: the tool's output IS the artifact awaiting
                # explicit human action. Do not let the model keep generating
                # or call other tools after producing a draft.
                answer = result
                requires_confirmation = True
                if name == "fill_form_field":
                    parsed = _safe_json(result)
                    single = {
                        "selector": parsed.get("selector"),
                        "value": parsed.get("value"),
                        "label": parsed.get("selector"),
                        "confidence": "high",
                        "source": "explicit fill_form_field call",
                    }
                    if isinstance(draft, dict) and draft.get("type") == "form_fill":
                        # detect_form_fields already produced the full plan;
                        # keep it and just note the model's per-field pick
                        # rather than replacing the whole preview.
                        pass
                    else:
                        draft = {"type": "form_fill", "fields": [single]}
                else:
                    draft = {
                        "type": "email",
                        "body": result,
                        "thread_subject": (ctx.email_thread or {}).get("subject", ""),
                    }
                break

            messages.append({"role": "tool", "content": result})

    tools_used = [t["tool"] for t in trace if "tool" in t]
    await log_interaction(ctx.url, ctx.title, req.question, answer, tools_used)
    lf.update_current_span(
        output={"answer": answer, "requires_confirmation": requires_confirmation},
        metadata={"tools_used": tools_used, "trace": trace},
    )
    return {
        "answer": answer,
        "trace": trace,
        "requires_confirmation": requires_confirmation,
        "draft": draft,
    }


def _user_message(question: str, ctx: Context) -> str:
    hints = []
    if ctx.title:
        hints.append(f"Tab title: {ctx.title}")
    if ctx.url:
        hints.append(f"URL: {ctx.url}")
    hints.append(f"Tab is a YouTube video: {ctx.is_youtube}")
    if ctx.is_youtube:
        hints.append(
            "Transcript available: "
            + ("yes" if ctx.transcript else "no")
        )
    header = "\n".join(hints)
    return f"{header}\n\nUser question: {question}"


@observe(as_type="generation", name="ollama.chat", capture_input=False, capture_output=False)
async def _chat(client: httpx.AsyncClient, messages: list[dict]) -> dict:
    r = await client.post(
        OLLAMA_CHAT_URL,
        json={
            "model": MODEL,
            "messages": messages,
            "tools": ollama_tool_specs(),
            "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
        },
    )
    r.raise_for_status()
    data = r.json()
    msg = data.get("message", {}) or {}
    get_client().update_current_generation(
        model=MODEL,
        input=messages,
        output=msg,
        usage_details={
            "input": data.get("prompt_eval_count", 0),
            "output": data.get("eval_count", 0),
        },
        metadata=ollama_perf_metadata(data),
    )
    return data


def _safe_json(s: Any) -> dict:
    try:
        return json.loads(s) if isinstance(s, str) else {}
    except Exception:
        return {}
