"""Observable original reading, scoped tasks, and continuation integrity."""
from pathlib import Path
import hashlib

import httpx
import pytest

from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.storage import now
from research_base.usage import ResearchUsage


class OfflineSources:
    def list_sources(self):
        return []


@pytest.fixture
def research(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        with ResearchBase(tmp_path / "library", source_registry=OfflineSources(), client=client) as core:
            collection = core.create_collection("Retrieval evaluation")
            papers, materials = [], []
            for index in range(3):
                paper = core.add_paper({"source": "offline", "source_id": str(index),
                    "title": f"Retrieval evaluation method {index}", "authors": ["Researcher"],
                    "doi": f"10.5555/evaluation.{index}"}, collection_id=collection["collection_id"])
                path = tmp_path / f"original-{index}.txt"
                path.write_text("Method: hold out independent queries. Metric: success at k. Limit: one corpus. " * 3,
                                encoding="utf-8")
                material = core.import_material(paper["paper_id"], path=path, confirmed=True)
                papers.append(paper["paper_id"])
                materials.append(material["material_id"])
            core.configure_frontier(collection["collection_id"], papers[:1], profile="machine_learning")
            yield core, collection["collection_id"], papers, materials


def test_paths_and_navigation_do_not_count_as_original_reading(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    started = usage.begin("retrieval evaluation", collection)
    sid = started["session_id"]
    assert not started["usage"]["ready"]
    assert started["preparation"]["frontier"]["papers"][0]["materials"]
    original = usage.read(sid, materials[0], view="original")
    assert Path(original["material"]["local_path"]).is_file()
    assert not usage.check(sid)["ready"]
    returned = usage.read(sid, materials[0], pages=[1])
    assert returned["pages"][0]["text"].startswith("Method:")
    checked = usage.check(sid, used_paper_ids=[papers[0]])
    assert checked["ready"]
    assert checked["status"] == "ready_to_synthesize"
    assert checked["reads"][0]["ranges"][0]["page"] == 1
    assert "text" not in checked["reads"][0]
    assert checked["freshness"]["needs_refresh"]
    assert any("freshness" in item for item in checked["limitations"])


def test_all_explicit_focus_and_actually_used_papers_require_reads(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection, focus_paper_ids=papers[:2])["session_id"]
    usage.read(sid, materials[0])
    checked = usage.check(sid, used_paper_ids=[papers[2]])
    assert not checked["ready"]
    assert checked["missing_focus_paper_ids"] == papers[1:]
    usage.read(sid, materials[1])
    assert not usage.check(sid)["ready"]  # the declared third paper persists
    usage.read(sid, materials[2])
    assert usage.check(sid)["ready"]
    assert not usage.check(sid, used_paper_ids=["invented-paper"])["ready"]
    assert not usage.check(sid)["ready"]


def test_continuation_replays_and_offset_jumps_cannot_skip_an_unread_gap(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection, focus_paper_ids=papers[:1])["session_id"]
    first = usage.read(sid, materials[0], pages=[1], max_chars=20)
    assert first["continuation"]["session_id"] == sid
    usage.read(sid, materials[0], pages=[1], char_offset=180, max_chars=10000)
    pending = usage.check(sid)["pending_continuations"]
    assert pending[0]["char_offset"] == 20
    assert not usage.check(sid)["ready"]
    while not usage.check(sid)["ready"]:
        continuation = usage.check(sid)["pending_continuations"][0]
        usage.read(**continuation)
    assert usage.check(sid)["ready"]


def test_starting_at_nonzero_offset_does_not_claim_the_selected_range_was_read(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection)["session_id"]
    usage.read(sid, materials[0], char_offset=100)
    checked = usage.check(sid)
    assert not checked["ready"]
    assert checked["pending_continuations"][0]["char_offset"] == 0
    usage.read(**checked["pending_continuations"][0])
    assert usage.check(sid)["ready"]


def test_actual_complete_page_read_clears_earlier_selection_of_same_source(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection)["session_id"]
    usage.read(sid, materials[0], pages=[1], max_chars=10)
    usage.read(sid, materials[0])
    checked = usage.check(sid)
    assert checked["ready"]
    assert not checked["pending_continuations"]


def test_reselecting_already_delivered_page_does_not_create_false_prefix_gap(research, tmp_path):
    from test_materials import pdf
    core, collection, papers, _ = research
    path = tmp_path / "three-pages.pdf"
    pdf(path, ["First method page " * 8, "Second metric page " * 20, "Last limitation page " * 8])
    material = core.import_material(papers[0], path=path, confirmed=True)["material_id"]
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection)["session_id"]
    first = usage.read(sid, material, pages=[1, 2, 3], max_chars=180)
    while first["continuation"]:
        first = usage.read(**first["continuation"])
    assert usage.check(sid)["ready"]
    usage.read(sid, material, pages=[2], char_offset=100, max_chars=10000)
    checked = usage.check(sid)
    assert checked["ready"] and not checked["pending_continuations"]


def test_cross_selection_coverage_does_not_skip_a_genuinely_unread_page_gap(research, tmp_path):
    from test_materials import pdf
    core, collection, papers, _ = research
    path = tmp_path / "gap-pages.pdf"
    pdf(path, ["Method page " * 8, "Metric page " * 30])
    material = core.import_material(papers[0], path=path, confirmed=True)["material_id"]
    first_page_chars = len(core.read_material(material, pages=[1])["pages"][0]["text"])
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection)["session_id"]
    usage.read(sid, material, pages=[1, 2], max_chars=first_page_chars + 10)
    usage.read(sid, material, pages=[2], char_offset=100, max_chars=10000)
    checked = usage.check(sid)
    assert not checked["ready"]
    assert any(item["pages"] == [2] and item["char_offset"] == 10 for item in checked["pending_continuations"])
    while checked["pending_continuations"]:
        usage.read(**checked["pending_continuations"][0])
        checked = usage.check(sid)
    assert checked["ready"]


