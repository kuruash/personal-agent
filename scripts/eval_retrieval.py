"""Retrieval evaluation harness.

Runs the same case suite through:
  A. vector-only retrieval (server.profile_retrieval.retrieve)
  B. hybrid retrieval     (server.profile_hybrid.retrieve_hybrid)

Reports top-1 / top-3 / top-5 success, latency percentiles, per-case
diffs (improved / regressed), and the raw top-5 for both routes on
questions that changed rank between the two.

Direct-lookup is exercised for negative cases (its FALSE answer is
authoritative for those; retrieval is allowed to surface neighbours
but doesn't affect pass/fail).

Run against a native Ollama instance:

    .venv/bin/python -m scripts.eval_retrieval

Set FORCE_REBUILD=1 to rebuild the Chroma index before running.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from server.domain.profile.direct_lookup import direct_lookup
from server.domain.profile.indexer import documents_by_type
from server.domain.profile.repository import profile_summary
from server.retrieval.hybrid import hybrid_weights, retrieve_hybrid
from server.retrieval.lexical import index_stats as lex_stats
from server.retrieval.vector import build_index, retrieve as vector_retrieve


@dataclass
class Case:
    question: str
    kind: str                        # "exact" | "semantic" | "negative" | "multi" | "adversarial"
    expected_any: tuple[str, ...] = ()
    expected_none: tuple[str, ...] = ()
    note: str = ""


BASE_CASES: list[Case] = [
    # --- direct / exact positive ---
    Case("Do you know Python?", "exact",
         expected_any=("skills:programming_languages", "inroads_ai_engineer")),
    Case("Have you used Kubernetes?", "exact",
         expected_any=("skills:devops", "aigen_technologies_swe_intern",
                       "inferfleet_distributed_inference")),
    Case("Do you have AWS experience?", "exact",
         expected_any=("skills:cloud", "subscription_service_backend")),
    Case("What school do you attend?", "exact",
         expected_any=("gmu_bs_cs",)),
    Case("What is your degree?", "exact",
         expected_any=("gmu_bs_cs",)),
    Case("Have you worked at InRoads?", "exact",
         expected_any=("inroads_ai_engineer",)),
    Case("Do you know Go?", "exact",
         expected_any=("skills:programming_languages",
                       "inferfleet_distributed_inference",
                       "subscription_service_backend")),
    Case("Have you used Docker?", "exact",
         expected_any=("skills:devops", "aigen_technologies_swe_intern",
                       "inferfleet_distributed_inference")),
    Case("What certifications do you have?", "exact"),
    Case("Do you know PyTorch?", "exact",
         expected_any=("skills:machine_learning",
                       "inferfleet_distributed_inference")),

    # --- semantic positive ---
    Case("Have you reduced AI inference costs?", "semantic",
         expected_any=("inroads_ai_engineer", "nl_to_sql_bi_engine")),
    Case("Have you improved latency?", "semantic",
         expected_any=("aigen_technologies_swe_intern",
                       "inferfleet_distributed_inference")),
    Case("Have you built distributed systems?", "semantic",
         expected_any=("inferfleet_distributed_inference",
                       "subscription_service_backend",
                       "rise_consultancy_swe_intern",
                       "skills:other")),
    Case("Have you worked with observability?", "semantic",
         expected_any=("aigen_technologies_swe_intern",
                       "inferfleet_distributed_inference",
                       "skills:devops")),
    Case("Have you built distributed ETL pipelines?", "semantic",
         expected_any=("rise_consultancy_swe_intern",
                       "distributed_etl_ml_pipeline")),
    Case("Do you have experience with CI/CD?", "semantic",
         expected_any=("skills:devops", "aigen_technologies_swe_intern",
                       "inroads_ai_engineer", "rise_consultancy_swe_intern")),
    Case("Have you built microservices?", "semantic",
         expected_any=("aigen_technologies_swe_intern", "skills:backend")),
    Case("Have you worked with RAG?", "semantic",
         expected_any=("skills:ai_llm", "lastminute_ai_exam_prep")),
    Case("Have you used Spark?", "semantic",
         expected_any=("rise_consultancy_swe_intern",
                       "distributed_etl_ml_pipeline", "skills:other")),
    Case("Have you built monitoring dashboards?", "semantic",
         expected_any=("aigen_technologies_swe_intern",
                       "rise_consultancy_swe_intern",
                       "inferfleet_distributed_inference")),
    Case("Do you have experience with cloud infrastructure?", "semantic",
         expected_any=("skills:cloud", "aigen_technologies_swe_intern",
                       "moveos_relocation_assistant")),
    Case("Have you used message queues?", "semantic",
         expected_any=("subscription_service_backend",
                       "inferfleet_distributed_inference")),
    Case("Have you built LLM pipelines?", "semantic",
         expected_any=("inroads_ai_engineer", "nl_to_sql_bi_engine",
                       "lastminute_ai_exam_prep")),
    Case("Do you have experience with vector databases?", "semantic",
         expected_any=("skills:databases", "skills:ai_llm",
                       "lastminute_ai_exam_prep")),
    Case("Have you worked on multi-agent systems?", "semantic",
         expected_any=("moveos_relocation_assistant", "lastminute_ai_exam_prep")),
    Case("Tell me about your AI experience.", "semantic",
         expected_any=("inroads_ai_engineer", "nl_to_sql_bi_engine",
                       "lastminute_ai_exam_prep", "skills:ai_llm")),
    Case("Have you improved reliability of production systems?", "semantic",
         expected_any=("aigen_technologies_swe_intern",
                       "inferfleet_distributed_inference",
                       "rise_consultancy_swe_intern")),

    # --- negative ---
    Case("Have you used RabbitMQ?", "negative",
         expected_none=("subscription_service_backend",
                        "inferfleet_distributed_inference")),
    Case("Do you have a Master's degree?", "negative",
         expected_none=("gmu_bs_cs",)),
    Case("Have you worked at Google?", "negative"),
    Case("Do you have a PhD?", "negative"),
    Case("Have you worked at Amazon?", "negative"),

    # --- multi-source ---
    Case("Have you used PostgreSQL?", "multi",
         expected_any=("skills:databases",
                       "inferfleet_distributed_inference",
                       "subscription_service_backend")),
    Case("Have you used Prometheus?", "multi",
         expected_any=("skills:devops",
                       "aigen_technologies_swe_intern",
                       "inferfleet_distributed_inference")),
    Case("Have you worked on NL-to-SQL?", "multi",
         expected_any=("inroads_ai_engineer", "nl_to_sql_bi_engine",
                       "skills:ai_llm")),
]

# --- adversarial cases (added in milestone 5) ---
# These questions are chosen to stress the LEXICAL/METADATA channels
# against near-neighbour distractors. Each expected set names the docs
# that literally mention the named technology.
ADVERSARIAL_CASES: list[Case] = [
    Case("Have you built ETL pipelines?", "adversarial",
         expected_any=("distributed_etl_ml_pipeline",
                       "rise_consultancy_swe_intern")),
    Case("Have you used AWS SQS?", "adversarial",
         expected_any=("subscription_service_backend", "skills:cloud")),
    Case("Have you built Grafana dashboards?", "adversarial",
         expected_any=("aigen_technologies_swe_intern",
                       "inferfleet_distributed_inference",
                       "skills:devops")),
    Case("Have you used ChromaDB?", "adversarial",
         expected_any=("skills:databases", "lastminute_ai_exam_prep")),
    Case("Have you used Redis?", "adversarial",
         expected_any=("skills:databases",
                       "subscription_service_backend",
                       "inferfleet_distributed_inference")),
    Case("Have you used GitHub Actions?", "adversarial",
         expected_any=("skills:devops", "aigen_technologies_swe_intern")),
    Case("Have you used gRPC?", "adversarial",
         expected_any=("skills:backend", "inferfleet_distributed_inference")),
    Case("Have you used FastAPI?", "adversarial",
         expected_any=("skills:backend",
                       "inferfleet_distributed_inference",
                       "moveos_relocation_assistant")),
    Case("Have you used ONNX?", "adversarial",
         expected_any=("skills:machine_learning",
                       "inferfleet_distributed_inference")),
    Case("Have you used LangGraph?", "adversarial",
         expected_any=("skills:ai_llm", "lastminute_ai_exam_prep")),
]

ALL_CASES: list[Case] = BASE_CASES + ADVERSARIAL_CASES


# ---------- pass logic ----------

def _case_passes(case: Case, top_ids: list[str], direct_answer_false: bool) -> tuple[bool, str]:
    """Same rules the milestone-4 harness used, applied to whatever
    top-N is passed in. `top_ids` is expected to be top-3."""
    if case.kind == "negative":
        if direct_answer_false:
            return True, ""
        bad = [x for x in top_ids if x in case.expected_none]
        if bad:
            return False, f"unexpected id(s) in top-3: {bad}"
        return True, ""
    if not case.expected_any:
        return True, ""
    if any(x in top_ids for x in case.expected_any):
        return True, ""
    return False, (f"none of expected {list(case.expected_any)} "
                   f"in top-3 {top_ids}")


# ---------- percentile helper (no scipy/numpy dep) ----------

def _pct(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    i = int(round((len(sorted_values) - 1) * p))
    return sorted_values[i]


# ---------- eval body ----------

@dataclass
class RunResult:
    label: str
    passed_top3: int = 0
    top1_hits: int = 0
    top3_hits: int = 0
    top5_hits: int = 0
    negative_ok: int = 0
    negative_total: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    fails: list[tuple[Case, list[str], str]] = field(default_factory=list)
    top3_by_case: dict[str, list[str]] = field(default_factory=dict)


def _hits_at_k(case: Case, top_ids: list[str]) -> bool:
    if case.kind == "negative":
        return not any(x in top_ids for x in case.expected_none)
    if not case.expected_any:
        return True
    return any(x in top_ids for x in case.expected_any)


async def _run_vector(cases: list[Case]) -> RunResult:
    r = RunResult(label="vector-only")
    for case in cases:
        t = time.perf_counter()
        hits = await vector_retrieve(case.question, top_k=5)
        r.latencies_ms.append((time.perf_counter() - t) * 1000)
        ids = [h["id"] for h in hits]
        r.top3_by_case[case.question] = ids[:3]

        direct = direct_lookup(case.question)
        direct_false = direct.matched and direct.answer is False

        if _hits_at_k(case, ids[:1]): r.top1_hits += 1
        if _hits_at_k(case, ids[:3]): r.top3_hits += 1
        if _hits_at_k(case, ids[:5]): r.top5_hits += 1

        if case.kind == "negative":
            r.negative_total += 1
            if direct_false or not any(x in ids[:3] for x in case.expected_none):
                r.negative_ok += 1

        ok, why = _case_passes(case, ids[:3], direct_false)
        if ok:
            r.passed_top3 += 1
        else:
            r.fails.append((case, ids[:5], why))
    return r


async def _run_hybrid(cases: list[Case]) -> RunResult:
    r = RunResult(label="hybrid")
    for case in cases:
        t = time.perf_counter()
        hits = await retrieve_hybrid(case.question, top_k=5)
        r.latencies_ms.append((time.perf_counter() - t) * 1000)
        ids = [h["id"] for h in hits]
        r.top3_by_case[case.question] = ids[:3]

        direct = direct_lookup(case.question)
        direct_false = direct.matched and direct.answer is False

        if _hits_at_k(case, ids[:1]): r.top1_hits += 1
        if _hits_at_k(case, ids[:3]): r.top3_hits += 1
        if _hits_at_k(case, ids[:5]): r.top5_hits += 1

        if case.kind == "negative":
            r.negative_total += 1
            if direct_false or not any(x in ids[:3] for x in case.expected_none):
                r.negative_ok += 1

        ok, why = _case_passes(case, ids[:3], direct_false)
        if ok:
            r.passed_top3 += 1
        else:
            r.fails.append((case, ids[:5], why))
    return r


def _print_summary(r: RunResult, total: int) -> None:
    lat_sorted = sorted(r.latencies_ms)
    print(f"  {r.label}")
    print(f"    passed (top-3 rule): {r.passed_top3}/{total}")
    print(f"    top-1 hits:          {r.top1_hits}/{total} "
          f"({r.top1_hits/total*100:.0f}%)")
    print(f"    top-3 hits:          {r.top3_hits}/{total} "
          f"({r.top3_hits/total*100:.0f}%)")
    print(f"    top-5 hits:          {r.top5_hits}/{total} "
          f"({r.top5_hits/total*100:.0f}%)")
    if r.negative_total:
        print(f"    negative correct:    {r.negative_ok}/{r.negative_total}")
    if lat_sorted:
        print(f"    latency (ms):  avg={sum(lat_sorted)/len(lat_sorted):.1f}  "
              f"p50={_pct(lat_sorted, 0.50):.1f}  "
              f"p95={_pct(lat_sorted, 0.95):.1f}  "
              f"max={lat_sorted[-1]:.1f}")


async def main() -> None:
    print(f"Profile summary: {profile_summary()}")
    print(f"Documents by type: {documents_by_type()}")
    print(f"BM25 index:      {lex_stats()}")
    print(f"Hybrid weights:  {hybrid_weights()}")

    stats = await build_index(force=bool(os.environ.get("FORCE_REBUILD")))
    print(f"Vector index build: {stats}")
    print()

    print(f"Running {len(ALL_CASES)} cases "
          f"({len(BASE_CASES)} base + {len(ADVERSARIAL_CASES)} adversarial)...")
    print()

    vec = await _run_vector(ALL_CASES)
    hyb = await _run_hybrid(ALL_CASES)

    print("=" * 68)
    print("RESULTS")
    print("=" * 68)
    _print_summary(vec, len(ALL_CASES))
    print()
    _print_summary(hyb, len(ALL_CASES))
    print()

    # ---- diffs ----
    improved: list[tuple[Case, list[str], list[str]]] = []
    regressed: list[tuple[Case, list[str], list[str]]] = []
    for case in ALL_CASES:
        v_top3 = vec.top3_by_case[case.question]
        h_top3 = hyb.top3_by_case[case.question]
        direct = direct_lookup(case.question)
        direct_false = direct.matched and direct.answer is False
        v_ok, _ = _case_passes(case, v_top3, direct_false)
        h_ok, _ = _case_passes(case, h_top3, direct_false)
        if h_ok and not v_ok:
            improved.append((case, v_top3, h_top3))
        elif v_ok and not h_ok:
            regressed.append((case, v_top3, h_top3))

    print(f"Cases improved by hybrid:  {len(improved)}")
    for c, v, h in improved:
        print(f"  [{c.kind}] {c.question}")
        print(f"    vec top-3: {v}")
        print(f"    hyb top-3: {h}")
    print()

    print(f"Cases regressed by hybrid: {len(regressed)}")
    for c, v, h in regressed:
        print(f"  [{c.kind}] {c.question}")
        print(f"    vec top-3: {v}")
        print(f"    hyb top-3: {h}")
    print()

    # ---- remaining failures ----
    print(f"Vector-only remaining failures: {len(vec.fails)}")
    for c, ids, why in vec.fails:
        print(f"  [{c.kind}] {c.question} — top-5={ids}")
        print(f"    {why}")
    print()
    print(f"Hybrid remaining failures: {len(hyb.fails)}")
    for c, ids, why in hyb.fails:
        print(f"  [{c.kind}] {c.question} — top-5={ids}")
        print(f"    {why}")


if __name__ == "__main__":
    asyncio.run(main())
