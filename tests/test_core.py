import json
from pathlib import Path

import httpx
import pytest

from research_base.core import ResearchBase
from research_base.models import ResearchError


class OfflineSources:
    def list_sources(self):
        return [{"name": "offline"}]

    def search_page(self, *args, **kwargs):
        raise AssertionError("This test must not use the network")


@pytest.fixture
def base(tmp_path):
    def offline(request):
        raise AssertionError("This test must not use the network")
    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        with ResearchBase(tmp_path / "library", source_registry=OfflineSources(), client=client) as library:
            yield library


def paper(source="offline", source_id="one", **extra):
    return {"source": source, "source_id": source_id, "title": "Evaluating Retrieval Quality in Research Systems", "authors": ["Example Author"], "year": 2026, **extra}


def test_same_doi_from_multiple_sources_reuses_paper_and_version(base):
    one = base.add_paper(paper("openalex", "W1", doi="https://doi.org/10.5555/DEMO", version_kind="published"))
    two = base.add_paper(paper("crossref", "10.5555/demo", doi="doi:10.5555/demo", version_kind="published"))
    assert one["paper_id"] == two["paper_id"]
    assert len(two["versions"]) == 1
    assert {entry["source"] for entry in two["sources"]} == {"openalex", "crossref"}


def test_aggregate_source_publication_does_not_rewrite_preprint_identity(base):
    first = base.add_paper(paper('openalex', 'W_AGGREGATED', version_kind='preprint', identifiers={'arxiv': '2601.01234v1'}))
    old_version = first['preferred_version_id']
    updated = base.add_paper(paper('openalex', 'W_AGGREGATED', version_kind='published', doi='10.5555/published'))
    assert updated['paper_id'] == first['paper_id']
    assert len(updated['versions']) == 2
    assert next(v for v in updated['versions'] if v['version_id'] == old_version)['kind'] == 'preprint'
    assert updated['preferred_version_id'] != old_version


def test_older_arxiv_revision_does_not_replace_newer_preference(base):
    recent = base.add_paper(paper('arxiv', '2601.01234v2', version_kind='preprint', identifiers={'arxiv': '2601.01234v2'}))
    older = base.add_paper(paper('arxiv', '2601.01234v1', version_kind='preprint', identifiers={'arxiv': '2601.01234v1'}))
    assert older['paper_id'] == recent['paper_id']
    assert older['preferred_version_id'] == recent['preferred_version_id']


def test_doi_supplied_only_in_identifier_map_still_reuses_version(base):
    one = base.add_paper(paper("openalex", "W1", doi="10.5555/demo", version_kind="published"))
    two = base.add_paper(paper("custom", "custom-1", identifiers={"doi": "10.5555/demo"}, version_kind="published"))
    assert one["paper_id"] == two["paper_id"]
    assert len(two["versions"]) == 1
    assert two["doi"] == "10.5555/demo"


def test_equal_titles_without_identity_link_are_not_merged(base):
    one = base.add_paper(paper("custom", "one"))
    two = base.add_paper(paper("custom", "two"))
    assert one["paper_id"] != two["paper_id"]


def test_versioned_preprints_remain_distinct_from_published_version(base):
    publication = base.add_paper(paper("crossref", "10.5555/published", doi="10.5555/published", version_kind="published"))
    preprint1 = base.add_paper(paper("arxiv", "2601.01234v1", identifiers={"arxiv": "2601.01234v1"}, version_kind="preprint", extra={"published_doi": "10.5555/published"}))
    preprint2 = base.add_paper(paper("arxiv", "2601.01234v2", identifiers={"arxiv": "2601.01234v2"}, version_kind="preprint", extra={"published_doi": "10.5555/published"}))
    assert publication["paper_id"] == preprint1["paper_id"] == preprint2["paper_id"]
    assert len(preprint2["versions"]) == 3
    assert preprint2["preferred_version_id"] == publication["preferred_version_id"]
    assert sum(v["kind"] == "preprint" for v in preprint2["versions"]) == 2


def test_conflicting_identifiers_do_not_merge_existing_papers(base):
    one = base.add_paper(paper("openalex", "W1", doi="10.5555/one"))
    two = base.add_paper(paper("custom", "two", doi="10.5555/two"))
    with pytest.raises(ResearchError) as error:
        base.add_paper(paper("openalex", "W1", doi="10.5555/two"))
    assert error.value.code == "IDENTITY_CONFLICT"
    assert base.get_paper(one["paper_id"])["doi"] == "10.5555/one"
    assert base.get_paper(two["paper_id"])["doi"] == "10.5555/two"