def test_changed_version_cannot_inherit_completed_page_coverage(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection)["session_id"]
    usage.read(sid, materials[0], pages=[1])
    assert usage.check(sid)["ready"]
    with core.store.connect() as db:
        db.execute("INSERT INTO versions(id,paper_id,kind,metadata) VALUES(?,?,?,?)", ("version-new-reading-test", papers[0], "accepted", "{}"))
        db.execute("UPDATE materials SET version_id=? WHERE id=?", ("version-new-reading-test", materials[0]))
    usage.read(sid, materials[0], pages=[1], char_offset=100)
    checked = usage.check(sid)
    assert not checked["ready"]
    assert checked["pending_continuations"][0]["char_offset"] == 0
    usage.read(**checked["pending_continuations"][0])
    assert usage.check(sid)["ready"]


def test_adjacent_context_omits_unpermitted_neighbors_without_reading_them(research, tmp_path, monkeypatch):
    from test_materials import pdf
    from research_base.tools import ToolRegistry
    core, collection, papers, _ = research
    path = tmp_path / "restricted-neighbors.pdf"
    pdf(path, ["Unpermitted prior page", "Permitted definition page", "Unpermitted next page"])
    material = core.import_material(papers[0], path=path, confirmed=True)["material_id"]
    original = core.read_material
    accessed = []
    def restricted_read(material_id, **kwargs):
        assert kwargs.get("pages") == [2]
        accessed.append(kwargs.get("view", "text"))
        result = original(material_id, **kwargs)
        result["material"]["permitted_pages"] = [2]
        return result
    monkeypatch.setattr(core, "read_material", restricted_read)
    registry = ToolRegistry(core, collection_id=collection)
    sid = registry.research.begin("retrieval", collection)["session_id"]
    result = registry.call("read_research_context", {"session_id": sid, "material_id": material, "page": 2})["data"]
    assert result["context_selection"]["selected_pages"] == [2]
    assert result["context_selection"]["omitted_neighbors"] == [1, 3]
    assert accessed == ["original", "text"]
    assert [part["page"] for part in result["pages"]] == [2]


