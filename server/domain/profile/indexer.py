"""Normalize profile.json into semantic retrieval documents.

One document per natural entity: work_experience, project, education,
skill_group (per category), certification. No fixed-size chunking — the
JSON already carries the boundaries.

Every document is:

    {
        "id":       "<stable id from profile.json where possible>",
        "type":     "work_experience" | "project" | "education" |
                    "skill_group" | "certification",
        "text":     "prose paragraph optimized for embedding",
        "metadata": { ... structured fields for filtering / display ... }
    }

Metadata always includes:
    "doc_type" (mirrors "type", so Chroma metadata filters can find it)
    "source"   (e.g. "collections.work_experiences[inroads_ai_engineer]")

Provenance for overlapping records
----------------------------------
Some projects mirror achievements from a work experience (e.g. the
`nl_to_sql_bi_engine` project overlaps with `inroads_ai_engineer`).
We do NOT dedupe or drop them — they are legitimately different
angles. Instead, `metadata.related_ids` links overlaps so retrieval /
reranking can down-weight duplicate signal if desired later.
"""

from __future__ import annotations

from .repository import (
    get_certifications,
    get_education,
    get_projects,
    get_skills,
    get_work_experiences,
)


def _fmt_date_range(start: str | None, end: str | None, current: bool = False) -> str:
    if current or (start and not end):
        return f"{start or '?'} to present"
    if start and end:
        return f"{start} to {end}"
    return start or end or "date unknown"


