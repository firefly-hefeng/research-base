"""Model/MCP lifecycle calls over persisted jobs, without network access."""
from __future__ import annotations

import httpx
import pytest

from research_base.core import ResearchBase
from research_base.models import Candidate, SourcePage
from research_base.tools import ToolRegistry


def paper(identifier):
    return Candidate(source="fixture", source_id=identifier,
                     title="Research Workflow " + identifier, authors=["Example Researcher"],
                     year=2026, doi="10.5555/" + identifier, version_kind="published")


class Sources:
    def list_sources(self):
        return [{"name": "fixture"}]

    def search_page(self, *args, **kwargs):
        return SourcePage(total=0)


class Scholar:
    def __init__(self):
        self.requests_count = 0
        self.cursors = []

    def neighbors(self, seed, direction="citations", cursor=None, **kwargs):
        self.requests_count += 1
        self.cursors.append(cursor)
        return {"provider": "fixture", "attempts": [],
                "page": SourcePage(items=[paper("citing-one")] if cursor is None else [paper("citing-one"), paper("citing-two")],
                                   next_cursor="page-two" if cursor is None else None, total=2)}


@pytest.fixture
def library(tmp_path):
    scholar = Scholar()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
        with ResearchBase(tmp_path / "library", source_registry=Sources(), scholarly_client=scholar, client=client) as base:
            cid = base.create_collection("Lifecycle workflow")["collection_id"]
            seed = base.add_paper(paper("seed"), collection_id=cid, status="included")
            yield base, cid, seed["paper_id"], scholar


@pytest.mark.parametrize("local_admin", [False, True])
def test_registry_resumes_real_paused_citation_pages(library, local_admin):
    base, cid, seed, scholar = library
    registry = ToolRegistry(base, allow_writes=True, local_admin=local_admin)
    job = registry.call("expand_citations", {"collection_id": cid, "paper_ids": [seed],
                                            "budget": {"max_requests": 1}})["data"]
    first = registry.call("run_worker", {"job_id": job["job_id"]})["data"]
    assert first["state"] == "paused"
    assert first["progress"]["streams"][0]["cursor"] == "page-two"
    registry.call("run_worker", {"job_id": job["job_id"]})
    assert scholar.cursors == [None]
    assert ToolRegistry(base).call("resume_job", {"job_id": job["job_id"]})["error"]["code"] == "TOOL_NOT_ALLOWED"
    resumed = registry.call("resume_job", {"job_id": job["job_id"]})["data"]
    assert resumed["state"] == "queued"
    assert resumed["progress"]["streams"][0]["cursor"] == "page-two"
    final = registry.call("run_worker", {"job_id": job["job_id"]})["data"]
    assert final["state"] == "succeeded"
    assert scholar.cursors == [None, "page-two"]
    assert final["progress"]["requests"] == 2
    assert final["progress"]["edges_added"] == 2
    assert len(final["progress"]["paper_ids"]) == 2


@pytest.mark.parametrize("local_admin", [False, True])
def test_registry_cannot_resume_real_acquisition_human_obstacle(library, local_admin):
    base, cid, seed, _ = library
    job = base.run_worker(base.acquire_papers(cid)["job_id"])
    claimed = base.claim_browser_task("fixture-host", job_id=job["job_id"])
    base.submit_browser_result(claimed["task_id"], claimed["lease"]["token"], "human-obstacle",
                               {"outcome": "needs_user", "reason": "Institutional sign-in required"})
    before = base.get_job(job["job_id"])
    assert before["tasks"][0]["state"] == "needs_user"
    registry = ToolRegistry(base, allow_writes=True, local_admin=local_admin)
    rejected = registry.call("resume_job", {"job_id": job["job_id"]})
    assert rejected["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert rejected["error"]["details"]["job_type"] == "acquisition"
    after = base.get_job(job["job_id"])
    assert after["state"] == before["state"]
    assert after["tasks"] == before["tasks"]
    retry = registry.call("retry_acquisition", {"job_id": job["job_id"]})["data"]
    assert retry["retried_task_ids"] == []
    assert retry["skipped"][0]["state"] == "needs_user"
    assert base.get_job(job["job_id"])["tasks"][0]["state"] == "needs_user"


def test_registry_cancellation_preserves_papers_and_rejects_invalid_lifecycle_arguments(library):
    base, cid, seed, scholar = library
    job = base.expand_citations(cid, [seed], budget={"max_requests": 1})
    registry = ToolRegistry(base, allow_writes=True)
    assert ToolRegistry(base).call("cancel_job", {"job_id": job["job_id"]})["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert registry.call("resume_job", {})["error"]["code"] == "INVALID_ARGUMENT"
    assert registry.call("resume_job", {"job_id": job["job_id"], "force": True})["error"]["code"] == "INVALID_ARGUMENT"
    assert registry.call("resume_job", {"job_id": "missing"})["error"]["code"] == "NOT_FOUND"
    assert registry.call("cancel_job", {"job_id": job["job_id"]})["data"]["state"] == "cancelled"
    registry.call("run_worker", {"job_id": job["job_id"]})
    assert scholar.cursors == []
    assert base.get_paper(seed)["title"] == "Research Workflow seed"
    assert registry.call("resume_job", {"job_id": job["job_id"]})["error"]["code"] == "INVALID_ARGUMENT"
