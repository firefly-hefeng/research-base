"""Observable workflow contracts over the real SQLite catalog, without network.

Provider fixtures supply records, not precomputed library state. These cases
exercise source attribution, resumability and version policy through the core.
"""
from __future__ import annotations

import json

import httpx
import pytest

from research_base.core import ResearchBase
from research_base.models import Candidate, ResearchError, SourcePage


def publication(source="fixture", source_id="record", *, doi="10.5555/record", title="Research Retrieval Evaluation", **kwargs):
    return Candidate(source=source, source_id=source_id, title=title, authors=["Example Researcher"],
                     year=2026, doi=doi, version_kind=kwargs.pop("version_kind", "published"), **kwargs)


class Pages:
    def __init__(self, pages=None):
        self.pages = pages or {None: SourcePage(total=0)}
        self.request_count = 0
        self.calls = []

    def list_sources(self):
        return [{"name": "alpha"}]

    def search_page(self, name, query, **kwargs):
        self.request_count += 1
        self.calls.append((name, query, kwargs.get("cursor")))
        return self.pages[kwargs.get("cursor")]


class Scholar:
    def __init__(self, lookups=None, graph=None):
        self.lookups = lookups or []
        self.graph = graph or {}
        self.requests_count = 0
        self.lookup_calls = []
        self.graph_calls = []

    def lookup(self, seed, **kwargs):
        self.requests_count += 1
        self.lookup_calls.append(seed)
        value = self.lookups[min(len(self.lookup_calls) - 1, len(self.lookups) - 1)] if self.lookups else {"candidates": [], "attempts": [{"status": "not_found"}]}
        if isinstance(value, Exception):
            raise value
        return value

    def neighbors(self, seed, direction="citations", cursor=None, **kwargs):
        self.requests_count += 1
        self.graph_calls.append((direction, cursor))
        return {"page": self.graph[(direction, cursor)], "provider": "fixture_graph", "attempts": []}


@pytest.fixture
def library_factory(tmp_path):
    opened = []

    def create(*, sources=None, scholar=None):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        base = ResearchBase(tmp_path / ("library-" + str(len(opened))), source_registry=sources or Pages(),
                            scholarly_client=scholar or Scholar(), client=client)
        opened.append((base, client))
        cid = base.create_collection("Frontier workflow fixture")["collection_id"]
        return base, cid

    yield create
    for base, client in opened:
        base.close()
        client.close()


def test_existing_library_seed_cannot_be_counted_as_fresh_source_recall(library_factory):
    broad = publication("alpha", "broad", doi="10.5555/broad", title="Related but Different Work")
    source = Pages({None: SourcePage(items=[broad], total=1)})
    scholar = Scholar()
    base, cid = library_factory(sources=source, scholar=scholar)
    seed = publication("previous_import", "known", doi="10.5555/known", title="Known Seed Paper")
    cached = base.add_paper(seed, collection_id=cid)
    job = base.discover_frontier(cid, "retrieval", [{"doi": seed.doi}], sources=["alpha"],
                                 budget={"max_requests": 3, "broad_requests": 1})
    result = base.run_worker(job["job_id"])
    assert result["progress"]["broad_recall"] == {"found": 0, "total": 1}
    assert result["progress"]["seed_coverage"] == {"found": 0, "total": 1}
    assert result["progress"]["seeds"][0]["status"] == "not_found"
    assert result["progress"]["seeds"][0]["paper_ids"] == []
    assert base.get_paper(cached["paper_id"])["doi"] == seed.doi
    assert len(scholar.lookup_calls) == 1


def test_broad_identity_snapshot_is_not_polluted_by_later_lookup_enrichment(library_factory):
    before = publication("alpha", "shared-provider-record", doi=None, title="General Retrieval Study",
                         identifiers={"openalex": "W_SHARED"}, version_kind="unknown")
    recovered = publication("alpha", "shared-provider-record", doi="10.5555/target", title="Known Seed Paper",
                            identifiers={"openalex": "W_SHARED"})
    base, cid = library_factory(sources=Pages({None: SourcePage(items=[before], total=1)}),
                                scholar=Scholar([{"candidates": [recovered], "attempts": [{"status": "matched"}]}]))
    result = base.run_worker(base.discover_frontier(cid, "retrieval", [{"doi": "10.5555/target"}],
                                                   sources=["alpha"], budget={"max_requests": 3, "broad_requests": 1})["job_id"])
    progress = result["progress"]
    assert progress["broad_recall"] == {"found": 0, "total": 1}
    assert progress["seed_coverage"] == {"found": 1, "total": 1}
    assert progress["seeds"][0]["status"] == "found_lookup"
    pid = progress["seeds"][0]["paper_ids"][0]
    assert base.get_paper(pid)["doi"] == "10.5555/target"
    with base.store.connect() as db:
        identities = [json.loads(row[0]) for row in db.execute(
            "SELECT identity FROM discovery_hits WHERE job_id=? AND stage='broad'", (progress["broad_job_id"],))]
    assert len(identities) == 1 and identities[0]["doi"] is None


