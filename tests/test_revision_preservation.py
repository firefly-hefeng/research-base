"""A provider work ID must not overwrite archived revision identity."""
import httpx
import pytest

from research_base.core import ResearchBase


@pytest.mark.parametrize("doi", [None, "10.48550/arxiv.2601.01234"])
def test_aggregated_source_revision_update_preserves_old_material(tmp_path, doi):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
        with ResearchBase(tmp_path / "library", client=client) as base:
            def record(revision):
                return {"source": "openalex", "source_id": "W_FIXTURE", "title": "Research on Stable Material Identity",
                        "doi": doi, "version_kind": "preprint", "identifiers": {"arxiv": f"2601.01234v{revision}"}}
            first = base.add_paper(record(1))
            v1 = first["preferred_version_id"]
            code = tmp_path / "revision-1.md"
            code.write_text("Original revision one author material", encoding="utf-8")
            material = base.import_material(first["paper_id"], path=code, role="code", version_id=v1)
            updated = base.add_paper(record(2))
            assert updated["paper_id"] == first["paper_id"]
            v2 = updated["preferred_version_id"]
            assert v1 != v2
            assert {v["metadata"]["identifiers"]["arxiv"] for v in updated["versions"]} == {"2601.01234v1", "2601.01234v2"}
            assert base.add_paper(record(2))["preferred_version_id"] == v2
            older = base.add_paper(record(1))
            assert len(older["versions"]) == 2 and older["preferred_version_id"] == v2
            archived = next(m for m in older["materials"] if m["material_id"] == material["material_id"])
            assert archived["version_id"] == v1 and archived["sha256"] == material["sha256"]
            with base.store.connect() as db:
                mapping = {r["value"]: r["version_id"] for r in db.execute("SELECT value,version_id FROM identifiers WHERE namespace='arxiv'")}
            assert mapping == {"2601.01234v1": v1, "2601.01234v2": v2}


def test_publication_with_shared_arxiv_alias_reuses_its_compatible_version(tmp_path):
    with ResearchBase(tmp_path / "library") as base:
        preprint = {"source": "openalex", "source_id": "W_SHARED", "title": "Research on Stable Material Identity",
                    "doi": "10.48550/arxiv.2601.01234", "version_kind": "preprint", "identifiers": {"arxiv": "2601.01234v2"}}
        first = base.add_paper(preprint)
        published = {**preprint, "doi": "10.5555/publication", "version_kind": "published"}
        formal = base.add_paper(published)
        formal_id = formal["preferred_version_id"]
        assert formal["paper_id"] == first["paper_id"] and len(formal["versions"]) == 2
        for _ in range(3):
            again = base.add_paper(published)
            assert len(again["versions"]) == 2 and again["preferred_version_id"] == formal_id
        assert len(base.add_paper(preprint)["versions"]) == 2
