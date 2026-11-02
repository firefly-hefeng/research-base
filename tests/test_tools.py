from research_base.models import ResearchError
from research_base.tools import ToolRegistry


class FakeCore:
    def __init__(self):
        self.calls = []

    def list_sources(self):
        self.calls.append(("list_sources", {}))
        return [{"name": "openalex"}]

    def search_library(self, **arguments):
        self.calls.append(("search_library", arguments))
        return {"items": []}

    def list_materials(self, **arguments):
        self.calls.append(("list_materials", arguments))
        return [{"material_id": "mat_1", "path": "C:/Library/paper.pdf"}]

    def list_papers(self, **arguments):
        self.calls.append(("list_papers", arguments))
        return {"papers": [{"paper_id": "p1", "title": "Example paper"}], "total": 2, "next_offset": 1}

    def set_tags(self, **arguments):
        self.calls.append(("set_tags", arguments))
        return {"tags": arguments["tags"]}

    def discover_papers(self, **arguments):
        self.calls.append(("discover_papers", arguments))
        return {"job_id": "job_1"}

    def discover_frontier(self, **arguments):
        self.calls.append(("discover_frontier", arguments))
        return {"job_id": "frontier_1", "state": "queued"}

    def expand_citations(self, **arguments):
        self.calls.append(("expand_citations", arguments))
        return {"job_id": "citations_1", "state": "queued"}

    def retry_acquisition(self, **arguments):
        self.calls.append(("retry_acquisition", arguments))
        return {"job_id": arguments["job_id"], "state": "queued"}

    def get_paper(self, **arguments):
        raise ResearchError("NOT_FOUND", "Paper not found")