def test_one_request_budget_resumes_seed_work_before_more_broad_pages(library_factory):
    broad = publication("alpha", "first", doi="10.5555/broad")
    source = Pages({None: SourcePage(items=[broad], next_cursor="page-two", total=3),
                    "page-two": SourcePage(items=[publication("alpha", "second", doi="10.5555/second")], next_cursor="page-three", total=3)})
    scholar = Scholar([{"candidates": [publication("lookup", "known", doi="10.5555/known")], "attempts": [{"status": "matched"}]}])
    base, cid = library_factory(sources=source, scholar=scholar)
    job = base.discover_frontier(cid, "retrieval", [{"doi": "10.5555/known"}], sources=["alpha"], budget={"max_requests": 1})
    first = base.run_worker(job["job_id"])
    assert first["state"] == "paused" and not scholar.lookup_calls
    base.resume_job(job["job_id"])
    second = base.run_worker(job["job_id"])
    assert second["progress"]["seed_coverage"]["found"] == 1
    assert len(source.calls) == 1
    base.resume_job(job["job_id"])
    third = base.run_worker(job["job_id"])
    assert third["progress"]["seed_coverage"]["found"] == 1
    assert len(scholar.lookup_calls) == 1 and len(source.calls) == 2


def test_lookup_provider_error_is_not_not_found_and_is_retryable(library_factory):
    scholar = Scholar([
        {"candidates": [], "attempts": [{"provider": "fixture", "status": "error", "code": "SOURCE_RATE_LIMITED"}]},
        {"candidates": [publication("lookup", "known", doi="10.5555/known")], "attempts": [{"status": "matched"}]},
    ])
    base, cid = library_factory(scholar=scholar)
    job = base.discover_frontier(cid, "retrieval", [{"doi": "10.5555/known"}], sources=["alpha"],
                                 budget={"max_requests": 3, "broad_requests": 1})
    first = base.run_worker(job["job_id"])
    assert first["progress"]["seeds"][0]["status"] == "error"
    assert first["progress"]["seed_coverage"]["found"] == 0
    assert first["state"] != "succeeded"
    base.resume_job(job["job_id"])
    second = base.run_worker(job["job_id"])
    assert second["progress"]["seeds"][0]["status"] == "found_lookup"
    assert second["progress"]["seed_coverage"]["found"] == 1


def test_both_graph_directions_resume_deduplicate_and_preserve_topic_freshness(library_factory):
    seed = publication("manual", "seed", doi="10.5555/seed", title="Known Seed Paper")
    citing_one = publication("graph", "citing-one", doi="10.5555/citing-one")
    citing_two = publication("graph", "citing-two", doi="10.5555/citing-two")
    ref_one = publication("graph", "ref-one", doi="10.5555/ref-one")
    ref_two = publication("graph", "ref-two", doi="10.5555/ref-two")
    scholar = Scholar(graph={
        ("citations", None): SourcePage(items=[seed, citing_one], next_cursor="c-next", total=2),
        ("references", None): SourcePage(items=[ref_one], next_cursor="r-next", total=2),
        ("citations", "c-next"): SourcePage(items=[citing_one, citing_two], total=2),
        ("references", "r-next"): SourcePage(items=[ref_one, ref_two], total=2),
    })
    base, cid = library_factory(scholar=scholar)
    stored = base.add_paper(seed, collection_id=cid)
    excluded = base.add_paper(citing_one, collection_id=cid, status="excluded")
    base.run_worker(base.discover_papers(cid, "retrieval", sources=["alpha"])["job_id"])
    with base.store.connect() as db:
        freshness_before = [tuple(row) for row in db.execute("SELECT * FROM search_runs")]
    job = base.expand_citations(cid, [stored["paper_id"]], direction="both", budget={"max_requests": 2})
    first = base.run_worker(job["job_id"])
    assert first["state"] == "paused" and first["progress"]["edges_added"] == 2
    base.resume_job(job["job_id"])
    final = base.run_worker(job["job_id"])
    assert final["state"] == "succeeded"
    assert final["progress"]["edges_added"] == 4 and len(final["progress"]["paper_ids"]) == 4
    ids = {base.get_paper(pid)["doi"]: pid for pid in final["progress"]["paper_ids"]}
    with base.store.connect() as db:
        actual = {tuple(row) for row in db.execute("SELECT citing_paper_id,cited_paper_id FROM citation_edges")}
        assert actual == {(ids[citing_one.doi], stored["paper_id"]), (ids[citing_two.doi], stored["paper_id"]),
                          (stored["paper_id"], ids[ref_one.doi]), (stored["paper_id"], ids[ref_two.doi])}
        assert freshness_before == [tuple(row) for row in db.execute("SELECT * FROM search_runs")]
        assert db.execute("SELECT status FROM memberships WHERE collection_id=? AND paper_id=?", (cid, excluded["paper_id"])).fetchone()[0] == "excluded"
    base.run_worker(job["job_id"])
    assert len(scholar.graph_calls) == 4


