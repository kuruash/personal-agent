"""Answer validation and format guards for the form-answering pipeline.

Owns:
  - `value_fits_options`      — case-insensitive bidirectional-substring
                                match against a field's option list.
  - `is_unqweenable_choice`   — choice field with too many options and
                                no DIRECT concept — refuse Qwen safely.
  - `validate_qwen_answer`    — post-Qwen sanity check: option-fit for
                                choices, length ceiling for text, and
                                a heuristic against Qwen echoing the
                                option list.
  - `reformat_phone_for_field`— optional formatting hint driven by the
                                field's label/aria/placeholder/pattern.

Nothing here calls the network. Route decisions live in `router.py`.
"""

from __future__ import annotations

import re
from typing import Any

from .constants import (
    _MAX_ANSWER_LEN_TEXT,
    _MAX_OPTIONS_FOR_QWEN,
)


def value_fits_options(value: Any, options: list[dict] | None) -> bool:
    """Return True iff `value` maps onto at least one option in the list.

    Match is case-insensitive and bidirectional-substring: option value OR
    option display text must equal, contain, or be contained in the profile
    value. Booleans are coerced to "yes"/"no" first so a stored
    `"requires_sponsorship": true` maps onto a Yes/No radio group without
    forcing the user to write "yes" as a string in their profile.
    """
    if not options or value is None:
        return False
    if isinstance(value, bool):
        value = "yes" if value else "no"
    v = str(value).strip().lower()
    if not v:
        return False
    for opt in options:
        for candidate in (opt.get("value"), opt.get("text")):
            if not candidate:
                continue
            c = str(candidate).strip().lower()
            if not c:
                continue
            if v == c or v in c or c in v:
                return True
    return False


def is_unqweenable_choice(field: dict) -> bool:
    """Choice fields with many options and no DIRECT concept fall
    outside Qwen's safe zone (see 'Country list' failure). Refuse to
    ship the option list to Qwen — mark UNKNOWN instead."""
    t = (field.get("type") or "").lower()
    if t not in ("select", "select-multiple", "radio_group", "checkbox_group"):
        return False
    opts = field.get("options") or []
    return len(opts) > _MAX_OPTIONS_FOR_QWEN


def validate_qwen_answer(answer: str, field: dict) -> tuple[bool, str]:
    if not answer:
        return False, "empty answer"
    options = field.get("options") or []
    if options:
        if value_fits_options(answer, options):
            return True, "option-fit ok"
        # Guard against option-list leakage — if the answer contains 3+
        # option texts joined by commas, Qwen echoed the option list.
        if len(answer) > 200 and answer.count(",") >= 3:
            return False, "answer looks like a copied option list"
        return False, f"answer {answer[:60]!r} did not fit any option"
    if len(answer) > _MAX_ANSWER_LEN_TEXT:
        return False, f"answer too long ({len(answer)} chars) — likely evidence leak"
    return True, "text ok"


def reformat_phone_for_field(value: str, field: dict) -> str | None:
    """If the field carries an explicit phone-format hint, reformat the raw
    digits of `value` to match. Returns None when no hint is detected (caller
    keeps the profile value as-is).

    Hint sources scanned (concatenated, lowercased):
      - label / aria_label / placeholder text — carries hints like
        "Format: 703-993-2999" or "e.g. (703) 993-2999".
      - the input's `pattern` attribute if present.

    Recognized shapes:
      ##########              → 10 raw digits
      ###-###-####            → dashes
      (###) ###-####          → parens + space + dash
      ###.###.####            → dots
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) != 10:
        return None
    hint = " ".join([
        str(field.get("label") or ""),
        str(field.get("aria_label") or ""),
        str(field.get("placeholder") or ""),
        str(field.get("pattern") or ""),
    ]).lower()
    if not hint.strip():
        return None
    # Order matters: check most-specific patterns first.
    if re.search(r"\(\s*\d{3}\s*\)\s*\d{3}[-\s]\d{4}", hint) or "(###) ###-####" in hint:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    if re.search(r"\d{3}\.\d{3}\.\d{4}", hint) or "###.###.####" in hint:
        return f"{digits[0:3]}.{digits[3:6]}.{digits[6:10]}"
    if re.search(r"\d{3}-\d{3}-\d{4}", hint) or "###-###-####" in hint:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    if "##########" in hint or re.search(r"10[- ]digit", hint):
        return digits
    return None