def test_candidates_staged_and_empty_materials_cannot_satisfy_usage(research, tmp_path):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection)["session_id"]
    path = tmp_path / "unconfirmed.txt"
    path.write_text("An unconfirmed original.", encoding="utf-8")
    staged = core.import_material(papers[0], path=path)
    assert staged["status"] == "staged"
    usage.read(sid, staged["material_id"])
    assert not usage.check(sid)["ready"]
    assert not usage.observe_read(sid, {"material": core.list_materials(paper_id=papers[0])[0], "pages": []})
    core.set_membership(collection, [papers[2]], status="candidate")
    with pytest.raises(ResearchError, match="included task-relevant"):
        usage.read(sid, materials[2])
    assert not usage.check(sid)["ready"]


def test_verified_linked_edition_material_counts_for_included_focus(research):
    core, collection, papers, materials = research
    # An official provider relation retains both stable IDs and material owner.
    with core.store.connect() as db:
        db.execute("INSERT INTO paper_relations VALUES(?,?,?,?)", (papers[1], papers[0], "official", now()))
    core.set_membership(collection, [papers[1]], status="candidate")
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection, focus_paper_ids=[papers[0]])["session_id"]
    returned = usage.read(sid, materials[1])
    assert returned["material"]["paper_id"] == papers[1]
    assert usage.check(sid, used_paper_ids=[papers[0]])["ready"]
    assert usage.check(sid, used_paper_ids=[papers[1]])["ready"]


def test_excluding_a_paper_after_reading_invalidates_its_usage(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection, focus_paper_ids=[papers[0]])["session_id"]
    usage.read(sid, materials[0])
    assert usage.check(sid)["ready"]
    core.set_membership(collection, papers[:1], status="excluded")
    assert not usage.check(sid)["ready"]


def test_reclassifying_material_outside_original_roles_invalidates_read_credit(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection, focus_paper_ids=papers[:1])["session_id"]
    usage.read(sid, materials[0])
    assert usage.check(sid)["ready"]
    core.confirm_material_identity(materials[0], role="other")
    assert not usage.check(sid)["ready"]


def test_task_extension_keeps_focus_and_rejects_atomic_invalid_extension(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("unmatched task", collection, focus_paper_ids=papers[:1])["session_id"]
    extended = usage.extend(sid, papers[1:2])
    assert extended["added_paper_ids"] == papers[1:2]
    usage.read(sid, materials[1])
    assert usage.check(sid)["missing_focus_paper_ids"] == papers[:1]
    core.set_membership(collection, papers[2:], status="excluded")
    with pytest.raises(ResearchError, match="included papers"):
        usage.extend(sid, [papers[2]])
    assert usage.check(sid)["missing_focus_paper_ids"] == papers[:1]


def test_sessions_are_connection_local_bounded_and_independent(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core, max_sessions=2, max_reads=1)
    sid = usage.begin("retrieval", collection)["session_id"]
    other = usage.begin("retrieval", collection)["session_id"]
    usage.read(sid, materials[0])
    assert usage.check(sid)["ready"]
    assert not usage.check(other)["ready"]
    with pytest.raises(ResearchError) as error:
        ResearchUsage(core).check(sid)
    assert error.value.code == "RESEARCH_SESSION_NOT_FOUND"
    with pytest.raises(ResearchError) as error:
        usage.begin("retrieval", collection)
    assert error.value.code == "RESEARCH_SESSION_LIMIT"
    with pytest.raises(ResearchError) as error:
        usage.read(sid, materials[1])
    assert error.value.code == "RESEARCH_READ_LIMIT"
    usage.end(other)
    with pytest.raises(ResearchError) as error:
        usage.check(other)
    assert error.value.code == "RESEARCH_SESSION_NOT_FOUND"
    assert usage.begin("retrieval", collection)["session_id"] != other


def test_novelty_requires_current_search_and_rechecks_without_discarding_reads(research, monkeypatch):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection, stage="novelty", focus_paper_ids=papers[:1])["session_id"]
    usage.read(sid, materials[0])
    checked = usage.check(sid)
    assert not checked["ready"]
    assert not checked["current_frontier_ready"]
    assert not checked["missing_focus_paper_ids"]
    original_prepare = core.prepare_context

    def refreshed(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        result["freshness"] = {**result["freshness"], "needs_refresh": False, "reason": None}
        return result

    monkeypatch.setattr(core, "prepare_context", refreshed)
    checked = usage.check(sid)
    assert checked["ready"]
    assert checked["current_frontier_ready"]
    assert checked["read_paper_ids"] == papers[:1]


def test_trusted_visual_callback_tracks_selected_pages_without_text_credit(research):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection, focus_paper_ids=papers[:1])["session_id"]
    usage.read(sid, materials[0], view="original")
    assert not usage.check(sid)["ready"]
    # The renderer and vision transport are trusted host responsibilities; this
    # unit test exercises their callback against the actual material catalog.
    image_digest = hashlib.sha256(b"test host rendered page image").hexdigest()
    assert usage.observe_visual_read(sid, materials[0], pages=[1], image_sha256=[image_digest.upper()])
    checked = usage.check(sid, used_paper_ids=papers[:1])
    assert checked["ready"]
    receipt = checked["reads"][0]
    assert receipt["source"] == "host_observed_visual"
    assert receipt["physical_pages"] == [1]
    assert receipt["image_sha256"] == [image_digest]
    assert receipt["returned_chars"] == 0
    assert receipt["ranges"] == []


