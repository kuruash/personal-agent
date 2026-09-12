"""Tool registry.

Every tool has one JSON schema entry (name, description, parameters,
side_effecting, requires_confirmation) and one implementation
(imported from `impls.py`). The `side_effecting` /
`requires_confirmation` flags live in the schema, not inferred by the
model at call time.

Three action tiers govern the flags:
  - read-only (autonomous)         — side_effecting=False, requires_confirmation=False
  - draft     (autonomous, inert)  — side_effecting=False, requires_confirmation=True
  - write     (explicit per-call)  — side_effecting=True,  requires_confirmation=True
"""

from __future__ import annotations

from typing import Any

from .context import ToolImpl
from .impls import (
    _detect_form_fields,
    _draft_email_reply,
    _extract_page_text,
    _fill_form_field,
    _read_email_thread,
    _summarize_transcript,
)


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
