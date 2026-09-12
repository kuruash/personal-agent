"""Form-answering orchestrator.

Coordinates the full lifecycle for a batch of detected fields:

    fields
      → route each field (router.route_field)
      → answer DIRECT_VALUE / DIRECT_BOOLEAN inline
      → for SEARCH / REASON:
          → hybrid retrieval → evidence packaging (semantic.dedupe_by_related)
          → ONE batched Qwen call (prompt_builder + llm.ollama_client._ollama_json)
          → per-field validation (validation.validate_qwen_answer)
      → emit NDJSON-shaped events for the panel

The streaming shape is preserved verbatim:

    {"event":"meta", ...}                        — once, up front
    {"event":"phase","phase":"direct_done", ...} — placeholders included
    {"event":"phase","phase":"generating", ...}  — before Qwen
    {"event":"field", ...}                       — one per semantic field
    {"event":"done", ...}                        — final counters

`answer_form()` is the non-streaming compatibility wrapper.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

from ...llm.ollama_client import _ollama_json
from ...observability import get_client
from ...retrieval.hybrid import retrieve_hybrid
from .boolean import answer_direct_boolean
from .constants import (
    _MAX_EVIDENCE_DOCS_PER_FIELD,
    _MIN_EVIDENCE_SCORE,
    _QWEN_NUM_PREDICT_CEILING,
    _QWEN_NUM_PREDICT_FLOOR,
    _QWEN_NUM_PREDICT_PER_FIELD,
)
from .direct import answer_certifications_list, answer_direct_value
from .models import final_counts, plan_entry, review_policy
from .prompt_builder import build_batch_prompt, parse_batch_response
from .router import Route, route_field
from .semantic import dedupe_by_related
from .validation import is_unqweenable_choice, validate_qwen_answer


async def answer_form_streaming(fields: list[dict]) -> AsyncIterator[dict]:
    """Yield NDJSON-shaped events for progressive filling.

    Sequence:
      1. {"event":"meta", "total_fields":N, "routing":{...}}
      2. {"event":"phase","phase":"direct_done","fields":[plan_entry,...]}
         — includes ALL fields; DIRECT ones are final (state=ready/unknown),
         SEMANTIC ones are placeholders (state="retrieving") so the panel
         can render everything immediately with the right badges.
      3. Per semantic field:
         {"event":"phase","phase":"generating","field_ids":[...]}
         (before the single batched Qwen call)
         {"event":"field", "field_id":N, ...plan_entry...}
         (one per semantic field, after Qwen returns and validation runs)
      4. {"event":"done","counts":{...},"total_ms":...}

    Field ids equal the field's index in the input list — stable, unique
    across duplicate labels ("Email" vs "Confirm Email")."""
    lf = get_client()
    t_start = time.perf_counter()

    # ---- Route + direct phase ----
    with lf.start_as_current_observation(
        name="profile.form.route",
        as_type="span",
        input={"total_fields": len(fields)},
    ) as route_span:
        routed: list[tuple[int, dict, Route, dict]] = []
        for i, f in enumerate(fields):
            r, extra = route_field(f)
            routed.append((i, f, r, extra))
        counts_by_route: dict[str, int] = {}
        for _, _, r, _ in routed:
            counts_by_route[r.value] = counts_by_route.get(r.value, 0) + 1
        route_span.update(metadata={"routes": counts_by_route})

    yield {
        "event": "meta",
        "total_fields": len(fields),
        "routing": counts_by_route,
    }

    plans: list[dict | None] = [None] * len(fields)
    semantic_queue: list[tuple[int, dict, Route]] = []

    with lf.start_as_current_observation(name="profile.form.direct", as_type="span") as direct_span:
        t_direct = time.perf_counter()
        for idx, f, route, extra in routed:
            if route == Route.DIRECT_VALUE:
                if "certifications_list" in extra:
                    p = answer_certifications_list(f, extra["certifications_list"])
                else:
                    p = answer_direct_value(f, extra["concept"])
                p["field_id"] = idx
                p["route"] = route.value
                p.update(review_policy(p["route"], p.get("source") or "", p.get("state") or "unknown"))
                p["latency_ms"] = round((time.perf_counter() - t_direct) * 1000, 1)
                plans[idx] = p
            elif route == Route.DIRECT_BOOLEAN:
                p = answer_direct_boolean(f, extra)
                p["field_id"] = idx
                p["route"] = route.value
                p.update(review_policy(p["route"], p.get("source") or "", p.get("state") or "unknown"))
                p["latency_ms"] = round((time.perf_counter() - t_direct) * 1000, 1)
                plans[idx] = p
            elif route in (Route.SEARCH, Route.REASON):
                # Guard: if this is a choice field with a huge option list
                # AND no DIRECT concept matched, Qwen can't safely pick —
                # mark UNKNOWN pre-retrieval and skip the whole semantic
                # cost. Prevents "country list" style prompt bloat.
                if is_unqweenable_choice(f):
                    plans[idx] = plan_entry(
                        f, "",
                        f"choice field with {len(f.get('options') or [])} options and no direct concept — skipped",
                        "unknown",
                        field_id=idx, route=route.value, latency_ms=0.0,
                    )
                else:
                    semantic_queue.append((idx, f, route))
                    # Placeholder so the panel can render a row now.
                    plans[idx] = plan_entry(
                        f, "", f"{route.value}: retrieving evidence…", "retrieving",
                        field_id=idx, route=route.value,
                    )
            else:
                plans[idx] = plan_entry(
                    f, "", "unrouted", "unknown",
                    field_id=idx, route=route.value, latency_ms=0.0,
                )
        direct_ms = (time.perf_counter() - t_direct) * 1000
        direct_span.update(metadata={
            "direct_value": counts_by_route.get("direct_value", 0),
            "direct_boolean": counts_by_route.get("direct_boolean", 0),
            "deferred_to_semantic": len(semantic_queue),
            "direct_phase_ms": round(direct_ms, 1),
        })

    yield {
        "event": "phase",
        "phase": "direct_done",
        "fields": plans,        # includes placeholders for semantic
        "direct_phase_ms": round(direct_ms, 1),
    }

    if not semantic_queue:
        yield {
            "event": "done",
            "counts": final_counts(plans),
            "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
        }
        return

    # ---- Retrieval ----
    bundles: list[dict] = []
    ret_start = time.perf_counter()
    with lf.start_as_current_observation(
        name="profile.retrieve.hybrid",
        as_type="span",
        input={"pending_fields": len(semantic_queue)},
    ) as ret_span:
        skipped = 0
        for idx, f, route in semantic_queue:
            t_field = time.perf_counter()
            hits = await retrieve_hybrid(f.get("label") or "", top_k=5)
            strong = [h for h in hits if h.get("hybrid_score", 0) >= _MIN_EVIDENCE_SCORE]
            deduped = dedupe_by_related(strong)[:_MAX_EVIDENCE_DOCS_PER_FIELD] if strong else []
            retrieval_ms = (time.perf_counter() - t_field) * 1000
            if not deduped:
                plans[idx] = plan_entry(
                    f, "",
                    f"hybrid: no evidence above threshold {_MIN_EVIDENCE_SCORE}",
                    "unknown",
                    field_id=idx, route=route.value, latency_ms=round(retrieval_ms, 1),
                )
                skipped += 1
                yield {"event": "field", **plans[idx]}
                continue
            bundles.append({
                "index": idx,
                "label": f.get("label") or "",
                "field_type": f.get("type") or "",
                "options": [(o.get("text") or o.get("value") or "")
                            for o in (f.get("options") or [])],
                "evidence": deduped,
                "_field": f,
                "_route": route.value,
                "_retrieval_ms": retrieval_ms,
            })
        ret_span.update(metadata={
            "batched_for_qwen": len(bundles),
            "skipped_no_evidence": skipped,
            "retrieve_phase_ms": round((time.perf_counter() - ret_start) * 1000, 1),
        })

    if not bundles:
        yield {
            "event": "done",
            "counts": final_counts(plans),
            "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
        }
        return

    # Announce which fields are about to be Qwen-generated.
    yield {
        "event": "phase",
        "phase": "generating",
        "field_ids": [b["index"] for b in bundles],
    }

    # ---- Batched Qwen ----
    num_predict = max(
        _QWEN_NUM_PREDICT_FLOOR,
        min(_QWEN_NUM_PREDICT_CEILING, _QWEN_NUM_PREDICT_PER_FIELD * len(bundles)),
    )
    with lf.start_as_current_observation(
        name="profile.form.answer_batch",
        as_type="span",
        input={"batched_field_count": len(bundles)},
        metadata={
            "field_indices": [b["index"] for b in bundles],
            "evidence_ids_per_field": {
                str(b["index"]): [d["id"] for d in b["evidence"]] for b in bundles
            },
            "num_predict": num_predict,
        },
    ) as ans_span:
        qwen_start = time.perf_counter()
        prompt = build_batch_prompt(bundles)
        text = await _ollama_json(prompt, num_predict=num_predict)
        qwen_ms = (time.perf_counter() - qwen_start) * 1000
        answers = parse_batch_response(text, [b["index"] for b in bundles])
        for b in bundles:
            idx = b["index"]
            f = b["_field"]
            ans = answers.get(idx, "")
            ok, reason = validate_qwen_answer(ans, f)
            total_ms = b["_retrieval_ms"] + qwen_ms
            if ok:
                ev_ids = ",".join(d["id"] for d in b["evidence"])
                plans[idx] = plan_entry(
                    f, ans, f"qwen(evidence={ev_ids})", "ready",
                    field_id=idx, route=b["_route"], latency_ms=total_ms,
                )
            else:
                plans[idx] = plan_entry(
                    f, "", f"qwen answer rejected: {reason}", "unknown",
                    field_id=idx, route=b["_route"], latency_ms=total_ms,
                )
            yield {"event": "field", **plans[idx]}
        ans_span.update(
            output={"answers": answers},
            metadata={
                "prompt_chars": len(prompt),
                "qwen_ms": round(qwen_ms, 1),
            },
        )

    yield {
        "event": "done",
        "counts": final_counts(plans),
        "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
    }


async def answer_form(fields: list[dict]) -> dict:
    """Non-streaming compatibility path — collects the streaming
    generator into the same {fields, counts} shape the old `/ask`
    route expects. Used when the caller doesn't want progressive
    updates."""
    plans_by_id: dict[int, dict] = {}
    counts: dict = {}
    async for event in answer_form_streaming(fields):
        et = event.get("event")
        if et == "phase" and event.get("phase") == "direct_done":
            for p in event.get("fields") or []:
                if p and "field_id" in p:
                    plans_by_id[p["field_id"]] = p
        elif et == "field":
            fid = event.get("field_id")
            if fid is not None:
                # Strip the "event" key before storing so the shape
                # matches other plan entries.
                pe = {k: v for k, v in event.items() if k != "event"}
                plans_by_id[fid] = pe
        elif et == "done":
            counts = event.get("counts") or {}
    plans = [plans_by_id.get(i) or plan_entry(fields[i], "", "unhandled", "unknown", field_id=i)
             for i in range(len(fields))]
    # Any retrieving/generating placeholders left over become UNKNOWN.
    for p in plans:
        if p.get("state") in ("retrieving", "generating"):
            p["state"] = "unknown"
            p["source"] = (p.get("source") or "") + " (stream ended before completion)"
    return {"fields": plans, "counts": counts}
