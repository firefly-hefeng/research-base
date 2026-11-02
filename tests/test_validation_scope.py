"""An approved two-paper validation cannot expand its outbound original text."""
import json

import pytest

from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.tools import ToolRegistry
from scripts.selected_originals import SelectedOriginals


@pytest.fixture
def scoped(tmp_path):
    with ResearchBase(tmp_path / "lib") as base:
        collection = base.create_collection("Evaluation")["collection_id"]
        papers, targets = [], []
        for index in range(3):
            paper = base.add_paper({"source": "manual", "source_id": str(index),
                "title": f"Evaluation methods {index}", "abstract": f"ABSTRACT-{index}"}, collection)
            path = tmp_path / f"paper-{index}.txt"
            path.write_text(f"ORIGINAL-{index} Evaluation methods", encoding="utf-8")
            material = base.import_material(paper["paper_id"], path=path, confirmed=True)
            papers.append(paper["paper_id"])
            targets.append({"paper_id": paper["paper_id"], "material_id": material["material_id"], "pages": [1]})
        base.configure_frontier(collection, papers)
        yield SelectedOriginals(base, collection, targets[:2]), collection, papers, targets


def test_navigation_and_metadata_never_send_original_excerpts(scoped):
    core, collection, papers, targets = scoped
    prepared = core.prepare_research("Evaluation", collection, focus_paper_ids=papers[:2])
    searched = core.search_library("Evaluation")
    listed = core.list_papers()
    for result in (prepared, searched, listed, core.get_paper(papers[0])):
        encoded = json.dumps(result)
        assert all(f"ORIGINAL-{index}" not in encoded and f"ABSTRACT-{index}" not in encoded for index in range(3))
        assert papers[2] not in encoded


@pytest.mark.parametrize("kind", ["unselected_paper", "unselected_material", "implicit_pages", "unselected_page"])
def test_unapproved_originals_and_page_selections_are_rejected(scoped, kind):
    core, collection, papers, targets = scoped
    with pytest.raises(ResearchError):
        if kind == "unselected_paper":
            core.get_paper(papers[2])
        elif kind == "unselected_material":
            core.read_material(targets[2]["material_id"], pages=[1])
        else:
            core.read_material(targets[0]["material_id"], pages=None if kind == "implicit_pages" else [2])


def test_scoped_originals_still_support_actual_session_reads_and_continuation(scoped):
    core, collection, papers, targets = scoped
    tools = ToolRegistry(core, collection_id=collection)
    sid = tools.call("begin_research", {"task": "Evaluation", "focus_paper_ids": papers[:2]})["data"]["session_id"]
    for target in targets[:2]:
        result = tools.call("read_research_material", {"session_id": sid,
            "material_id": target["material_id"], "pages": [1], "max_chars": 8})["data"]
        assert result["pages"]
        while result["continuation"]:
            result = tools.call("read_research_material", result["continuation"])["data"]
    checked = tools.require_research_ready(sid, used_paper_ids=papers[:2])
    assert checked["ready"]
    assert checked["read_paper_ids"] == sorted(papers[:2])
