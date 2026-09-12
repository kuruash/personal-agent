"""POST /ask/form_stream — streaming form-fill endpoint.

Emits newline-delimited JSON:

    {"event":"session", "session_tab_id": null, "title": ..., "url": ...}
    {"event":"meta", ...}
    {"event":"phase","phase":"direct_done","fields":[...],...}
    {"event":"phase","phase":"generating","field_ids":[...]}
    {"event":"field", ...plan_entry...}
    {"event":"done","counts":{...},"total_ms":...}

Separate from /ask because /ask is the general ReAct path; this one
is specifically for the "fill this form" fast-intent flow and needs
to push interim results as soon as DIRECT fields resolve.
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langfuse import get_client, observe

from ...tools import Context
from ...workflows.form_answering import answer_form_streaming
from ..schemas import AskRequest

router = APIRouter()


@router.post("/ask/form_stream")
@observe(name="ask.form_stream", capture_input=False, capture_output=False)
async def ask_form_stream(req: AskRequest):
    lf = get_client()
    lf.update_current_span(
        input={"question": req.question, "url": req.context.url, "title": req.context.title},
        metadata={"form_fields_count": len(req.context.form_fields or [])},
    )
    ctx = Context(**req.context.model_dump())

    if not ctx.form_fields:
        async def _empty():
            yield json.dumps({"event": "done", "counts": {"total": 0}, "total_ms": 0}) + "\n"
        return StreamingResponse(_empty(), media_type="application/x-ndjson")

    async def _stream():
        # A meta frame the sidepanel can use to pin the session tab id
        # immediately, before any field rendering happens.
        yield json.dumps({
            "event": "session",
            "session_tab_id": None,  # background.js overlays the real tabId; server has no browser-side context
            "title": ctx.title,
            "url": ctx.url,
        }) + "\n"
        async for event in answer_form_streaming(ctx.form_fields):
            yield json.dumps(event) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")