def test_default_registry_is_read_only_and_explicit():
    core = FakeCore()
    registry = ToolRegistry(core)
    assert registry.call("list_sources")["data"][0]["name"] == "openalex"
    assert registry.call("discover_papers", {"collection_id": "c", "query": "q"})["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert registry.call("__getattribute__", {"name": "_api_key"})["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert len(core.calls) == 1


def test_validation_rejects_unknown_arguments_without_invoking_core():
    core = FakeCore()
    result = ToolRegistry(core).call("search_library", {"query": "evaluation", "shell": "anything"})
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert core.calls == []


def test_schema_bounds_reject_bool_as_integer_and_long_lists():
    registry = ToolRegistry(FakeCore())
    assert registry.call("search_library", {"query": "q", "limit": True})["error"]["code"] == "INVALID_ARGUMENT"
    assert registry.call("search_library", {"query": "q", "limit": 101})["error"]["code"] == "INVALID_ARGUMENT"
    assert registry.call("read_material", {"material_id": "m", "pages": [0]})["error"]["code"] == "INVALID_ARGUMENT"
    assert registry.call("list_sources", ["not-object"])["error"]["code"] == "INVALID_ARGUMENT"


def test_missing_fields_and_enums_are_validated():
    registry = ToolRegistry(FakeCore())
    assert registry.call("read_material", {})["error"]["code"] == "INVALID_ARGUMENT"
    assert registry.call("read_material", {"material_id": "m", "view": "execute"})["error"]["code"] == "INVALID_ARGUMENT"


def test_write_permission_does_not_grant_local_export_or_browser_control():
    core = FakeCore()
    registry = ToolRegistry(core, allow_writes=True)
    result = registry.call("discover_papers", {"collection_id": "c", "query": "q", "source_queries": {"crossref": "query"}})
    assert result["data"]["job_id"] == "job_1"
    assert registry.call("export_collection", {"collection_id": "c", "output_dir": "C:/"})["error"]["code"] == "TOOL_NOT_ALLOWED"
    names = {item["function"]["name"] for item in registry.specs()}
    assert "claim_browser_task" not in names
    assert "import_material" not in names


def test_material_paths_are_preserved_for_host_agents():
    result = ToolRegistry(FakeCore()).call("list_materials", {"paper_id": "p", "include_paths": True})
    assert result["data"][0]["path"] == "C:/Library/paper.pdf"


def test_read_only_agent_can_browse_papers_without_constructing_query():
    core = FakeCore()
    registry = ToolRegistry(core)
    result = registry.call("list_papers", {"collection_id": "c1", "membership": "candidate", "limit": 1, "offset": 0})
    assert result["data"]["papers"][0]["paper_id"] == "p1"
    assert result["data"]["total"] == 2
    assert result["data"]["next_offset"] == 1
    assert core.calls == [("list_papers", {"collection_id": "c1", "membership": "candidate", "limit": 1, "offset": 0})]
    assert registry.call("list_papers", {})["data"]["total"] == 2
    assert registry.call("list_papers", {"limit": 0})["error"]["code"] == "INVALID_ARGUMENT"


def test_error_envelope_preserves_expected_codes():
    result = ToolRegistry(FakeCore()).call("get_paper", {"paper_id": "missing"})
    assert result == {"schema_version": 1, "error": {"code": "NOT_FOUND", "message": "Paper not found", "retryable": False, "details": {}}}


def test_unexpected_exception_does_not_leak_internal_message():
    class BrokenCore:
        def list_sources(self):
            raise RuntimeError("sensitive-internal-text")
    result = ToolRegistry(BrokenCore()).call("list_sources")
    assert result["error"]["code"] == "INTERNAL_ERROR"
    assert "sensitive-internal-text" not in str(result)


def test_specs_are_copies_and_local_tools_require_both_permissions():
    registry = ToolRegistry(FakeCore())
    specs = registry.specs()
    specs.clear()
    assert registry.specs()
    assert "export_collection" not in {x["function"]["name"] for x in ToolRegistry(FakeCore(), local_admin=True).specs()}
    assert "export_collection" in {x["function"]["name"] for x in ToolRegistry(FakeCore(), allow_writes=True, local_admin=True).specs()}


def test_tags_and_bibliography_import_have_distinct_permissions():
    core = FakeCore()
    read_only = ToolRegistry(core)
    writes = ToolRegistry(core, allow_writes=True)
    assert read_only.call("list_papers", {"tag": "evaluation"})["data"]["total"] == 2
    assert read_only.call("set_tags", {"paper_id": "p1", "tags": ["evaluation"]})["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert writes.call("set_tags", {"paper_id": "p1", "tags": ["evaluation"]})["data"]["tags"] == ["evaluation"]
    assert writes.call("import_bibliography", {"path": "C:/private.json", "collection_id": "c1"})["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert "import_bibliography" in {item["function"]["name"] for item in ToolRegistry(core, allow_writes=True, local_admin=True).specs()}


def test_bound_collection_is_default_scope_without_mutating_arguments():
    core = FakeCore()
    registry = ToolRegistry(core, collection_id="bound")
    arguments = {}
    registry.call("list_materials", arguments)
    registry.call("list_papers", {})
    registry.call("search_library", {"query": "method"})
    assert [call[1]["collection_id"] for call in core.calls] == ["bound", "bound", "bound"]
    assert arguments == {}
    registry.call("list_materials", {"collection_id": "explicit"})
    assert core.calls[-1][1]["collection_id"] == "explicit"
    ToolRegistry(core).call("list_materials", {})
    assert core.calls[-1][1] == {}


def test_frontier_and_graph_are_write_tools_with_structured_seed_arguments():
    core = FakeCore()
    read_only = ToolRegistry(core)
    writes = ToolRegistry(core, allow_writes=True)
    args = {"collection_id": "c1", "query": "retrieval evaluation", "seeds": [
        {"doi": "10.5555/example", "title": "Example paper"}, {"arxiv": "2408.08067"}],
        "budget": {"max_requests": 10, "broad_requests": 4}}
    assert read_only.call("discover_frontier", args)["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert writes.call("discover_frontier", args)["data"]["job_id"] == "frontier_1"
    assert core.calls[-1] == ("discover_frontier", args)
    graph = {"collection_id": "c1", "paper_ids": ["p1"], "direction": "both", "date_from": "2025-01-01"}
    assert writes.call("expand_citations", graph)["data"]["job_id"] == "citations_1"
    assert core.calls[-1] == ("expand_citations", graph)


def test_frontier_rejects_file_paths_empty_or_unidentified_seeds_without_core_calls():
    core = FakeCore()
    registry = ToolRegistry(core, allow_writes=True)
    for seeds in ("C:/private/seeds.json", [], [{}], [{"path": "C:/private/file"}], [{"title": ""}], [{"doi": True}]):
        result = registry.call("discover_frontier", {"collection_id": "c", "query": "q", "seeds": seeds})
        assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert core.calls == []


def test_citation_and_retry_options_have_bounded_validated_schema():
    core = FakeCore()
    registry = ToolRegistry(core, allow_writes=True)
    for args in ({"paper_ids": []}, {"paper_ids": ["p"], "direction": "all-hops"},
                 {"paper_ids": ["p"], "budget": {"max_requests": 0}}):
        response = registry.call("expand_citations", {"collection_id": "c", **args})
        assert response["error"]["code"] == "INVALID_ARGUMENT"
    assert registry.call("retry_acquisition", {"job_id": "j", "refresh_locations": "yes"})["error"]["code"] == "INVALID_ARGUMENT"
    assert ToolRegistry(core).call("retry_acquisition", {"job_id": "j"})["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert registry.call("retry_acquisition", {"job_id": "j", "refresh_locations": True})["data"]["job_id"] == "j"
