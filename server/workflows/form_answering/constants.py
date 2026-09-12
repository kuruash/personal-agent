"""Thresholds and caps for the form-answering pipeline.

Any tuning knob that affects routing, retrieval, prompting, or answer
validation lives here so it can be scanned and adjusted without
grep-jumping between modules.
"""

from __future__ import annotations


# ---------- semantic evidence packaging ----------
_MIN_EVIDENCE_SCORE = 0.40                # hybrid_score floor for evidence
_MAX_EVIDENCE_DOCS_PER_FIELD = 2          # was 3; second doc rarely adds new signal after related_ids dedupe
_MAX_EVIDENCE_TEXT_CHARS = 260            # was 500; enough for one summary + one metric line

# ---------- prompt shape ----------
_MAX_OPTIONS_IN_PROMPT = 8                # was 20; if more options, we mark UNKNOWN pre-Qwen instead
_MAX_OPTIONS_FOR_QWEN = 15                # over this many options AND no DIRECT concept → UNKNOWN, don't Qwen

# ---------- Qwen output caps ----------
_MAX_ANSWER_LEN_TEXT = 500                # was 800; matches the "1-2 sentences" instruction below
_QWEN_NUM_PREDICT_PER_FIELD = 120         # soft cap; total batch limit = this * batched_field_count
_QWEN_NUM_PREDICT_FLOOR = 200
_QWEN_NUM_PREDICT_CEILING = 800
