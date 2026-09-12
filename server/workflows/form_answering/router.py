"""Route a single form field to one of five answering strategies.

    Detected field
       ↓
    DIRECT_VALUE     — concept lookup on the profile schema
    DIRECT_BOOLEAN   — eligibility flag or profile_direct match
    SEARCH           — hybrid retrieval → batched Qwen
    REASON           — free-response textarea → batched Qwen
    UNKNOWN          — no data / unhandled

The router is deterministic and cheap — no network, no Ollama. It
only decides which downstream answerer runs. This module knows nothing
about the LLM or retrieval; it only reads the field payload.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from ...domain.profile.direct_lookup import direct_lookup


class Route(str, Enum):
    DIRECT_VALUE = "direct_value"
    DIRECT_BOOLEAN = "direct_boolean"
    SEARCH = "search"
    REASON = "reason"
    UNKNOWN = "unknown"


# Full-normalized-label → canonical value concept. Keep small and
# unambiguous. Membership is checked as an EXACT match after
# _normalize_label; no substring guessing here.
_DIRECT_VALUE_LABELS: dict[str, str] = {
    # names
    "first name": "first_name",
    "given name": "first_name",
    "forename": "first_name",
    "last name": "last_name",
    "family name": "last_name",
    "surname": "last_name",
    "preferred name": "preferred_name",
    "preferred first name": "preferred_name",
    "nickname": "preferred_name",
    "full name": "full_name",
    "legal name": "legal_name",
    "name": "full_name",
    "your name": "full_name",
    "applicant name": "full_name",
    # contact
    "email": "email",
    "email address": "email",
    "personal email": "email",
    "contact email": "email",
    "primary email": "email",
    "confirm email": "email",
    "confirm your email": "email",
    "phone": "phone",
    "phone number": "phone",
    "mobile": "phone",
    "mobile number": "phone",
    "cell phone": "phone",
    "telephone": "phone",
    "contact number": "phone",
    # address
    "country": "country",
    "city": "city",
    "town": "city",
    "location city": "city",  # "Location (City)" after normalization
    "state": "state",
    "province": "state",
    "state or province": "state",
    "zip": "postal_code",
    "zip code": "postal_code",
    "postal code": "postal_code",
    "postcode": "postal_code",
    "address": "street_address",
    "street address": "street_address",
    "address line 1": "street_address",
    "home address": "street_address",
    "mailing address": "street_address",
    # web presence
    "linkedin": "linkedin",
    "linkedin url": "linkedin",
    "linkedin profile": "linkedin",
    "github": "github",
    "github url": "github",
    "github profile": "github",
    "website": "website",
    "personal website": "website",
    "portfolio": "website",
    "portfolio website": "website",
    # education (backed by collections.education_history[0])
    "school": "school",
    "school name": "school",
    "university": "school",
    "university name": "school",
    "college": "school",
    "college name": "school",
    "institution": "school",
    "current school": "school",
    "degree": "degree",
    "degree type": "degree",
    "qualification": "degree",
    "major": "major",
    "field of study": "major",
    "discipline": "major",
    "program": "major",
    "area of study": "major",
    "graduation date": "graduation_date",
    "expected graduation": "graduation_date",
    "expected graduation date": "graduation_date",
    "graduation year": "graduation_year",
    "grad year": "graduation_year",
    "expected grad year": "graduation_year",
    "gpa": "gpa",
    "grade point average": "gpa",
    "cumulative gpa": "gpa",
}


# Boolean intents anchored on unambiguous keywords. Current profile has
# no eligibility section, so these presently resolve to UNKNOWN — but
# we still classify them here so they don't leak into search/Qwen.
_BOOLEAN_INTENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(1[89]|eighteen)\s*(?:or\s+(?:older|above))?\b", re.I), "age_18_or_older"),
    (re.compile(r"\bwork\s*authoriz|authorized\s+to\s+work", re.I), "work_authorization"),
    (re.compile(r"\bsponsor(?:ship|ing)?\b", re.I), "sponsorship"),
    (re.compile(r"\bwilling\s+to\s+relocate|open\s+to\s+relocation|relocate\b", re.I), "relocation"),
]


def _normalize_label(text: str) -> str:
    if not text:
        return ""
    s = text.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _direct_value_concept(label: str) -> str | None:
    return _DIRECT_VALUE_LABELS.get(_normalize_label(label))


def _boolean_intent(label: str) -> str | None:
    for pat, name in _BOOLEAN_INTENT_PATTERNS:
        if pat.search(label or ""):
            return name
    return None


def _is_reason_field(field: dict) -> bool:
    t = (field.get("type") or "").lower()
    if t == "textarea":
        return True
    label = field.get("label") or ""
    if len(label) > 90:
        return True
    n = _normalize_label(label)
    if re.search(r"\b(describe|explain|why|how|tell us|please share|elaborate|walk us through)\b", n):
        return True
    return False


def route_field(field: dict) -> tuple[Route, dict[str, Any]]:
    """Classify one field. Returns (route, extra) — `extra` carries the
    minimum data the answerer needs so it doesn't have to re-run the
    same detection (e.g. the resolved concept, the DirectMatch)."""
    label = field.get("label") or ""
    # 1. Direct value by exact-label lookup.
    concept = _direct_value_concept(label)
    if concept:
        return Route.DIRECT_VALUE, {"concept": concept}
    # 2. Boolean intents that need structured eligibility data.
    bi = _boolean_intent(label)
    if bi:
        return Route.DIRECT_BOOLEAN, {"boolean_intent": bi}
    # 3. Skill / worked-at boolean via existing direct_lookup.
    dm = direct_lookup(label)
    if dm.matched and dm.intent in ("skill_known", "worked_at"):
        return Route.DIRECT_BOOLEAN, {"direct_match": dm}
    if dm.matched and dm.intent == "certifications" and isinstance(dm.answer, list):
        return Route.DIRECT_VALUE, {"certifications_list": dm.answer}
    # 4. Reason / free-response.
    if _is_reason_field(field):
        return Route.REASON, {}
    # 5. Everything else → semantic retrieval.
    return Route.SEARCH, {}
