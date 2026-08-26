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
    TOOLS,
    Context,
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
    "3. Never invent content. If neither memory nor a tool can answer, say so."
)

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

    trace: list[dict[str, Any]] = []
    if matches:
        trace.append({
            "recall": [
                {"sim": m["sim"], "title": m["title"], "url": m["url"]}
                for m in matches
            ]
        })
    tool_error_retried = False

    answer = "Reached tool-call limit without a final answer."
    requires_confirmation = False
    draft: dict[str, Any] | None = None
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
            if spec.get("requires_confirmation"):
                # Terminal state: the tool's output IS the artifact awaiting
                # explicit human action. Do not let the model keep generating
                # or call other tools after producing a draft.
                answer = result
                requires_confirmation = True
                draft = {
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
        metadata={
            "total_duration_ns": data.get("total_duration"),
            "eval_duration_ns": data.get("eval_duration"),
            "done_reason": data.get("done_reason"),
        },
    )
    return data


def _safe_json(s: Any) -> dict:
    try:
        return json.loads(s) if isinstance(s, str) else {}
    except Exception:
        return {}
