"""Cheap structured lookup for obvious profile questions.

Answers "do you know X?" / "have you used X?" / "have you worked at X?"
/ "what school do you attend?" / "what degree?" / "what certifications
do you have?" without embeddings, without Qwen — a normalized substring
scan over structured collections.

This module stays deliberately small. It is NOT a giant alias catalog.
Only:
  - one tiny alias map for well-known technology abbreviations
    (shared via retrieval.normalize),
  - a handful of intent patterns anchored on unambiguous keywords.

Anything vague or narrative falls through — the caller then routes to
semantic retrieval, which is what should happen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ...retrieval.normalize import contains_term, normalize_text
from .repository import (
    get_certifications,
    get_education,
    get_projects,
    get_skills,
    get_work_experiences,
)


@dataclass
class DirectMatch:
    matched: bool
    intent: str                # "skill_known" | "worked_at" | "degree" | "school" | "certifications" | "none"
    answer: bool | str | list[str] | None
    evidence_ids: list[str]
    detail: str                # human-readable explanation for the trace


def _all_skill_terms() -> list[tuple[str, str]]:
    """Return [(normalized_term, source_id), ...] over every skill,
    technology, and domain across the profile."""
    out: list[tuple[str, str]] = []
    for category, items in get_skills().items():
        source = f"skills:{category}"
        for s in items:
            out.append((normalize_text(s), source))
    for w in get_work_experiences():
        wid = w.get("id") or ""
        for t in (w.get("technologies") or []):
            out.append((normalize_text(t), wid))
        for d in (w.get("domains") or []):
            out.append((normalize_text(d), wid))
        for s in (w.get("skills_demonstrated") or []):
            out.append((normalize_text(s), wid))
    for p in get_projects():
        pid = p.get("id") or ""
        for t in (p.get("technologies") or []):
            out.append((normalize_text(t), pid))
    return out


_KNOW_PATTERNS = [
    re.compile(r"^\s*(?:do you|have you) (?:know|used?|worked with|have experience with|have experience in|worked in)\s+(.+?)\s*[.?!]*\s*$", re.I),
    re.compile(r"^\s*(?:do you|have you) know\s+(.+?)\s*[.?!]*\s*$", re.I),
    re.compile(r"^\s*(?:have you|has \w+) (?:used|worked with)\s+(.+?)\s*[.?!]*\s*$", re.I),
    re.compile(r"^\s*(?:do you|have you) have\s+(.+?)\s+experience\s*[.?!]*\s*$", re.I),
    re.compile(r"^\s*experience with\s+(.+?)\s*[.?!]*\s*$", re.I),
]

_WORKED_AT_PATTERNS = [
    re.compile(r"^\s*(?:have you|has \w+)\s+worked (?:at|for)\s+(.+?)\s*[.?!]*\s*$", re.I),
    re.compile(r"^\s*(?:did you|has \w+)\s+work (?:at|for)\s+(.+?)\s*[.?!]*\s*$", re.I),
]


def _try_skill(question: str) -> DirectMatch | None:
    for pat in _KNOW_PATTERNS:
        m = pat.match(question)
        if m:
            term = normalize_text(m.group(1))
            if not term:
                return None
            return _skill_check(term)
    return None


def _skill_check(term: str) -> DirectMatch:
    hits: list[str] = []
    for stored, source in _all_skill_terms():
        if not stored:
            continue
        if contains_term(term, stored):
            hits.append(source)
    hits = _dedupe_preserve_order(hits)
    if hits:
        return DirectMatch(
            matched=True, intent="skill_known", answer=True,
            evidence_ids=hits,
            detail=f"'{term}' matched in {len(hits)} profile source(s)",
        )
    return DirectMatch(
        matched=True, intent="skill_known", answer=False,
        evidence_ids=[],
        detail=f"'{term}' not present in skills / technologies / domains",
    )


def _try_worked_at(question: str) -> DirectMatch | None:
    for pat in _WORKED_AT_PATTERNS:
        m = pat.match(question)
        if m:
            company = normalize_text(m.group(1))
            if not company:
                return None
            hits: list[str] = []
            for w in get_work_experiences():
                employer = normalize_text(w.get("employer") or "")
                if not employer:
                    continue
                if company in employer or employer in company:
                    hits.append(w.get("id") or "")
            hits = [h for h in hits if h]
            if hits:
                return DirectMatch(
                    matched=True, intent="worked_at", answer=True,
                    evidence_ids=hits,
                    detail=f"employer match for '{company}'",
                )
            return DirectMatch(
                matched=True, intent="worked_at", answer=False,
                evidence_ids=[],
                detail=f"no employer matches '{company}'",
            )
    return None


def _try_education(question: str) -> DirectMatch | None:
    q = question.lower()
    edus = get_education()
    if not edus:
        return None
    # Master's / PhD negative check runs FIRST so "Do you have a Master's
    # degree?" doesn't fall into the generic "what is your degree?" branch.
    if re.search(r"\b(master'?s|masters|phd|doctorate|doctoral)\b", q):
        wanted = "master" if "master" in q else "phd"
        has = any(wanted in normalize_text(e.get("degree") or "") for e in edus)
        return DirectMatch(
            matched=True, intent="degree", answer=has,
            evidence_ids=[e.get("id") or "" for e in edus] if has else [],
            detail=(f"education contains '{wanted}'" if has else
                    f"education does NOT contain '{wanted}'"),
        )
    if re.search(r"\b(school|university|college|institution)\b", q) and \
       re.search(r"\b(what|which|where)\b", q):
        schools = [e.get("school") for e in edus if e.get("school")]
        return DirectMatch(
            matched=True, intent="school", answer=schools,
            evidence_ids=[e.get("id") or "" for e in edus],
            detail=f"reporting {len(schools)} school(s)",
        )
    if re.search(r"\b(degree|major|field of study|studying)\b", q):
        answer = [f"{e.get('degree','')} in {e.get('field','')}".strip() for e in edus]
        return DirectMatch(
            matched=True, intent="degree", answer=answer,
            evidence_ids=[e.get("id") or "" for e in edus],
            detail=f"reporting {len(answer)} degree(s)",
        )
    return None


def _try_certifications(question: str) -> DirectMatch | None:
    q = question.lower()
    if "certification" not in q and "certified" not in q and "certificate" not in q:
        return None
    certs = get_certifications()
    if re.search(r"\bwhat|which|list|any\b", q):
        return DirectMatch(
            matched=True, intent="certifications", answer=certs,
            evidence_ids=[f"certification:{i}" for i in range(len(certs))],
            detail=f"reporting {len(certs)} certification(s)",
        )
    # "Do you have AWS certification?" — take the noun-ish token as the term.
    m = re.search(r"(?:have|hold|possess)\s+(?:an?\s+|any\s+)?(.+?)\s+certif", q)
    if m:
        term = normalize_text(m.group(1))
        hits = [c for c in certs if term and term in normalize_text(c)]
        return DirectMatch(
            matched=True, intent="certifications", answer=bool(hits),
            evidence_ids=[f"certification:{certs.index(h)}" for h in hits] if hits else [],
            detail=(f"certification containing '{term}' present" if hits else
                    f"no certification mentions '{term}'"),
        )
    return None


def direct_lookup(question: str) -> DirectMatch:
    """Try each cheap pattern in order. Falls through to `intent='none'`
    when nothing structural applies — caller then hands off to retrieval."""
    if not question or not question.strip():
        return DirectMatch(False, "none", None, [], "empty question")

    for candidate in (
        _try_certifications(question),
        _try_worked_at(question),
        _try_education(question),
        _try_skill(question),
    ):
        if candidate is not None:
            return candidate

    return DirectMatch(False, "none", None, [], "no direct pattern matched")


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out
