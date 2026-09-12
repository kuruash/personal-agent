"""Request models for the FastAPI endpoints.

The extension always sends `{question, context}`; the context bundle is
what tools read via `tools.Context`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