@pytest.mark.parametrize("pages,digests", [
    ([], []), ([True], ["a" * 64]), ([0], ["a" * 64]),
    ([1], []), ([1], ["not-a-sha256"]), ([1], ["g" * 64]),
    ([1, 1], ["a" * 64, "a" * 64]), ([2], ["a" * 64]),
])
def test_visual_callback_rejects_invalid_or_nonexistent_page_receipts(research, pages, digests):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("retrieval", collection)["session_id"]
    with pytest.raises(ResearchError) as error:
        usage.observe_visual_read(sid, materials[0], pages=pages, image_sha256=digests)
    assert error.value.code == "INVALID_ARGUMENT"
    assert not usage.check(sid)["ready"]


def test_visual_callback_rejects_outside_scope_and_unconfirmed_material(research, tmp_path):
    core, collection, papers, materials = research
    usage = ResearchUsage(core)
    sid = usage.begin("unmatched task", collection, focus_paper_ids=papers[:1])["session_id"]
    with pytest.raises(ResearchError) as error:
        usage.observe_visual_read(sid, materials[2], pages=[1], image_sha256=["a" * 64])
    assert error.value.code == "MATERIAL_OUTSIDE_RESEARCH_SCOPE"
    path = tmp_path / "staged-visual.txt"
    path.write_text("Unconfirmed original", encoding="utf-8")
    staged = core.import_material(papers[0], path=path)
    with pytest.raises(ResearchError) as error:
        usage.observe_visual_read(sid, staged["material_id"], pages=[1], image_sha256=["a" * 64])
    assert error.value.code == "MATERIAL_NOT_VERIFIED"
    assert not usage.check(sid)["ready"]


def test_project_binding_is_preserved_for_live_session_freshness(research, tmp_path, monkeypatch):
    core, collection, papers, materials = research
    project = tmp_path / "bound-project"
    (project / ".research").mkdir(parents=True)
    (project / ".research" / "config.yaml").write_text(
        f"schema_version: 1\nlibrary_id: {core.library_id}\ncollection_id: {collection}\n"
        "discovery:\n  query: project-specific-retrieval\n", encoding="utf-8")
    calls = []
    original = core.prepare_context

    def observed(*args, **kwargs):
        calls.append(kwargs.copy())
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "prepare_context", observed)
    usage = ResearchUsage(core)
    opened = usage.begin("retrieval", collection, project_root=project)
    assert opened["preparation"]["project_root"] == str(project.resolve())
    usage.read(opened["session_id"], materials[0])
    usage.check(opened["session_id"])
    assert calls[-1]["project_root"] == str(project.resolve())
    assert calls[-1]["collection_id"] == collection
