"""DIRECT_VALUE answering — resolve a routed concept to a profile value.

Handles the deterministic path: label → canonical concept → schema.*
dotted path lookup (with an education-only fallback to
`collections.education_history[0]`). No embeddings, no Qwen. Returns
a plan entry with state=ready or state=unknown.
"""

from __future__ import annotations

import re
from typing import Any

from ...domain.profile.repository import get_education, get_path, get_raw_profile
from .models import plan_entry
from .validation import value_fits_options


# Canonical schema paths for each direct-value concept. `get_path`
# handles missing keys, null, and empty-string-after-strip → None.
# For education, we consult schema.education first and fall back to
# collections.education_history[0] if the schema value is missing —
# one deterministic policy, not two contradictory ones.
_SCHEMA_PATHS: dict[str, str] = {
    "first_name":     "schema.identity.first_name",
    "last_name":      "schema.identity.last_name",
    "preferred_name": "schema.identity.preferred_name",
    "full_name":      "schema.identity.full_name",
    "legal_name":     "schema.identity.legal_name",
    "email":          "schema.contact.email",
    "phone":          "schema.contact.phone",
    "country":        "schema.address.country",
    "city":           "schema.address.city",
    "state":          "schema.address.state",
    "postal_code":    "schema.address.postal_code",
    "street_address": "schema.address.street_address",
    "linkedin":       "schema.web_presence.linkedin",
    "github":         "schema.web_presence.github",
    "website":        "schema.web_presence.website",
}

# Education concept → (schema path, fallback key on education_history[0]).
_EDUCATION_PATHS: dict[str, tuple[str, str]] = {
    "school":          ("schema.education.current_school", "school"),
    "degree":          ("schema.education.degree",          "degree"),
    "major":           ("schema.education.field_of_study",  "field"),
    "graduation_date": ("schema.education.graduation_date", "end_date"),
    "gpa":             ("schema.education.gpa",             "gpa"),
    # graduation_year is derived from graduation_date; handled below.
}


def _extract_year(dstr: str | None) -> str | None:
    m = re.match(r"^\s*(\d{4})", dstr or "")
    return m.group(1) if m else None


def resolve_direct_value(concept: str) -> tuple[Any, str]:
    """Look up a concept's value in the canonical profile.

    Order:
      1. schema.* path via get_raw_profile (source of truth).
      2. For education concepts only, fall back to
         collections.education_history[0] when the schema value is missing.
      3. Return (None, honest source string) if nothing is found.
    """
    profile = get_raw_profile()

    # Identity / contact / address / web — schema-only.
    if concept in _SCHEMA_PATHS:
        path = _SCHEMA_PATHS[concept]
        v = get_path(profile, path)
        if v is not None:
            return v, path
        return None, f"profile-missing:{concept} ({path} unset)"

    # Education — schema first, collections fallback.
    if concept in _EDUCATION_PATHS:
        schema_path, coll_key = _EDUCATION_PATHS[concept]
        v = get_path(profile, schema_path)
        if v is not None:
            return v, schema_path
        edus = get_education()
        if edus:
            v = edus[0].get(coll_key)
            if v is not None and str(v).strip() != "":
                return v, f"collections.education_history[0].{coll_key}"
        return None, f"profile-missing:{concept} ({schema_path} and education_history[0].{coll_key} both unset)"

    if concept == "graduation_year":
        gd = get_path(profile, "schema.education.graduation_date")
        if gd is None:
            edus = get_education()
            gd = edus[0].get("end_date") if edus else None
        year = _extract_year(gd)
        if year:
            return year, f"year({gd!r})"
        return None, "profile-missing:graduation_year"

    return None, f"unhandled concept {concept}"


def answer_direct_value(field: dict, concept: str) -> dict:
    val, source = resolve_direct_value(concept)
    if val is None:
        return plan_entry(field, "", source, "unknown")
    v = str(val)
    options = field.get("options")
    if options and not value_fits_options(v, options):
        return plan_entry(
            field, "",
            f"direct_value:{concept}={v!r} did not fit any option",
            "unknown",
        )
    return plan_entry(field, v, f"direct_value:{concept}", "ready")


def answer_certifications_list(field: dict, certs: list[str]) -> dict:
    if not certs:
        return plan_entry(field, "", "certifications: none in profile", "unknown")
    return plan_entry(field, ", ".join(certs), "direct_value:certifications", "ready")
