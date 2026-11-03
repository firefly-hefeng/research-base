"""A useful query revision reaches ordinary navigation without topic hijacking."""
import httpx
import pytest

from research_base.core import ResearchBase
from research_base.discovery_plans import task_discovery_suggestions
from research_base.tools import ToolRegistry


@pytest.mark.parametrize("task,scope", [
    ("为实验室RAG系统设计可诊断的评估", "rag_evaluation"),
    ("Compare retrieval-augmented generation evidence metrics", "rag_evaluation"),
    ("Design single-cell perturbation prediction evaluation", "single_cell_perturbation"),
    ("设计单细胞扰动预测的外推评价", "single_cell_perturbation"),
    ("virtual-cell CRISPR response modeling", "single_cell_perturbation"),
])
def test_explicit_task_scope_matches(task, scope):
    result = task_discovery_suggestions(task, date_from="2025-01-01", date_to="2026-11-03")
    assert result["matched_scope"] == scope and result["plans"]
    assert not result["automatic_search_started"] and not result["freshness_changed"]


@pytest.mark.parametrize("task", ["Protein structure model evaluation", "Image classification calibration",
    "Single-cell immune atlas clustering", "Build a RAG chatbot", "Evaluate protein fragments", "预测一般药物反应"])
def test_unrelated_or_insufficiently_specific_tasks_get_no_template(task):
    result = task_discovery_suggestions(task, date_from="2025-01-01", date_to="2026-11-03")
    assert result["status"] == "no_matching_template" and result["plans"] == []


def test_mixed_topic_is_not_silently_assigned_to_one_scope():
    result = task_discovery_suggestions("RAG evaluation and single-cell perturbation prediction",
        date_from="2025-01-01", date_to="2026-11-03")
    assert result["status"] == "mixed_task" and result["plans"] == []


def test_tool_navigation_exposes_new_plans_without_http_jobs_config_or_freshness_changes(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("Navigation cannot start network"))) as client:
        with ResearchBase(tmp_path / "lib", client=client) as base:
            cid = base.create_collection("Biology", config={"discovery": {"query": "original query",
                "sources": ["pubmed"], "date_from": "2025-01-01", "date_to": "2026-11-03"}})["collection_id"]
            base.configure_frontier(cid, [], profile="single_cell")
            before = base.get_collection(cid)
            old_context = base.prepare_context("Single-cell perturbation prediction", collection_id=cid)
            registry = ToolRegistry(base, collection_id=cid)
            result = registry.call("prepare_research", {"task": "Single-cell perturbation prediction evaluation"})
            assert "data" in result
            nav = result["data"]
            suggestions = nav["discovery_suggestions"]
            assert suggestions["matched_scope"] == "single_cell_perturbation"
            assert {s for p in suggestions["plans"] for s in p["sources"]} == {"pubmed", "europe_pmc", "arxiv", "biorxiv"}
            assert all(p["date_from"] == "2025-01-01" and p["date_to"] == "2026-11-03" for p in suggestions["plans"])
            assert base.get_collection(cid) == before
            assert nav["context"]["freshness"] == old_context["freshness"]
            with base.store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0] == 0
            # A broad profile must not cause RAG or perturbation suggestions.
            unrelated = registry.call("prepare_research", {"task": "Image classification calibration", "profile": "machine_learning"})["data"]
            assert unrelated["discovery_suggestions"]["plans"] == []
            assert "discovery_suggestions" in next(s for s in registry.specs() if s["function"]["name"] == "prepare_research")["function"]["description"]
