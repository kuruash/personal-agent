"""Phase 1: /ask runs a tool-calling loop against Ollama.

Flow: user question + tool specs go to Ollama's /api/chat. If the model returns
tool_calls, we validate the name against the registry, run the implementation
against the request's context bundle, feed the result back as a `tool` message,
and loop. On malformed tool calls (unknown name), we retry once with the
validation error injected. Stops after MAX_ITERS.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .tools import (
    IMPLS,
    MODEL,
    TOOLS,
    Context,
    ollama_tool_specs,
)

OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
MAX_ITERS = 4

SYSTEM_PROMPT = (
    "You are a local browser-side assistant. The user is looking at a web page "
    "or a YouTube video. You have tools that read the current tab. Call a tool "
    "when the user's question is about the current tab's content. Do not "
    "invent content — call the appropriate tool first. Once you have enough "
    "information, answer the user directly in plain text."
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


class AskRequest(BaseModel):
    question: str
    context: ContextPayload = Field(default_factory=ContextPayload)


@app.post("/ask")
async def ask(req: AskRequest) -> dict[str, Any]:
    ctx = Context(**req.context.model_dump())
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_message(req.question, ctx)},
    ]
    trace: list[dict[str, Any]] = []
    tool_error_retried = False

    async with httpx.AsyncClient(timeout=180.0) as client:
        for _ in range(MAX_ITERS):
            resp = await _chat(client, messages)
            msg = resp.get("message", {}) or {}
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                content = (msg.get("content") or "").strip()
                return {"answer": content, "trace": trace}

            # Only handle the first tool call per turn; append the assistant
            # message so the model sees its own call in the next round.
            messages.append(msg)
            call = tool_calls[0]
            fn = (call.get("function") or {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", {}) or {}
            args = raw_args if isinstance(raw_args, dict) else _safe_json(raw_args)

            if name not in IMPLS:
                if tool_error_retried:
                    return {
                        "answer": f"Model kept calling unknown tool '{name}'.",
                        "trace": trace,
                    }
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

            try:
                result = await IMPLS[name](args, ctx)
            except Exception as e:  # pragma: no cover
                result = f"Tool '{name}' failed: {e}"

            trace.append({"tool": name, "chars": len(result)})
            messages.append({"role": "tool", "content": result})

    return {
        "answer": "Reached tool-call limit without a final answer.",
        "trace": trace,
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
    return r.json()


def _safe_json(s: Any) -> dict:
    try:
        return json.loads(s) if isinstance(s, str) else {}
    except Exception:
        return {}
