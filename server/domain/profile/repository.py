"""Single source of truth for reading profile.json.

The user's profile is stored as a hierarchical JSON document at
`server/domain/profile/profile.json`. Values are looked up by dotted
path (e.g. `"schema.identity.full_name"`).

Missing keys, `null`, and empty-string-after-strip all resolve to `None` —
callers treat that uniformly as "unknown", which the form-fill pipeline
surfaces as an UNKNOWN row rather than substituting a similar value.
Boolean `False` and `0` survive the walk because they are legitimate
answers (e.g. `requires_sponsorship: false`).

Every module in the profile-QA stack (indexer, direct_lookup, retrieval,
etc.) goes through this repository — nobody else calls
`open("profile.json")`. Keeping the read path in one place makes it easy
to add caching, hashing, and file-change detection later without hunting
through the codebase.

Design notes:
  - Structural accessors return empty lists / dicts (never None) for
    missing sections, so callers can write straight-line code.
  - `False` and `0` survive intact (nothing coerces truthy). `null` in
    the JSON becomes Python `None`.
  - `profile_fingerprint()` is a content-hash used by the indexer to
    decide when to rebuild.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PROFILE_PATH = Path(__file__).parent / "profile.json"


# ---------- raw read + dotted-path walk ----------

def load_profile() -> dict:
    """Read and parse profile.json. Returns `{}` if the file is missing."""
    if not PROFILE_PATH.exists():
        return {}
    with PROFILE_PATH.open() as f:
        return json.load(f)


def get_path(profile: dict, dotted: str) -> Any:
    """Walk a dotted path into the profile."""
    if not dotted:
        return None
    node: Any = profile
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
        if node is None:
            return None
    if isinstance(node, str):
        s = node.strip()
        return s or None
    return node


def get_raw_profile() -> dict:
    """Return the whole parsed profile.json. Prefer the typed accessors
    below — this exists for the rare case that needs the full document
    (e.g. sending the profile to Qwen as one blob)."""
    return load_profile()


# ---------- structural accessors ----------

def _collections() -> dict:
    return get_raw_profile().get("collections", {}) or {}


def get_work_experiences() -> list[dict]:
    return list(_collections().get("work_experiences", []) or [])


def get_education() -> list[dict]:
    return list(_collections().get("education_history", []) or [])


def get_projects() -> list[dict]:
    return list(_collections().get("projects", []) or [])


def get_skills() -> dict[str, list[str]]:
    """Return {category: [skill, ...]}. Order preserved."""
    raw = _collections().get("skills", {}) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for category, items in raw.items():
        if isinstance(items, list):
            out[category] = [str(s) for s in items if s]
    return out


def get_certifications() -> list[str]:
    raw = _collections().get("certifications", []) or []
    return [str(c) for c in raw if c]


def get_publications() -> list[dict | str]:
    return list(_collections().get("publications", []) or [])


def get_references() -> list[dict]:
    return list(_collections().get("references", []) or [])


# ---------- fingerprint + summary ----------

def profile_fingerprint() -> str:
    """Content hash of profile.json. Used by the indexer to detect when
    a rebuild is needed. Missing file → empty-string hash so callers
    can compare stably even before the file exists."""
    if not PROFILE_PATH.exists():
        return ""
    with PROFILE_PATH.open("rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def profile_summary() -> dict[str, Any]:
    """Small dict describing what the loader sees. Handy for logging and
    for the retrieval test harness."""
    return {
        "work_experiences": len(get_work_experiences()),
        "education": len(get_education()),
        "projects": len(get_projects()),
        "skill_categories": len(get_skills()),
        "skills_total": sum(len(v) for v in get_skills().values()),
        "certifications": len(get_certifications()),
        "publications": len(get_publications()),
        "references": len(get_references()),
        "fingerprint": profile_fingerprint()[:12],
    }
