"""An HTTP success containing a provider error must not certify coverage."""
import httpx
import pytest

from research_base.core import ResearchBase
from research_base.sources import SourceRegistry


@pytest.mark.parametrize("source", ["openalex", "crossref", "arxiv", "europe_pmc", "pubmed", "semantic_scholar", "doaj", "datacite"])
def test_provider_error_cannot_complete_source_or_refresh_freshness(tmp_path, source):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"error": "upstream unavailable"}))) as client:
        registry = SourceRegistry(client, throttle=False)
        with ResearchBase(tmp_path / "library", source_registry=registry, client=client) as app:
            collection = app.create_collection("Coverage", config={"discovery": {"query": "retrieval", "sources": [source]}})
            cid = collection["collection_id"]
            job = app.discover_papers(cid, "retrieval", sources=[source])
            result = app.run_worker(job["job_id"])
            assert result["state"] == "failed"
            assert result["progress"]["sources"][source]["complete"] is False
            assert result["progress"]["sources"][source]["error"]["code"] == "SOURCE_INVALID_RESPONSE"
            context = app.prepare_context("retrieval", collection_id=cid)
            assert context["freshness"]["last_complete_search_at"] is None
            assert context["freshness"]["needs_refresh"] is True
            assert context["freshness"]["reason"] == "never_completed"
            with app.store.connect() as db:
                assert db.execute("SELECT count(*) FROM search_runs WHERE collection_id=?", (cid,)).fetchone()[0] == 0