def test_chinese_substring_search_finds_original_text(base, tmp_path):
    collection = base.create_collection("蛋白研究")
    record = base.add_paper(paper(), collection["collection_id"])
    source = tmp_path / "method.txt"
    source.write_text("该研究提出蛋白质表达调控网络的实验方法。", encoding="utf-8")
    material = base.import_material(record["paper_id"], path=source, confirmed=True)
    result = base.search_library("表达调控", collection_id=collection["collection_id"], content_scope="fulltext")
    assert result["search_mode"] == "fts+cjk_substring"
    assert result["papers"][0]["paper_id"] == record["paper_id"]
    assert result["papers"][0]["matches"][0]["material_id"] == material["material_id"]
    assert result["papers"][0]["matches"][0]["page"] == 1


def test_metadata_and_original_text_matches_are_distinguished(base, tmp_path):
    collection = base.create_collection("evaluation")
    record = base.add_paper(paper(abstract="metadataterm appears in an abstract"), collection["collection_id"])
    source = tmp_path / "paper.txt"
    source.write_text("Full text contains a bodyuniqueterm absent from the abstract.", encoding="utf-8")
    base.import_material(record["paper_id"], path=source, confirmed=True)
    metadata = base.search_library("metadataterm", collection_id=collection["collection_id"], content_scope="metadata")
    fulltext = base.search_library("bodyuniqueterm", collection_id=collection["collection_id"], content_scope="fulltext")
    absent = base.search_library("metadataterm", collection_id=collection["collection_id"], content_scope="fulltext")
    assert metadata["papers"][0]["matches"][0]["content_scope"] == "metadata"
    assert fulltext["papers"][0]["matches"][0]["content_scope"] == "fulltext"
    assert absent["papers"] == []


def test_excluded_and_candidate_items_are_not_injected_as_included_context(base):
    collection = base.create_collection("research")
    included = base.add_paper(paper(source_id="i"), collection["collection_id"])
    base.add_paper(paper(source_id="c"), collection["collection_id"], status="candidate")
    base.add_paper(paper(source_id="e"), collection["collection_id"], status="excluded")
    context = base.prepare_context("retrieval", collection_id=collection["collection_id"])
    assert [entry["paper_id"] for entry in context["papers"]] == [included["paper_id"]]
    assert context["freshness"]["needs_refresh"] is True
    assert context["gaps"][0]["reason"] == "fulltext_unavailable"


def test_context_reports_last_match_truncation(base):
    collection = base.create_collection("research")
    base.add_paper(paper(abstract="retrieval " + "x" * 1000), collection["collection_id"])
    context = base.prepare_context("retrieval", collection_id=collection["collection_id"], max_chars=256)
    assert sum(len(m["text"]) for p in context["papers"] for m in p["matches"]) <= 256
    assert context["truncated"] is True


def test_collection_bundle_copies_raw_files_with_portable_relative_entries(base, tmp_path):
    collection = base.create_collection("research")
    record = base.add_paper(paper(), collection["collection_id"])
    source = tmp_path / "source.txt"
    source.write_text("original material content", encoding="utf-8")
    material = base.import_material(record["paper_id"], path=source, confirmed=True)
    output = tmp_path / "bundle"
    exported = base.export_collection(collection["collection_id"], output)
    assert exported["portable"] is True
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["papers"][0]["materials"][0]
    assert not Path(entry["bundle_path"]).is_absolute()
    assert (output / entry["bundle_path"]).read_bytes() == source.read_bytes()
    assert Path(material["local_path"]).read_bytes() == source.read_bytes()
    assert (output / "references.bib").exists()


def test_context_with_wrong_library_binding_is_rejected(base, tmp_path):
    project = tmp_path / "project"
    (project / ".research").mkdir(parents=True)
    (project / ".research" / "config.yaml").write_text("library_id: another-library\ncollection_id: unknown\n", encoding="utf-8")
    with pytest.raises(ResearchError) as error:
        base.prepare_context("retrieval", project_root=project)
    assert error.value.code == "LIBRARY_ID_MISMATCH"
