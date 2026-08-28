"""Profile loader.

The user's profile is stored as a hierarchical JSON document at
`server/profile.json`. Values are looked up by dotted path (e.g.
`"schema.identity.full_name"`).

Missing keys, `null`, and empty-string-after-strip all resolve to `None` —
callers treat that uniformly as "unknown", which the form-fill pipeline
surfaces as an UNKNOWN row rather than substituting a similar value.
Boolean `False` and `0` survive the walk because they are legitimate
answers (e.g. `requires_sponsorship: false`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROFILE_PATH = Path(__file__).parent / "profile.json"


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
