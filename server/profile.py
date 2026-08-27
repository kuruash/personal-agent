"""Phase 4 profile store, v2 schema.

profile.json is a rich per-key record:
    {"facts": {
        "personal_email": {
            "value": "...",
            "type": "email",
            "description": "...",
            "aliases": ["email", "email address", ...],
            "autocomplete_tokens": ["email"],
            ...
        },
        ...
    }, "derived_facts": {...}, "preferences": {...}}

Only `facts.*` are matchable today; `derived_facts` and `preferences` are
metadata for future logic. Empty string values (or REPLACE_ME_-prefixed
stubs) mean the user has intentionally left the fact unknown — the mapper
still returns the key with confidence, but no value to fill.

Matching uses aliases and descriptions (not values — values are never
embedded). See server/tools.py for the three-stage mapper.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .memory import embed

PROFILE_PATH = Path(__file__).parent / "profile.json"


def _load_raw() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    with PROFILE_PATH.open() as f:
        return json.load(f)


def _facts() -> dict[str, dict]:
    raw = _load_raw()
    return raw.get("facts", {}) if isinstance(raw, dict) else {}


def _auto_description(key: str) -> str:
    """Fallback description when facts.{key}.description is missing."""
    return key.replace("_", " ").replace("-", " ")


def _normalize_alias(s: str) -> str:
    return " ".join(s.replace("_", " ").replace("-", " ").lower().split())


def load_profile() -> dict[str, str]:
    """Return {key: value} for facts that have a real, populated value.

    A value is "unset" if it's empty or still starts with REPLACE_ME. Unset
    keys are omitted so the mapper treats them as "key known but no answer"
    rather than filling the placeholder string.
    """
    out: dict[str, str] = {}
    for key, spec in _facts().items():
        if not isinstance(spec, dict):
            continue
        v = spec.get("value")
        if not isinstance(v, str):
            continue
        vs = v.strip()
        if not vs or vs.startswith("REPLACE_ME"):
            continue
        out[key] = vs
    return out


def profile_keys() -> list[str]:
    """All facts keys, including unset ones."""
    return list(_facts().keys())


def get_profile_descriptions() -> dict[str, str]:
    """Return {key: description}. Auto-generates from the key name when the
    schema omits an explicit description — same fallback rule as v1 keys
    that predate the v2 rewrite."""
    out: dict[str, str] = {}
    for key, spec in _facts().items():
        if not isinstance(spec, dict):
            continue
        desc = spec.get("description")
        if not isinstance(desc, str) or not desc.strip():
            desc = _auto_description(key)
        out[key] = desc.strip()
    return out


def get_profile_aliases() -> dict[str, list[str]]:
    """Return {key: [normalized aliases]}. Includes autocomplete tokens as
    aliases too so a label like 'given-name' still short-circuits at stage 1
    without needing to duplicate tokens in the aliases array."""
    out: dict[str, list[str]] = {}
    for key, spec in _facts().items():
        if not isinstance(spec, dict):
            continue
        raw_aliases = list(spec.get("aliases") or [])
        raw_aliases.extend(spec.get("autocomplete_tokens") or [])
        # Include the key itself as an alias so an exact key-name label still
        # hits (rare on real forms, common in test fixtures).
        raw_aliases.append(key)
        seen: set[str] = set()
        norm: list[str] = []
        for a in raw_aliases:
            if not isinstance(a, str):
                continue
            n = _normalize_alias(a)
            if n and n not in seen:
                seen.add(n)
                norm.append(n)
        out[key] = norm
    return out


# Cache: {descriptions_snapshot_id → {key: (description, unit_vector)}}.
# Snapshot-id is a hash of the {key: description} dict, so any edit to
# profile.json that changes descriptions invalidates automatically without
# needing an mtime watch.
_embed_cache: dict[str, object] = {"snapshot_id": None, "vecs": {}}


def _descriptions_snapshot_id(descs: dict[str, str]) -> str:
    return json.dumps(descs, sort_keys=True)


async def get_profile_embeddings() -> dict[str, tuple[str, np.ndarray]]:
    """Return {key: (description, embedding)} for all facts keys, cached."""
    descs = get_profile_descriptions()
    snap = _descriptions_snapshot_id(descs)
    if _embed_cache["snapshot_id"] == snap and _embed_cache["vecs"]:
        return _embed_cache["vecs"]  # type: ignore[return-value]

    vecs: dict[str, tuple[str, np.ndarray]] = {}
    for key, desc in descs.items():
        vec = await embed(desc)
        vecs[key] = (desc, vec)
    _embed_cache["snapshot_id"] = snap
    _embed_cache["vecs"] = vecs
    return vecs