def test_cancelling_frontier_also_cancels_its_existing_broad_child(library_factory):
    source = Pages({None: SourcePage(items=[publication("alpha", "first")], next_cursor="more", total=2)})
    base, cid = library_factory(sources=source)
    job = base.discover_frontier(cid, "retrieval", [{"doi": "10.5555/known"}], sources=["alpha"], budget={"max_requests": 1})
    running = base.run_worker(job["job_id"])
    child = running["progress"]["broad_job_id"]
    cancelled = base.cancel_job(job["job_id"])
    assert cancelled["state"] == "cancelled" and base.get_job(child)["state"] == "cancelled"
    assert base.run_worker(job["job_id"])["state"] == "cancelled"
    assert len(source.calls) == 1
    with pytest.raises(ResearchError):
        base.resume_job(job["job_id"])


def test_retry_access_preserves_succeeded_human_and_active_browser_barriers(library_factory, tmp_path):
    base, cid = library_factory()
    papers = [base.add_paper(publication("manual", str(index), doi=f"10.5555/paper-{index}"), collection_id=cid) for index in range(4)]
    original = tmp_path / "confirmed.txt"
    original.write_text("Explicitly confirmed original material", encoding="utf-8")
    base.import_material(papers[0]["paper_id"], path=original, version_id=papers[0]["preferred_version_id"], confirmed=True)
    job = base.acquire_papers(cid)
    base.run_worker(job["job_id"])
    human = base.claim_browser_task("worker", session_id="human", job_id=job["job_id"])
    base.submit_browser_result(human["task_id"], human["lease"]["token"], "human-check", {"outcome": "needs_user", "reason": "Institutional sign-in required"})
    leased = base.claim_browser_task("worker", session_id="active", job_id=job["job_id"])
    response = base.retry_acquisition(job["job_id"])
    assert len(response["retried_task_ids"]) == 1
    assert {entry["state"] for entry in response["skipped"]} == {"succeeded", "needs_user", "leased"}
    assert base.renew_browser_lease(leased["task_id"], leased["lease"]["token"])["task_id"] == leased["task_id"]
    states = base.get_job(job["job_id"])["task_count"]
    assert states == {"succeeded": 1, "needs_user": 1, "leased": 1, "queued": 1}


def test_retry_old_failed_task_does_not_collide_with_replacement_target(library_factory):
    base, cid = library_factory()
    base.add_paper(publication(), collection_id=cid)
    old = base.acquire_papers(cid)
    old_task = old["tasks"][0]["task_id"]
    with base.store.connect() as db:
        db.execute("UPDATE tasks SET state='failed' WHERE id=?", (old_task,))
    replacement = base.acquire_papers(cid)
    active = replacement["tasks"][0]["task_id"]
    assert active != old_task
    retried = base.retry_acquisition(old["job_id"])
    assert retried["retried_task_ids"] == []
    assert retried["skipped"] == [{"task_id": old_task, "state": "failed", "reason": "active_target_exists", "active_task_id": active}]
    assert base.get_job(replacement["job_id"])["tasks"][0]["state"] == "queued"


def test_explicit_publication_preprint_relation_preserves_ids_and_strict_version_policy(library_factory, tmp_path):
    base, cid = library_factory()
    pub_record = publication("crossref", "publication", doi="10.5555/publication", title="Predicting Cellular Responses")
    pre_record = publication("biorxiv", "preprint", doi="10.5555/preprint", title="Predicting Cellular Responses", version_kind="preprint")
    published = base.add_paper(pub_record, collection_id=cid)
    preprint = base.add_paper(pre_record, collection_id=cid)
    assert published["paper_id"] != preprint["paper_id"]
    assert base.get_paper(published["paper_id"])["related_paper_ids"] == []
    related = pre_record.model_copy(update={"extra": {"published_doi": pub_record.doi}})
    linked = base.add_paper(related, collection_id=cid)
    assert linked["paper_id"] == preprint["paper_id"]
    assert base.get_paper(published["paper_id"])["related_paper_ids"] == [preprint["paper_id"]]
    assert base.get_paper(preprint["paper_id"])["related_paper_ids"] == [published["paper_id"]]
    with base.store.connect() as db:
        assert db.execute("SELECT paper_id FROM identifiers WHERE namespace='doi' AND value=?", (pub_record.doi,)).fetchone()[0] == published["paper_id"]
        assert db.execute("SELECT paper_id FROM identifiers WHERE namespace='doi' AND value=?", (pre_record.doi,)).fetchone()[0] == preprint["paper_id"]
    original = tmp_path / "preprint.txt"
    original.write_text("The explicitly identified preprint original", encoding="utf-8")
    imported = base.import_material(preprint["paper_id"], path=original, version_id=preprint["preferred_version_id"], confirmed=True)
    strict = base.acquire_papers(cid, paper_ids=[published["paper_id"]], version_policy=["published"])
    assert strict["state"] != "succeeded"
    permissive = base.acquire_papers(cid, paper_ids=[published["paper_id"]], version_policy=["preprint"])
    assert permissive["state"] == "succeeded"
    assert permissive["tasks"][0]["result"]["material_id"] == imported["material_id"]
    assert base.get_job(strict["job_id"])["state"] != "succeeded"
