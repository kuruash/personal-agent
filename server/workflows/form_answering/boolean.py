"""DIRECT_BOOLEAN answering — resolve boolean-intent and skill/worked-at
matches to a yes/no field value.

Two shapes come in via the router's `extra` dict:
  - {"boolean_intent": "..."}      — eligibility intent (age, work auth,
                                     sponsorship, relocation). Current
                                     profile has no eligibility data →
                                     always UNKNOWN, honestly.
  - {"direct_match": DirectMatch}  — profile_direct's answer to
                                     "worked at X" / "know Y".

For choice fields we pick a Yes/No option that fits, and validate that
the picked value maps onto the field's option list before marking READY.
"""

from __future__ import annotations

from .models import plan_entry
from .validation import value_fits_options


def _pick_yes_no_option(field: dict, answer_bool: bool) -> str | None:
    options = field.get("options") or []
    if not options:
        return "Yes" if answer_bool else "No"
    target = "yes" if answer_bool else "no"
    for opt in options:
        for candidate in (opt.get("value") or "", opt.get("text") or ""):
            c = str(candidate).strip().lower()
            if c == target or (target == "yes" and c.startswith("yes")) \
                            or (target == "no" and c.startswith("no")):
                return (opt.get("value") or opt.get("text") or "")
    return None


def answer_direct_boolean(field: dict, extra: dict) -> dict:
    if "boolean_intent" in extra:
        # Current profile has no eligibility data. Don't guess — UNKNOWN.
        return plan_entry(
            field, "",
            f"boolean_intent:{extra['boolean_intent']} — no eligibility data in profile",
            "unknown",
        )
    dm = extra["direct_match"]
    if not isinstance(dm.answer, bool):
        return plan_entry(
            field, "",
            f"direct_lookup:{dm.intent} did not return a boolean",
            "unknown",
        )
    picked = _pick_yes_no_option(field, dm.answer)
    if picked is None:
        return plan_entry(
            field, "",
            f"direct_lookup:{dm.intent}={dm.answer} — no Yes/No option on this field",
            "unknown",
        )
    options = field.get("options") or []
    if options and not value_fits_options(picked, options):
        return plan_entry(
            field, "",
            f"direct_lookup:{dm.intent}={picked!r} did not fit options",
            "unknown",
        )
    return plan_entry(field, picked, f"direct_lookup:{dm.intent}", "ready")
