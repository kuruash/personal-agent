"""Interactive REPL for the profile QA pipeline.

Usage (from repo root):
    .venv/bin/python -m scripts.ask_profile

Type a question, hit enter. You'll see:
  - route (DIRECT_VALUE / DIRECT_BOOLEAN / SEARCH / REASON)
  - direct-lookup result (if any)
  - top-5 hybrid retrieval hits (id + score + snippet)
  - the final answer the form-fill path would produce

Prefix a question with `?` to skip retrieval and only print routing +
direct lookup (fast, no Ollama call).

Commands:
  :profile      show what's in the current profile
  :stats        show ChromaDB / BM25 stats
  :reindex      rebuild the Chroma index from profile.json
  :quit         exit

Requires:
  - native Ollama running at 127.0.0.1:11434
  - qwen2.5:7b and nomic-embed-text pulled
  - profile.json present at server/domain/profile/profile.json
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from server.domain.profile.direct_lookup import direct_lookup
from server.domain.profile.repository import profile_summary
from server.retrieval.hybrid import retrieve_hybrid
from server.retrieval.lexical import index_stats as bm25_stats
from server.retrieval.vector import build_index, index_stats as chroma_stats
from server.workflows.form_answering import Route, answer_form, route_field


C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_CYAN = "\033[36m"
C_RED = "\033[31m"

def bold(s): return f"{C_BOLD}{s}{C_RESET}"
def dim(s): return f"{C_DIM}{s}{C_RESET}"
def green(s): return f"{C_GREEN}{s}{C_RESET}"
def yellow(s): return f"{C_YELLOW}{s}{C_RESET}"
def cyan(s): return f"{C_CYAN}{s}{C_RESET}"
def red(s): return f"{C_RED}{s}{C_RESET}"


def _short(s: str, n: int = 110) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


async def _handle(question: str, retrieve_only: bool) -> None:
    # Build a synthetic field the same way form-fill would see it, so the
    # router / answerer paths are exercised end-to-end.
    field = {"label": question, "type": "text", "options": []}

    # 1. Routing
    t = time.perf_counter()
    r, extra = route_field(field)
    print(f"  route:        {bold(r.value)}  {dim(str(extra) if extra else '')}"
          f"  {dim(f'({(time.perf_counter()-t)*1000:.1f} ms)')}")

    # 2. Direct lookup (always try, even for other routes — informative)
    t = time.perf_counter()
    dm = direct_lookup(question)
    if dm.matched:
        print(f"  direct:       intent={cyan(dm.intent)}  answer={green(str(dm.answer)[:80])}"
              f"  evidence={dm.evidence_ids[:4]}"
              f"  {dim(f'({(time.perf_counter()-t)*1000:.1f} ms)')}")
    else:
        print(f"  direct:       {dim('no direct pattern matched')}")

    # 3. Hybrid retrieval — top 5
    if not retrieve_only or r in (Route.SEARCH, Route.REASON):
        t = time.perf_counter()
        try:
            hits = await retrieve_hybrid(question, top_k=5)
        except Exception as e:
            print(f"  {red('hybrid retrieval failed:')} {e}")
            hits = []
        print(f"  hybrid top-5: {dim(f'({(time.perf_counter()-t)*1000:.1f} ms)')}")
        for i, h in enumerate(hits, 1):
            print(f"    {i}. {bold(h['id'][:40]):45s} "
                  f"hyb={h['hybrid_score']:.3f} "
                  f"vec={h['vector_score']:.3f} lex={h['lexical_score']:.3f} "
                  f"meta={h['metadata_score']:.3f}")
            print(f"       {dim(_short(h.get('text', ''), 100))}")

    if retrieve_only:
        return

    # 4. Full form-fill pipeline against this single field — shows what
    #    the panel would receive (state, value, source).
    print(dim("  running full answer_form for this one field..."))
    t = time.perf_counter()
    result = await answer_form([field])
    elapsed_ms = (time.perf_counter() - t) * 1000
    f0 = result["fields"][0]
    color = green if f0["state"] == "ready" else yellow
    print(f"  answer:       state={color(f0['state'])}  "
          f"value={green(repr(f0['value']))}")
    print(f"  source:       {dim(f0.get('source', ''))}")
    print(f"  counts:       {result['counts']}  {dim(f'({elapsed_ms:.0f} ms)')}")


def _cmd_profile() -> None:
    from server.domain.profile.repository import (
        get_certifications, get_education, get_projects,
        get_publications, get_references, get_skills, get_work_experiences,
    )
    print(bold("Profile summary:"), profile_summary())
    print(f"  work_experiences: {[w.get('id') for w in get_work_experiences()]}")
    print(f"  projects:         {[p.get('id') for p in get_projects()]}")
    print(f"  education:        {[e.get('id') for e in get_education()]}")
    print(f"  skill categories: {list(get_skills().keys())}")
    print(f"  certifications:   {get_certifications()}")
    print(f"  publications:     {len(get_publications())}")
    print(f"  references:       {len(get_references())}")


def _cmd_stats() -> None:
    print(bold("Chroma:"), chroma_stats())
    print(bold("BM25:  "), bm25_stats())


async def _cmd_reindex() -> None:
    print(dim("rebuilding Chroma index (embeds every doc via Ollama)…"))
    t = time.perf_counter()
    stats = await build_index(force=True)
    print(f"  {green('done')}  {stats}  {dim(f'({(time.perf_counter()-t)*1000:.0f} ms)')}")


HELP = """
Type a question. Commands:
  ?<question>   routing + retrieval only (no Qwen call)
  :profile      show what's in the profile
  :stats        show index stats
  :reindex      rebuild Chroma from profile.json
  :help         this text
  :quit         exit
"""


async def main() -> None:
    print(bold("Personal Agent — profile QA REPL"))
    print(dim("uses server.profile_form_qa directly; no FastAPI, no browser."))
    _cmd_stats()
    print(HELP)

    while True:
        try:
            raw = input(bold("ask> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        if raw in (":quit", ":q", ":exit"):
            return
        if raw in (":help", "?"):
            print(HELP); continue
        if raw == ":profile":
            _cmd_profile(); continue
        if raw == ":stats":
            _cmd_stats(); continue
        if raw == ":reindex":
            await _cmd_reindex(); continue
        retrieve_only = raw.startswith("?")
        if retrieve_only:
            raw = raw[1:].strip()
            if not raw:
                continue
        try:
            await _handle(raw, retrieve_only=retrieve_only)
        except Exception as e:
            print(red(f"error: {e}"))
        print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
