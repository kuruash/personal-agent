"""Simple form-fill resolver.

Two paths for every field:

    1. OBVIOUS — a small list of fields whose label unambiguously means the
       same thing on any form: first name, last name, email, phone, address
       parts, personal links. Matched by HTML autocomplete tokens or a strict
       full-label alias match. Value comes straight from the profile.

    2. OTHERWISE — collected and sent to Qwen in a SINGLE call with the
       user's profile as context. Qwen returns the actual answer string
       per field (or empty string when the profile has nothing to say).
       User reviews the answers in the panel and clicks Fill.

No concept catalog, no modes, no per-field ambiguity gate. If a field is
obvious, one deterministic lookup. Everything else is one LLM call.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .profile import get_path


# Small on purpose. Only include fields whose label is unambiguous on any
# form — if the same phrase can mean two different things across forms, it
# belongs in the Qwen stage, not here.
OBVIOUS: dict[str, dict[str, Any]] = {
    "first_name": {
        "path": "schema.identity.first_name",
        "aliases": ("first name", "given name", "forename"),
        "autocomplete": ("given-name",),
    },
    "last_name": {
        "path": "schema.identity.last_name",
        "aliases": ("last name", "family name", "surname"),
        "autocomplete": ("family-name",),
    },
    "full_name": {
        "path": "schema.identity.full_name",
        "aliases": ("full name", "your name", "legal name", "name"),
        "autocomplete": ("name",),
    },
    "email": {
        "path": "schema.contact.personal_email",
        "aliases": ("email", "email address", "personal email",
                    "contact email", "primary email"),
        "autocomplete": ("email",),
    },
    "phone": {
        "path": "schema.contact.phone",
        "aliases": ("phone", "phone number", "mobile", "mobile number",
                    "cell phone", "telephone", "contact number"),
        "autocomplete": ("tel",),
    },
    "street": {
        "path": "schema.address.street",
        "aliases": ("street address", "address line 1", "home address",
                    "mailing address"),
        "autocomplete": ("street-address", "address-line1"),
    },
    "city": {
        "path": "schema.address.city",
        "aliases": ("city", "town", "locality"),
        "autocomplete": ("address-level2",),
    },
    "state": {
        "path": "schema.address.state",
        "aliases": ("state", "province", "state or province", "region"),
        "autocomplete": ("address-level1",),
    },
    "postal_code": {
        "path": "schema.address.postal_code",
        "aliases": ("zip", "zip code", "postal code", "postcode"),
        "autocomplete": ("postal-code",),
    },
    "country": {
        "path": "schema.address.country",
        "aliases": ("country",),
        "autocomplete": ("country", "country-name"),
    },
    "linkedin_url": {
        "path": "schema.web_presence.linkedin_url",
        "aliases": ("linkedin", "linkedin profile", "linkedin url"),
    },
    "github_url": {
        "path": "schema.web_presence.github_url",
        "aliases": ("github", "github profile", "github url"),
    },
    "personal_website": {
        "path": "schema.web_presence.personal_website",
        "aliases": ("website", "personal website", "portfolio",
                    "portfolio website"),
        "autocomplete": ("url",),
    },
}


def _normalize(text: str) -> str:
    if not text:
        return ""
    s = text.lower().strip()
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch == " ") else " ")
    return " ".join("".join(out).split())


def match_obvious(label: str, autocomplete: str = "") -> str | None:
    """Return an OBVIOUS id when the field unambiguously maps to a standard
    slot. Autocomplete beats alias. Alias must equal the FULL normalized
    label — no substring guessing lives here."""
    ac = _normalize(autocomplete)
    if ac:
        for oid, info in OBVIOUS.items():
            if ac in (_normalize(t) for t in info.get("autocomplete", ())):
                return oid
    nl = _normalize(label)
    if not nl:
        return None
    for oid, info in OBVIOUS.items():
        if nl in (_normalize(a) for a in info["aliases"]):
            return oid
    return None


def obvious_value(oid: str, profile: dict) -> Any:
    return get_path(profile, OBVIOUS[oid]["path"])


# ---------- single Qwen call ----------

def build_prompt(unresolved: list[dict], profile: dict) -> str:
    """Build the one-shot Qwen prompt. Ships the profile as JSON context and
    asks for {index: answer_string}. Empty string when profile can't answer.
    """
    profile_json = json.dumps({
        "schema": profile.get("schema", {}),
        "collections": profile.get("collections", {}),
        "generation_context": profile.get("generation_context", {}),
    }, ensure_ascii=False, indent=2)

    lines = [
        "You are an autofill assistant. Below is the USER'S PROFILE and a list "
        "of form FIELDS. Produce the best answer for each field using the "
        "profile. Do not invent facts.",
        "",
        "Rules:",
        "- Short-answer / choice fields: pick the profile value or the option "
        "that best matches it.",
        "- Free-response fields (e.g. 'why are you interested', 'tell us about "
        "yourself', 'what skills do you bring'): write 2-4 sentences grounded "
        "in the profile's generation_context.",
        "- If the profile has no basis to answer, output an EMPTY STRING for "
        "that field — do NOT guess.",
        "",
        f"PROFILE:\n{profile_json}",
        "",
        "OUTPUT: valid JSON only, no prose, no code fences. Shape: "
        '{"0": "answer", "1": "answer", ...} — every field index appears '
        "exactly once, string values only.",
        "",
        "FIELDS:",
    ]
    for f in unresolved:
        idx = f["index"]
        label = f.get("label") or "(no label)"
        opts = f.get("options") or []
        lines.append(f"{idx}. {label!r}")
        if opts:
            lines.append(f"   options: {', '.join(opts[:12])}")
    lines.append("")
    lines.append("Output:")
    return "\n".join(lines)


def parse_response(text: str, indices: list[int]) -> dict[int, str]:
    """Extract {index: answer} from Qwen's JSON. Missing / malformed → ''."""
    if not text:
        return {i: "" for i in indices}
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
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
