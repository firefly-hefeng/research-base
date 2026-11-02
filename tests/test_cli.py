import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from research_base.cli import app
from research_base.models import ResearchError


runner = CliRunner()


@pytest.fixture(autouse=True)
def do_not_load_local_credentials(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)


class FakeBase:
    library_id = "lib-test"
    instances = []

    def __init__(self, path):
        self.path = path
        self.closed = False
        self.instances.append(self)

    def close(self):
        self.closed = True

    def list_sources(self):
        return [{"name": "openalex"}]

    def create_collection(self, name, description=""):
        return {"collection_id": "col-test", "name": name, "description": description}

    def list_collections(self):
        return [{"collection_id": "col-test"}]

    def discover_frontier(self, collection_id, query, seeds, **kwargs):
        return {"job_id": "frontier-test", "collection_id": collection_id, "query": query, "seeds": seeds, **kwargs}

    def expand_citations(self, collection_id, paper_ids, **kwargs):
        return {"job_id": "citations-test", "collection_id": collection_id, "paper_ids": paper_ids, **kwargs}

    def retry_acquisition(self, job_id, refresh_locations=True):
        return {"job_id": job_id, "refresh_locations": refresh_locations}

    def list_materials(self, paper_id=None, collection_id=None, include_paths=True):
        return [{"material_id": "mat-test", "local_path": "C:/library/article.pdf" if include_paths else None}]

    def list_papers(self, collection_id=None, membership="included", limit=100, offset=0, tag=None):
        return {"papers": [], "total": 0, "next_offset": None, "collection_id": collection_id,
                "membership": membership, "limit": limit, "offset": offset, "tag": tag}

    def search_library(self, query, **kwargs):
        raise ResearchError("NOT_FOUND", "Test search failure")


def install_fake(monkeypatch):
    monkeypatch.setitem(sys.modules, "research_base.core", SimpleNamespace(ResearchBase=FakeBase))


