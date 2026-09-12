"""The Context bundle handed to every tool implementation.

Populated once per /ask request from the extension's context payload;
the tool loop reads it, tools read from it, nothing writes back to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


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