def _join_nonempty(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def _work_document(exp: dict) -> dict:
    dates = _fmt_date_range(exp.get("start_date"), exp.get("end_date"), bool(exp.get("current")))
    title = exp.get("title") or ""
    employer = exp.get("employer") or ""
    summary = exp.get("summary") or ""
    resp = " ".join(exp.get("responsibilities") or [])
    ach = " ".join(exp.get("achievements") or [])
    techs = exp.get("technologies") or []
    tech_line = f"Technologies: {', '.join(techs)}." if techs else ""
    skills_line = ""
    if exp.get("skills_demonstrated"):
        skills_line = f"Skills demonstrated: {', '.join(exp['skills_demonstrated'])}."
    domain_line = ""
    if exp.get("domains"):
        domain_line = f"Domains: {', '.join(exp['domains'])}."

    text = _join_nonempty(
        f"{title} at {employer} ({dates}).",
        summary,
        resp,
        ach,
        tech_line,
        skills_line,
        domain_line,
    )
    return {
        "id": exp.get("id") or f"work_{employer.lower().replace(' ', '_')}",
        "type": "work_experience",
        "text": text,
        "metadata": {
            "doc_type": "work_experience",
            "source": f"collections.work_experiences[{exp.get('id')}]",
            "employer": employer,
            "title": title,
            "current": bool(exp.get("current")),
            "start_date": exp.get("start_date") or "",
            "end_date": exp.get("end_date") or "",
            "location": exp.get("location") or "",
            "employment_type": exp.get("employment_type") or "",
            "technologies": list(techs),
            "domains": list(exp.get("domains") or []),
        },
    }


def _project_document(proj: dict, work_by_summary: dict[str, str]) -> dict:
    dates = _fmt_date_range(proj.get("start_date"), proj.get("end_date"))
    name = proj.get("name") or proj.get("id") or "Unnamed project"
    desc = proj.get("description") or ""
    highlights = " ".join(proj.get("highlights") or [])
    techs = proj.get("technologies") or []
    tech_line = f"Technologies: {', '.join(techs)}." if techs else ""
    text = _join_nonempty(f"{name} ({dates}).", desc, highlights, tech_line)

    # Overlap detection: if any word-set overlap between project name/desc
    # and a work experience summary is strong, link the related work id.
    related = _find_related_work(proj, work_by_summary)

    return {
        "id": proj.get("id") or _slugify(name),
        "type": "project",
        "text": text,
        "metadata": {
            "doc_type": "project",
            "source": f"collections.projects[{proj.get('id')}]",
            "name": name,
            "start_date": proj.get("start_date") or "",
            "end_date": proj.get("end_date") or "",
            "technologies": list(techs),
            "related_ids": related,
        },
    }


def _education_document(edu: dict) -> dict:
    dates = _fmt_date_range(edu.get("start_date"), edu.get("end_date"))
    school = edu.get("school") or ""
    degree = edu.get("degree") or ""
    field = edu.get("field") or ""
    gpa = edu.get("gpa")
    gpa_line = f"GPA: {gpa}." if gpa else ""
    coursework = ", ".join(edu.get("coursework") or [])
    cw_line = f"Coursework: {coursework}." if coursework else ""
    activities = ", ".join(edu.get("activities") or [])
    act_line = f"Activities: {activities}." if activities else ""
    ach = ", ".join(edu.get("achievements") or [])
    ach_line = f"Achievements: {ach}." if ach else ""

    text = _join_nonempty(
        f"{degree} in {field} at {school} ({dates}).",
        gpa_line,
        cw_line,
        act_line,
        ach_line,
    )
    return {
        "id": edu.get("id") or _slugify(f"{school} {degree}"),
        "type": "education",
        "text": text,
        "metadata": {
            "doc_type": "education",
            "source": f"collections.education_history[{edu.get('id')}]",
            "school": school,
            "degree": degree,
            "field": field,
            "start_date": edu.get("start_date") or "",
            "end_date": edu.get("end_date") or "",
            "gpa": str(gpa) if gpa is not None else "",
            "coursework": list(edu.get("coursework") or []),
        },
    }


# Human-readable labels so embeddings pick up on "cloud", "backend" etc.
_SKILL_CATEGORY_LABELS = {
    "programming_languages": "Programming languages",
    "frontend": "Frontend technologies",
    "machine_learning": "Machine learning tools",
    "ai_llm": "AI, LLM, and RAG tools",
    "backend": "Backend and API technologies",
    "databases": "Databases and data stores",
    "cloud": "Cloud platforms",
    "devops": "DevOps, CI/CD, and observability",
    "other": "Other technical skills",
}


def _skill_documents(skills: dict[str, list[str]]) -> list[dict]:
    docs: list[dict] = []
    for category, items in skills.items():
        if not items:
            continue
        label = _SKILL_CATEGORY_LABELS.get(category, category.replace("_", " ").title())
        text = f"{label}: {', '.join(items)}."
        docs.append({
            "id": f"skills:{category}",
            "type": "skill_group",
            "text": text,
            "metadata": {
                "doc_type": "skill_group",
                "source": f"collections.skills.{category}",
                "category": category,
                "label": label,
                "items": list(items),
            },
        })
    return docs


def _certification_document(cert: str, index: int) -> dict:
    return {
        "id": f"certification:{_slugify(cert)[:60]}",
        "type": "certification",
        "text": f"Certification: {cert}.",
        "metadata": {
            "doc_type": "certification",
            "source": f"collections.certifications[{index}]",
            "name": cert,
        },
    }


def build_documents() -> list[dict]:
    """Materialize the full document set from the currently-loaded profile."""
    works = get_work_experiences()
    # Build a quick lookup {work_id: summary} used for project overlap detection.
    work_by_summary = {w.get("id", ""): (w.get("summary") or "") for w in works}

    docs: list[dict] = []
    docs.extend(_work_document(w) for w in works)
    docs.extend(_project_document(p, work_by_summary) for p in get_projects())
    docs.extend(_education_document(e) for e in get_education())
    docs.extend(_skill_documents(get_skills()))
    docs.extend(_certification_document(c, i) for i, c in enumerate(get_certifications()))
    return docs


def documents_by_type(docs: list[dict] | None = None) -> dict[str, int]:
    docs = docs or build_documents()
    out: dict[str, int] = {}
    for d in docs:
        out[d["type"]] = out.get(d["type"], 0) + 1
    return out


# ---------- helpers ----------

def _slugify(text: str) -> str:
    out = []
    for ch in text.lower().strip():
        out.append(ch if ch.isalnum() else "_")
    return "_".join("".join(out).split("_")).strip("_") or "doc"


def _find_related_work(project: dict, work_by_summary: dict[str, str]) -> list[str]:
    """Cheap word-overlap heuristic — flag a work experience as related
    when the project's tokens strongly appear in the work summary. Not
    used for scoring; just annotation so downstream code can spot
    duplicated evidence."""
    proj_text = " ".join([
        project.get("name") or "",
        project.get("description") or "",
        " ".join(project.get("highlights") or []),
    ]).lower()
    if not proj_text.strip():
        return []
    proj_tokens = {w for w in _words(proj_text) if len(w) > 4}
    if not proj_tokens:
        return []

    related: list[tuple[str, float]] = []
    for wid, wsummary in work_by_summary.items():
        if not wsummary:
            continue
        w_tokens = {w for w in _words(wsummary.lower()) if len(w) > 4}
        if not w_tokens:
            continue
        overlap = len(proj_tokens & w_tokens)
        # Small denominator — Jaccard on the smaller set catches even
        # short work summaries that share several distinctive terms.
        smallest = min(len(proj_tokens), len(w_tokens))
        score = overlap / smallest if smallest else 0.0
        if overlap >= 3 and score >= 0.25:
            related.append((wid, score))
    related.sort(key=lambda x: x[1], reverse=True)
    return [wid for wid, _ in related]


def _words(text: str) -> list[str]:
    out = []
    buf = []
    for ch in text.lower():
        if ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out
