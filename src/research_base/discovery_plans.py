"""Reusable, inspectable discovery suggestions, never completed-search claims."""
from __future__ import annotations

from datetime import date
import json
import re
import unicodedata

from .models import ResearchError


def frontier_query_plans(profile: str, *, date_from: str, date_to: str) -> list[dict]:
    """Return ordinary discover_papers arguments for a host-selected scope.

    These recall-oriented plans were revised after known misses. They are
    suggestions to run and screen, not a blind recall benchmark or an assertion
    that every source is available. Date bounds remain explicit in each plan.
    """
    try:
        if date.fromisoformat(date_from) > date.fromisoformat(date_to):
            raise ValueError()
    except (ValueError, TypeError):
        raise ResearchError("INVALID_ARGUMENT", "Discovery suggestions require an ordered explicit ISO date range") from None
    common = {"date_from": date_from, "date_to": date_to, "budget": {"max_requests": 12}}
    if profile in ("rag_evaluation", "rag", "machine_learning", "machine-learning"):
        concepts = '(all:"retrieval augmented generation" OR all:"retrieval-augmented generation" OR ti:RAG OR abs:RAG)'
        terms = ("evaluation", "evaluating", "benchmark", "benchmarking", "factuality", "faithfulness", "calibration",
                 "calibrated", "reranking", "rerankers", "diagnosing", "diagnostic", "evidence", "redundancy", "robustness")
        topic = " OR ".join(f"{field}:{term}" for term in terms for field in ("ti", "abs"))
        years = range(int(date_from[:4]), int(date_to[:4]) + 1)
        collections = [f"{year}.{venue}" for year in years for venue in ("acl", "emnlp", "findings")]
        # EACL is biennial. Unknown/unpublished official collection files must
        # fail visibly rather than being treated as an empty complete source.
        collections += [f"{year}.eacl" for year in years if year % 2 == 0]
        return [{**common, "query": "RAG evaluation, evidence diagnostics, calibration and retrieval robustness",
            "sources": ["arxiv", "acl_anthology"], "source_queries": {
                "arxiv": f"{concepts} AND ({topic})",
                "acl_anthology": json.dumps({"collections": collections,
                    "any": ["retrieval augmented", "retrieval-augmented", "RAG"], "all": []}, separators=(",", ":"))}}]
    if profile in ("single_cell_perturbation", "single_cell", "single-cell", "bio"):
        entities = ["single-cell", "single cell", "virtual cell", "cellular responses", "Perturb-seq"]
        pm_entities = " OR ".join(f'"{term}"[Title/Abstract]' for term in entities)
        ep_entities = " OR ".join(f'TITLE_ABS:"{term}"' for term in entities)
        ax_entities = " OR ".join(f'all:"{term}"' for term in entities)
        return [{**common, "query": "Single-cell and virtual-cell perturbation prediction, evaluation and generalization",
            "sources": ["europe_pmc", "pubmed"], "source_queries": {
                "pubmed": f'({pm_entities}) AND (perturb*[Title/Abstract] OR CRISPR[Title/Abstract]) AND (predict*[Title/Abstract] OR benchmark*[Title/Abstract] OR model*[Title/Abstract] OR evaluat*[Title/Abstract] OR generaliz*[Title/Abstract] OR generalis*[Title/Abstract])',
                "europe_pmc": f'({ep_entities}) AND (TITLE_ABS:perturb* OR TITLE_ABS:CRISPR) AND (TITLE_ABS:predict* OR TITLE_ABS:benchmark* OR TITLE_ABS:model* OR TITLE_ABS:evaluat* OR TITLE_ABS:generaliz* OR TITLE_ABS:generalis*)'}},
            {**common, "query": "Single-cell and virtual-cell perturbation preprints",
             "sources": ["arxiv"], "source_queries": {"arxiv": f'({ax_entities}) AND (all:perturbation OR all:perturbations OR all:CRISPR) AND (all:prediction OR all:predicting OR all:predictive OR all:benchmark OR all:benchmarking OR all:model OR all:models OR all:generalization OR all:generalizing OR all:evaluation)'}},
            {**common, "query": "Official bioRxiv perturbation metadata in declared categories",
             "sources": ["biorxiv"], "source_queries": {"biorxiv": json.dumps({
                 "categories": ["bioinformatics", "genomics", "cell_biology"],
                 "any": ["perturbation", "perturbations", "Perturb-seq", "virtual cell"], "all": []}, separators=(",", ":"))}}]
    raise ResearchError("INVALID_ARGUMENT", "No scoped discovery suggestion exists for this profile")


def task_discovery_suggestions(task: str, *, date_from: str, date_to: str) -> dict:
    """Match task text, never a broad discipline profile, to a known template."""
    if not isinstance(task, str) or not task.strip() or len(task) > 12000:
        raise ResearchError("INVALID_ARGUMENT", "Discovery suggestions require a nonempty task up to 12000 characters")
    text = unicodedata.normalize("NFKC", task).casefold()
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    rag = bool(re.search(r"(?<![a-z0-9_])rag(?![a-z0-9_])|retrieval[- ]augmented generation|检索增强生成", text))
    evaluation = bool(re.search(r"\b(?:evaluat\w*|diagnos\w*|benchmark\w*|metric\w*|calibrat\w*|factual\w*|faithful\w*|evidence|rerank\w*|compar\w*)\b|评估|评价|评测|诊断|基准|指标|校准|忠实|证据|重排序|比较|对照", text))
    cell = bool(re.search(r"single[- ]cell|virtual[- ]cell|perturb-seq|单细胞|虚拟细胞", text))
    perturbation = bool(re.search(r"\bperturb\w*|\bcrispr\b|扰动", text))
    scopes = (["rag_evaluation"] if rag and evaluation else []) + (["single_cell_perturbation"] if cell and perturbation else [])
    scope = scopes[0] if len(scopes) == 1 else None
    return {"matched_scope": scope, "status": "available" if scope else "mixed_task" if scopes else "no_matching_template",
        "plans": frontier_query_plans(scope, date_from=date_from, date_to=date_to) if scope else [],
        "automatic_search_started": False, "freshness_changed": False,
        "notice": "Task-matched query suggestions only. Explicitly choose/adapt and execute each plan, inspect pagination/errors, and screen candidates. Suggestions do not replace the active discovery configuration or certify coverage; other topics need their own research queries."}