def test_help_and_source_listing(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "material" in help_result.output
    result = runner.invoke(app, ["--library", str(tmp_path / "library"), "--json", "sources", "list"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == [{"name": "openalex"}]


def test_create_collection_and_material_paths(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    result = runner.invoke(app, ["collection", "create", "--name", "示例方向"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["name"] == "示例方向"
    result = runner.invoke(app, ["material", "list", "--paper", "paper-test"])
    assert result.exit_code == 0
    assert json.loads(result.output)["data"][0]["local_path"].endswith("article.pdf")


def test_domain_error_has_json_envelope(monkeypatch):
    install_fake(monkeypatch)
    result = runner.invoke(app, ["search", "test"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "NOT_FOUND"
    assert FakeBase.instances[-1].closed is True


def test_attach_dry_run_and_real_binding(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    library = tmp_path / "library"
    library.mkdir()
    (library / "library.json").write_text('{}', encoding="utf-8")
    result = runner.invoke(app, ["--library", str(library), "attach", "--project", str(tmp_path), "--collection", "col-test", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "AGENTS.md").exists()
    result = runner.invoke(app, ["--library", str(library), "attach", "--project", str(tmp_path), "--collection", "col-test"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").exists()
    result = runner.invoke(app, ["detach", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "AGENTS.md").exists()


def test_detach_requires_explicit_project():
    assert runner.invoke(app, ["detach"]).exit_code == 2


def test_attach_preview_does_not_initialize_a_missing_library(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    library = tmp_path / "missing-library"
    result = runner.invoke(app, ["--library", str(library), "attach", "--project", str(tmp_path),
                                 "--collection", "col-test", "--dry-run"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "LIBRARY_UNAVAILABLE"
    assert not library.exists()
    assert not (tmp_path / ".research").exists()


def test_clients_close_on_normal_command_completion(monkeypatch):
    install_fake(monkeypatch)
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert FakeBase.instances[-1].closed is True


def test_empty_library_environment_uses_safe_default(monkeypatch):
    install_fake(monkeypatch)
    monkeypatch.setenv("RESEARCH_BASE_LIBRARY", "")
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert FakeBase.instances[-1].path == Path(".research-base/library")


def test_paper_list_exposes_collection_and_pagination(monkeypatch):
    install_fake(monkeypatch)
    result = runner.invoke(app, ["paper", "list", "--collection", "col-test", "--membership", "candidate",
                                 "--limit", "7", "--offset", "14", "--tag", "baseline"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["collection_id"] == "col-test" and payload["membership"] == "candidate"
    assert payload["limit"] == 7 and payload["offset"] == 14
    assert payload["tag"] == "baseline"


def test_agent_step_limit_matches_core():
    result = runner.invoke(app, ["agent", "run", "Test task", "--max-steps", "33"])
    assert result.exit_code == 2


def test_doctor_is_read_only_for_missing_library_and_binding(tmp_path):
    library = tmp_path / "not-initialized"
    result = runner.invoke(app, ["--library", str(library), "doctor", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["bound"] is False
    assert payload["data"]["library_matches"] is False
    assert not library.exists()
    assert not (tmp_path / ".research").exists()


def test_real_cli_core_material_paths_export_and_project_binding(tmp_path):
    library = tmp_path / "library"
    def command(*args):
        result = runner.invoke(app, ["--library", str(library), "--json", *args])
        assert result.exit_code == 0, (args, result.output, result.exception)
        response = json.loads(result.output)
        assert response["schema_version"] == 1
        assert "error" not in response
        return response["data"]

    initialized = command("library", "init")
    assert initialized["library_id"]
    collection = command("collection", "create", "--name", "Retrieval experiments")
    collection_id = collection["collection_id"]
    defaults = tmp_path / "collection-defaults.json"
    defaults.write_text(json.dumps({"discovery": {"sources": ["openalex"], "query": "retrieval"}}), encoding="utf-8")
    updated = command("collection", "update", "--collection", collection_id, "--config", str(defaults))
    assert updated["config"]["discovery"]["query"] == "retrieval"

    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"source": "manual", "source_id": "cli-fixture", "title": "Controlled Retrieval Evaluation",
                                    "authors": ["Test Author"], "year": 2026, "version_kind": "published",
                                    "abstract": "Evaluating retrieval quality with reproducible experiments."}), encoding="utf-8")
    paper = command("paper", "add", "--metadata", str(metadata), "--collection", collection_id)
    paper_id = paper["paper_id"]
    listed = command("paper", "list", "--collection", collection_id, "--limit", "1")
    assert listed["total"] == 1 and listed["papers"][0]["paper_id"] == paper_id
    original = tmp_path / "methods.txt"
    original.write_text("Controlled retrieval quality evaluation and experiment settings.", encoding="utf-8")
    imported = command("material", "import", "--paper", paper_id, "--file", str(original), "--role", "supplement")
    assert imported["status"] == "available"
    path = Path(imported["local_path"])
    assert path.is_absolute() and path.read_bytes() == original.read_bytes()
    linked = command("material", "import", "--paper", paper_id, "--url", "https://code.example/experiment", "--role", "code")
    assert linked["status"] == "external"

    materials = command("material", "list", "--paper", paper_id)
    assert any(item.get("local_path") == str(path) for item in materials)
    text = command("material", "read", imported["material_id"], "--page", "1")
    assert "retrieval quality" in text["pages"][0]["text"]
    first_part = command("material", "read", imported["material_id"], "--page", "1", "--max-chars", "10")
    next_part = command("material", "read", imported["material_id"], "--page", "1", "--max-chars", "10", "--char-offset", "10")
    assert first_part["next_char_offset"] == 10
    assert first_part["pages"][0]["text"] + next_part["pages"][0]["text"] == original.read_text(encoding="utf-8")[:20]
    view = command("material", "read", imported["material_id"], "--view", "original")
    assert view["material"]["local_path"] == str(path)
    search = command("search", "retrieval", "--collection", collection_id)
    assert search["papers"][0]["paper_id"] == paper_id

    exported_dir = tmp_path / "exported-collection"
    exported = command("collection", "export", "--collection", collection_id, "--output", str(exported_dir), "--format", "bundle")
    assert exported["portable"] is True
    manifest = json.loads(Path(exported["manifest_path"]).read_text(encoding="utf-8"))
    material = next(m for m in manifest["papers"][0]["materials"] if m["material_id"] == imported["material_id"])
    exported_path = exported_dir / material["bundle_path"]
    assert exported_path.read_bytes() == original.read_bytes()
    assert (exported_dir / "references.bib").is_file()

    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("Existing laboratory rules\n", encoding="utf-8")
    command("attach", "--project", str(project), "--collection", collection_id)
    diagnostic = command("doctor", "--project", str(project))
    assert diagnostic["collection_exists"] is True
    assert diagnostic["library_matches"] is True
    context = command("context", "--project", str(project), "--task", "retrieval evaluation")
    assert context["papers"][0]["paper_id"] == paper_id
    command("detach", "--project", str(project))
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "Existing laboratory rules\n"


def test_real_cli_invalid_metadata_and_export_do_not_report_success(tmp_path):
    library = tmp_path / "library"
    metadata = tmp_path / "invalid-metadata.json"
    metadata.write_text('{}', encoding="utf-8")
    result = runner.invoke(app, ["--library", str(library), "paper", "add", "--metadata", str(metadata)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "data" not in payload
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    result = runner.invoke(app, ["--library", str(library), "collection", "export", "--collection", "missing",
                                 "--output", str(tmp_path / "no-export")])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "NOT_FOUND"
    assert not (tmp_path / "no-export").exists()


def test_real_cli_bibliography_import_and_tags(tmp_path):
    library = tmp_path / "library"
    def command(*args):
        result = runner.invoke(app, ["--library", str(library), "--json", *args])
        assert result.exit_code == 0, (args, result.output, result.exception)
        return json.loads(result.output)["data"]
    cid = command("collection", "create", "--name", "Imported references")["collection_id"]
    bibliography = tmp_path / "references.ris"
    bibliography.write_text("TY  - JOUR\nTI  - Reproducible Retrieval Evaluation\nAU  - Example Author\nPY  - 2025\nDO  - 10.5555/research-base-cli-fixture\nKW  - screening\nER  - \n", encoding="utf-8")
    imported = command("collection", "import", "--collection", cid, "--file", str(bibliography), "--format", "ris")
    assert imported["paper_count"] == 1
    assert imported["attachments_auto_imported"] is False
    pid = imported["paper_ids"][0]
    assert command("paper", "list", "--collection", cid)["total"] == 0
    assert command("paper", "list", "--collection", cid, "--membership", "candidate", "--tag", "screening")["total"] == 1
    tagged = command("paper", "tag", pid, "--tag", "baseline", "--tag", "review")
    assert set(tagged["tags"]) >= {"screening", "baseline", "review"}
    replaced = command("paper", "tag", pid, "--tag", "selected", "--replace")
    assert replaced["tags"] == ["selected"]
    command("collection", "include", "--collection", cid, "--paper", pid)
    assert command("paper", "list", "--collection", cid, "--tag", "selected")["total"] == 1


def test_frontier_cli_loads_only_explicit_seed_json_and_source_queries(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    seeds = tmp_path / "seeds.json"
    known = [{"title": "Known paper", "doi": "10.5555/known"}, {"arxiv": "2408.08067"}]
    seeds.write_text(json.dumps(known), encoding="utf-8")
    queries = tmp_path / "queries.json"
    queries.write_text(json.dumps({"arxiv": "all:retrieval AND all:evaluation"}), encoding="utf-8")
    result = runner.invoke(app, ["frontier", "--collection", "col-test", "--query", "retrieval evaluation", "--seeds", str(seeds),
                                 "--source", "openalex", "--source", "arxiv", "--source-queries", str(queries), "--max-requests", "8", "--broad-requests", "4"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["seeds"] == known and data["sources"] == ["openalex", "arxiv"]
    assert data["source_queries"] == {"arxiv": "all:retrieval AND all:evaluation"}
    assert data["budget"] == {"max_requests": 8, "broad_requests": 4}
    assert FakeBase.instances[-1].closed


def test_frontier_cli_rejects_bad_seeds_before_initializing_library(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    for name, value in (("object", {"title": "Not a list"}), ("empty", []), ("missing", [{}]), ("path", [{"path": "file"}])):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        before = len(FakeBase.instances)
        result = runner.invoke(app, ["frontier", "--collection", "c", "--query", "q", "--seeds", str(path)])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"
        assert len(FakeBase.instances) == before
    result = runner.invoke(app, ["frontier", "--collection", "c", "--query", "q", "--seeds", str(path),
                                 "--max-requests", "2", "--broad-requests", "3"])
    assert result.exit_code == 1
    assert "broad-requests" in json.loads(result.output)["error"]["message"]


def test_citation_expansion_cli_and_retry_access_are_explicit(monkeypatch):
    install_fake(monkeypatch)
    result = runner.invoke(app, ["citations", "expand", "--collection", "c", "--paper", "p1", "--paper", "p2",
                                 "--direction", "both", "--date-from", "2024-01-01", "--max-requests", "5"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["paper_ids"] == ["p1", "p2"] and data["direction"] == "both"
    assert data["date_from"] == "2024-01-01" and data["budget"] == {"max_requests": 5}
    invalid = runner.invoke(app, ["citations", "expand", "--collection", "c", "--paper", "p1", "--direction", "all-hops"])
    assert invalid.exit_code == 1
    assert json.loads(invalid.output)["error"]["code"] == "INVALID_ARGUMENT"
    retry = runner.invoke(app, ["job", "retry-access", "job-existing", "--keep-locations"])
    assert retry.exit_code == 0
    assert json.loads(retry.output)["data"] == {"job_id": "job-existing", "refresh_locations": False}


def test_real_core_frontier_graph_and_retry_api_through_cli(monkeypatch, tmp_path):
    import httpx
    import research_base.core as core_module
    from research_base.models import Candidate, SourcePage

    class Broad:
        request_count = 0
        def list_sources(self):
            return [{"name": "alpha"}]
        def search_page(self, *args, **kwargs):
            self.request_count += 1
            return SourcePage(items=[Candidate(source="alpha", source_id="broad-one", title="Broad Retrieval Evaluation",
                                               doi="10.5555/broad", version_kind="published")], total=1)

    class Scholar:
        requests_count = 0
        def lookup(self, seed, **kwargs):
            self.requests_count += 1
            return {"matched": True, "candidates": [Candidate(source="verified_lookup", source_id="seed-one", title="Known Seed Paper",
                                                               doi=seed["doi"], version_kind="published")],
                    "attempts": [{"provider": "fixture", "status": "matched"}]}
        def neighbors(self, seed, direction="citations", **kwargs):
            self.requests_count += 1
            return {"provider": "fixture", "attempts": [], "page": SourcePage(items=[
                Candidate(source="graph", source_id=direction, title="Graph " + direction, doi="10.5555/" + direction,
                          version_kind="published")], total=1)}

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
        with core_module.ResearchBase(tmp_path / "library", source_registry=Broad(), scholarly_client=Scholar(), client=client) as base:
            cid = base.create_collection("Workflow interoperability")["collection_id"]
            monkeypatch.setattr(core_module, "ResearchBase", lambda path: base)
            def command(*args):
                response = runner.invoke(app, ["--library", str(base.root), *args])
                assert response.exit_code == 0, (response.output, response.exception)
                return json.loads(response.output)["data"]
            seeds = tmp_path / "seeds.json"
            seeds.write_text(json.dumps([{"doi": "10.5555/known", "title": "Known Seed Paper"}]), encoding="utf-8")
            frontier = command("frontier", "--collection", cid, "--query", "retrieval", "--seeds", str(seeds),
                               "--source", "alpha", "--max-requests", "3", "--broad-requests", "1")
            finished = command("worker", "run", "--job", frontier["job_id"])
            assert finished["progress"]["broad_recall"] == {"found": 0, "total": 1}
            assert finished["progress"]["seed_coverage"] == {"found": 1, "total": 1}
            paper_id = finished["progress"]["seeds"][0]["paper_ids"][0]
            with base.store.connect() as db:
                before = [tuple(row) for row in db.execute("SELECT * FROM search_runs")]
            graph = command("citations", "expand", "--collection", cid, "--paper", paper_id, "--direction", "both", "--max-requests", "2")
            graph_result = command("worker", "run", "--job", graph["job_id"])
            assert graph_result["progress"]["edges_added"] == 2
            assert len(graph_result["progress"]["paper_ids"]) == 2
            with base.store.connect() as db:
                assert before == [tuple(row) for row in db.execute("SELECT * FROM search_runs")]
            base.set_membership(cid, [paper_id])
            acquisition = base.acquire_papers(cid)
            base.run_worker(acquisition["job_id"])
            ticket = base.claim_browser_task("fixture-worker", session_id="fixture-session", job_id=acquisition["job_id"])
            retried = command("job", "retry-access", acquisition["job_id"])
            assert retried["retried_task_ids"] == []
            assert retried["skipped"] == [{"task_id": ticket["task_id"], "state": "leased"}]
