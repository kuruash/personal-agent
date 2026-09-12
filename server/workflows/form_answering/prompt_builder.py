"""Batched Qwen prompt construction + response parsing.

Every character in `build_batch_prompt` ships on every form-fill
request, so the prompt stays terse: one line per field, one indented
line per evidence document. Qwen returns a JSON dict keyed by field
index; `parse_batch_response` normalizes it back to
`{index: answer_string}`.
"""

from __future__ import annotations

import json
import re

from .constants import _MAX_EVIDENCE_TEXT_CHARS, _MAX_OPTIONS_IN_PROMPT


def build_batch_prompt(bundles: list[dict]) -> str:
    """Tight batched prompt. Every character counts — this ships on
    every form-fill request."""
    lines = [
        "Answer each FIELD using ONLY its EVIDENCE. Do not invent facts.",
        "Textarea: 1-2 concise sentences. Text: short direct answer. "
        "If OPTIONS given, output ONE option verbatim only. "
        "If evidence is insufficient, output an empty string.",
        'Output JSON only: {"<field_id>": "answer", ...}',
        "",
        "FIELDS:",
    ]
    for b in bundles:
        opts = b.get("options") or []
        if opts:
            shown = opts[:_MAX_OPTIONS_IN_PROMPT]
            more = f" (+{len(opts) - len(shown)})" if len(opts) > len(shown) else ""
            opt_str = f" opts=[{', '.join(shown)}{more}]"
        else:
            opt_str = ""
        lines.append(f"[{b['index']}] {b['field_type']} {b['label']!r}{opt_str}")
        for d in b.get("evidence") or []:
            t = (d.get("text") or "").replace("\n", " ")
            if len(t) > _MAX_EVIDENCE_TEXT_CHARS:
                t = t[:_MAX_EVIDENCE_TEXT_CHARS] + "..."
            lines.append(f"  ev [{d['id']}] {t}")
    return "\n".join(lines) + "\n"


def parse_batch_response(text: str, indices: list[int]) -> dict[int, str]:
    if not text:
        return {i: "" for i in indices}
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return {i: "" for i in indices}
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return {i: "" for i in indices}
    out: dict[int, str] = {}
    for i in indices:
        v = raw.get(str(i)) if str(i) in raw else raw.get(i)
        if v is None:
            out[i] = ""
        elif isinstance(v, bool):
            out[i] = "yes" if v else "no"
        else:
            out[i] = str(v).strip()
    return out
