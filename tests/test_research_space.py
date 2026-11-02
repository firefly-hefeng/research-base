from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.storage import now


class OfflineSources:
    def list_sources(self):
        return [{"name": "offline"}]

    def search_page(self, *args, **kwargs):
        raise AssertionError("Research navigation must not perform network discovery")


@pytest.fixture
def base(tmp_path):
    def offline(request):
        raise AssertionError("Research navigation must not perform network acquisition")
    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        with ResearchBase(tmp_path / "library", source_registry=OfflineSources(), client=client) as library:
            yield library


def add(base, cid, source_id, title, status="included"):
    return base.add_paper({"source": "offline", "source_id": source_id, "title": title, "year": 2026}, cid, status=status)["paper_id"]


def test_curated_anchors_survive_task_search_miss_and_expose_original_paths(base, tmp_path):
    cid = base.create_collection("Retrieval", config={"discovery": {"query": "retrieval", "sources": ["offline"]}})["collection_id"]
    anchor = add(base, cid, "anchor", "An essential evaluation baseline")
    raw = tmp_path / "baseline.txt"
    raw.write_text("Read exact assumptions and control settings here.", encoding="utf-8")
    material = base.import_material(anchor, path=raw, confirmed=True)
    base.configure_frontier(cid, [anchor], profile="machine_learning")
    result = base.prepare_research("termwithnomatches", cid, stage="design")
    assert result["context"]["papers"] == []
    assert result["frontier"]["paper_ids"] == [anchor]
    entry = result["frontier"]["papers"][0]
    assert entry["materials"][0]["material_id"] == material["material_id"]
    assert Path(entry["materials"][0]["local_path"]).is_absolute()
    assert Path(entry["materials"][0]["local_path"]).read_text(encoding="utf-8") == raw.read_text(encoding="utf-8")
    assert entry["eligible"] and entry["text_searchable"]
    assert result["profile"] == "machine_learning"
    assert any("train/validation/test" in question for question in result["reading_questions"])
    assert result["frontier"]["freshness"]["needs_refresh"]
    assert not result["focus_paper_ids"]


def test_frontier_configuration_preserves_other_defaults_and_is_idempotent(base):
    config = {"discovery": {"query": "original"}, "context": {"max_papers": 3}}
    cid = base.create_collection("Cell biology", config=config)["collection_id"]
    anchor = add(base, cid, "cell", "Perturbation model")
    one = base.configure_frontier(cid, [anchor, anchor], profile="single_cell")
    before = base.get_collection(cid)
    two = base.configure_frontier(cid, [anchor], profile="single_cell")
    after = base.get_collection(cid)
    assert one == two
    assert before["revision"] == after["revision"]
    assert after["config"]["discovery"] == config["discovery"]
    assert after["config"]["context"] == config["context"]
    assert after["config"]["frontier"]["paper_ids"] == [anchor]
    assert "frontier" not in config


def test_anchor_later_excluded_stays_visible_as_a_gap(base):
    cid = base.create_collection("Research")["collection_id"]
    anchor = add(base, cid, "one", "Essential paper")
    base.configure_frontier(cid, [anchor])
    base.set_membership(cid, [anchor], "excluded")
    frontier = base.get_frontier(cid)
    assert frontier["papers"][0]["membership"] == "excluded"
    assert not frontier["papers"][0]["eligible"]
    assert {"paper_id": anchor, "reason": "frontier_not_included", "membership": "excluded"} in frontier["gaps"]
    with pytest.raises(ResearchError, match="included papers"):
        base.prepare_research("essential", cid, focus_paper_ids=[anchor])
    assert base.prepare_research("essential", cid)["context"]["papers"] == []


def test_configure_and_focus_reject_foreign_candidate_or_unknown_ids(base):
    cid = base.create_collection("One")["collection_id"]
    other = base.create_collection("Two")["collection_id"]
    foreign = add(base, other, "foreign", "Other research")
    candidate = add(base, cid, "candidate", "Candidate research", status="candidate")
    before = base.get_collection(cid)
    for pid in (foreign, candidate, "paper_unknown"):
        with pytest.raises(ResearchError) as error:
            base.configure_frontier(cid, [pid])
        assert error.value.code == "INVALID_ARGUMENT"
        with pytest.raises(ResearchError):
            base.prepare_research("research", cid, focus_paper_ids=[pid])
    assert base.get_collection(cid) == before


