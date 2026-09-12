"""Form-answering workflow.

Entry points:
  - `answer_form(fields)`           — non-streaming; returns {fields, counts}
  - `answer_form_streaming(fields)` — async generator yielding NDJSON events
  - `Route`                         — enum of the five routing outcomes
  - `route_field(field)`            — classify a single field

Read the modules in order to understand the workflow:
  1. router.py           — Route enum + route_field
  2. direct.py           — DIRECT_VALUE resolution
  3. boolean.py          — DIRECT_BOOLEAN resolution
  4. semantic.py         — evidence packaging for SEARCH / REASON
  5. prompt_builder.py   — batched Qwen prompt + response parsing
  6. validation.py       — answer validation / option-fit / phone reformat
  7. models.py           — plan_entry shape + final_counts
  8. constants.py        — thresholds
  9. orchestrator.py     — the streaming lifecycle that ties them together
"""

from .orchestrator import answer_form, answer_form_streaming
from .router import Route, route_field

__all__ = [
    "Route",
    "answer_form",
    "answer_form_streaming",
    "route_field",
]
