"""Plan-entry shape (what the panel renders) + result counters.

Every answerer (direct, boolean, semantic) produces a plan entry via
`plan_entry(...)`. Streaming events carry the same shape; the panel
reads `state` to decide badges and `value` to decide auto-fill.
"""

from __future__ import annotations


def review_policy(route: str | None, source: str, state: str) -> dict:
    """Return browser review metadata derived from answer provenance."""
    is_ready = state == "ready"
    is_direct = route in ("direct_value", "direct_boolean")
    is_deterministic = source.startswith(("direct_value:", "direct_lookup:"))
    can_auto_fill = is_ready and is_direct and is_deterministic
    if can_auto_fill:
        return {
            "confidence": "high",
            "requires_review": False,
            "status": "confident",
        }
    if is_ready:
        return {
            "confidence": "medium",
            "requires_review": True,
            "status": "review_required",
        }
    return {
        "confidence": "low",
        "requires_review": True,
        "status": "review_required",
    }


def plan_entry(
    field: dict, value: str, source: str, state: str,
    *,
    field_id: int | None = None,
    latency_ms: float | None = None,
    route: str | None = None,
) -> dict:
    """Shape consumed by the panel. Includes frameId + shadowPath so
    Fill routes back to the exact frame the field came from, and
    `field_id` so streaming patches match by stable id (not label —
    "Email" and "Confirm Email" collide)."""
    entry = {
        "field_id": field_id if field_id is not None else field.get("field_id"),
        "selector": field.get("selector"),
        "label": (
            field.get("label")
            or field.get("aria_label")
            or field.get("placeholder")
            or field.get("name")
            or field.get("id")
            or ""
        ),
        "type": field.get("type") or field.get("tag"),
        "required": field.get("required", False),
        "current_value": field.get("current_value") or "",
        "options": field.get("options"),
        "frameId": field.get("frameId"),
        "shadowPath": field.get("shadowPath") or [],
        "value": value,
        "state": state,          # "ready" | "unknown" | "retrieving" | "generating"
        "source": source,
        "route": route,
    }
    entry.update(review_policy(route, source, state))
    if latency_ms is not None:
        entry["latency_ms"] = round(latency_ms, 1)
    return entry


def final_counts(plans: list[dict | None]) -> dict:
    out = {"total": len(plans), "ready": 0, "unknown": 0}
    for p in plans:
        if not p:
            continue
        if p["state"] == "ready":
            out["ready"] += 1
        else:
            out["unknown"] += 1
    return out