def test_focus_materials_are_accessible_even_if_no_task_keyword_matches(base):
    cid = base.create_collection("Research")["collection_id"]
    pid = add(base, cid, "reference", "Independent noise controls")
    result = base.prepare_research("unrelatedquery", cid, stage="interpret", profile="single_cell", focus_paper_ids=[pid])
    assert [paper["paper_id"] for paper in result["focus_papers"]] == [pid]
    assert result["focus_paper_ids"] == [pid]
    assert result["context"]["papers"] == []
    assert any("biological replicates" in question for question in result["reading_questions"])
    assert any("effect sizes" in question for question in result["reading_questions"])
    assert base.get_collection(cid)["config"] == {}


@pytest.mark.parametrize("stage", ["explore", "design", "implement", "interpret", "novelty"])
def test_stage_guidance_is_explicit_and_does_not_mark_materials_read(base, stage):
    cid = base.create_collection("Research")["collection_id"]
    result = base.prepare_research("research", cid, stage=stage)
    assert result["stage"] == stage
    assert result["reading_questions"]
    assert result["frontier"]["configured"] is False
    assert result["warnings"]
    assert "read" not in result


@pytest.mark.parametrize("arguments", [{"stage": "unknown"}, {"profile": "unknown"}, {"focus_paper_ids": "paper"}, {"focus_paper_ids": [""]}])
def test_invalid_prepare_arguments_do_not_silently_change_behavior(base, arguments):
    cid = base.create_collection("Research")["collection_id"]
    with pytest.raises(ResearchError) as error:
        base.prepare_research("research", cid, **arguments)
    assert error.value.code == "INVALID_ARGUMENT"


def test_project_research_uses_local_search_scope_and_freshness_defaults(base, tmp_path):
    shared_plan = {"query": "shared research", "sources": ["offline"]}
    project_plan = {"query": "project-specific research", "sources": ["offline"]}
    cid = base.create_collection("Research", config={"discovery": shared_plan, "freshness": {"max_age_days": 30}})["collection_id"]
    project = tmp_path / "research-project"
    (project / ".research").mkdir(parents=True)
    (project / ".research/config.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "library_id": base.library_id, "collection_id": cid,
        "discovery": project_plan, "freshness": {"max_age_days": 1},
    }), encoding="utf-8")
    completed = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with base.store.connect() as db:
        for plan in (shared_plan, project_plan):
            db.execute("INSERT INTO search_runs VALUES(?,?,?,?)", (cid, base._plan_fingerprint(plan), completed, now()))
    shared = base.prepare_research("research", cid)
    local = base.prepare_research("research", cid, project_root=project)
    assert "project_root" not in shared
    assert local["project_root"] == str(project.resolve())
    assert shared["context"]["freshness"]["needs_refresh"] is False
    assert local["context"]["freshness"]["reason"] == "stale"
    assert local["frontier"]["freshness"] == local["context"]["freshness"]
    assert local["context"]["freshness"]["plan_fingerprint"] == base._plan_fingerprint(project_plan)
    assert shared["context"]["freshness"]["plan_fingerprint"] == base._plan_fingerprint(shared_plan)
    assert base.get_collection(cid)["config"]["discovery"] == shared_plan


@pytest.mark.parametrize("conflict", ["library", "collection"])
def test_project_research_rejects_mismatched_binding(base, tmp_path, conflict):
    cid = base.create_collection("Research")["collection_id"]
    project = tmp_path / "project"
    (project / ".research").mkdir(parents=True)
    (project / ".research/config.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "library_id": "another-library" if conflict == "library" else base.library_id,
        "collection_id": "another-collection" if conflict == "collection" else cid,
    }), encoding="utf-8")
    with pytest.raises(ResearchError) as error:
        base.prepare_research("research", cid, project_root=project)
    assert error.value.code == ("LIBRARY_ID_MISMATCH" if conflict == "library" else "INVALID_ARGUMENT")
