from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from research_base.core import ResearchBase
from research_base.models import Candidate, ResearchError, SourcePage


class ScriptedSources:
    """Provider pages are deterministic; the actual library and worker are real."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.request_count = 0

    def list_sources(self):
        return [{"name": name} for name in self.pages]

    def search_page(self, name, query, **kwargs):
        self.request_count += 1
        self.calls.append((name, query, kwargs))
        value = self.pages[name][kwargs.get("cursor")]
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        pass


def candidate(source="alpha", source_id="one", *, doi="10.5555/retrieval", **kwargs):
    return Candidate(source=source, source_id=source_id, title="Research Retrieval Evaluation Study",
                     authors=["Example Scientist"], year=2026, doi=doi,
                     abstract="Retrieval evaluation study of research methods.", version_kind="published",
                     **kwargs)


@pytest.fixture
def library(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    registry = ScriptedSources({"alpha": {None: SourcePage()}})
    with ResearchBase(tmp_path / "library", source_registry=registry, client=client) as app:
        yield app
    client.close()


def collection(app, sources=None):
    config = {"discovery": {"query": "retrieval evaluation", "sources": sources or ["alpha"]}}
    return app.create_collection("Test research", config=config)["collection_id"]


def add_paper(app, cid, **kwargs):
    return app.add_paper(candidate(**kwargs).model_dump(), collection_id=cid)


def pdf(path: Path, title="Research Retrieval Evaluation Study", extra="10.5555/retrieval"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 50 750 Td ({title}) Tj 0 -20 Td ({extra}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def get_task(app, job):
    tasks = app.get_job(job["job_id"])["tasks"]
    assert len(tasks) == 1
    return tasks[0]


def browser_waiting(app, *, cid=None):
    cid = cid or collection(app)
    paper = add_paper(app, cid)
    job = app.acquire_papers(cid)
    result = app.run_worker(job["job_id"])
    assert result["state"] == "waiting_external"
    return cid, paper, job


def test_failed_known_location_uses_an_alternate_oa_resolver(library, tmp_path):
    app = library
    cid = collection(app)
    added = add_paper(app, cid, fulltext_urls=['https://broken.example/paper.pdf'])
    content = pdf(tmp_path / 'good.pdf').read_bytes()
    requests = []
    def handle(request):
        requests.append(request.url.host)
        if request.url.host == 'api.openalex.org':
            return httpx.Response(200, json={'best_oa_location': {'pdf_url': 'https://good.example/article.pdf', 'is_oa': True, 'version': 'publishedVersion'}})
        return httpx.Response(200, content=content) if request.url.host == 'good.example' else httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        app.client = client
        result = app.run_worker(app.acquire_papers(cid)['job_id'])
    assert result['state'] == 'succeeded'
    assert requests[:3] == ['broken.example', 'api.openalex.org', 'good.example']


def test_public_route_diagnostics_survive_browser_handoff_and_success(library, tmp_path):
    app = library
    cid = collection(app)
    add_paper(app, cid, fulltext_urls=["https://unavailable.example/article.pdf"])
    job = app.run_worker(app.acquire_papers(cid)["job_id"])
    before = job["tasks"][0]["result"]
    assert before["route_attempts"][0]["outcome"] == "failed"
    ticket = app.claim_browser_task("fixture-browser", job_id=job["job_id"])
    app.submit_browser_result(ticket["task_id"], ticket["lease"]["token"], "needs-signin", {"outcome": "needs_user", "reason": "Institution sign-in"})
    assert get_task(app, job)["result"]["route_attempts"] == before["route_attempts"]
    app.resume_browser_task(ticket["task_id"])
    ticket = app.claim_browser_task("fixture-browser", job_id=job["job_id"])
    path = pdf(Path(ticket["handoff"]["download_dir"]) / "article.pdf")
    app.submit_browser_result(ticket["task_id"], ticket["lease"]["token"], "signed-in-download", {"outcome": "downloaded", "file": {"kind": "local_path", "path": str(path)}})
    done = app.run_worker(job["job_id"])
    assert done["state"] == "succeeded"
    assert done["tasks"][0]["result"]["route_attempts"] == before["route_attempts"]
    assert "reason" not in done["tasks"][0]["result"]


def test_identity_ambiguity_does_not_skip_later_correct_pdf(library, tmp_path):
    app = library
    cid = collection(app)
    add_paper(app, cid, fulltext_urls=['https://repo.example/wrong.pdf', 'https://repo.example/right.pdf'])
    wrong = pdf(tmp_path / 'wrong.pdf', title='A Different Scientific Study', extra='10.5555/other').read_bytes()
    right = pdf(tmp_path / 'right.pdf').read_bytes()
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=wrong if request.url.path == '/wrong.pdf' else right)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        app.client = client
        result = app.run_worker(app.acquire_papers(cid)['job_id'])
    assert result['state'] == 'succeeded'
    assert calls == ['/wrong.pdf', '/right.pdf']


def test_small_budget_resumes_with_next_source_instead_of_starving_it(library):
    app = library
    app.sources = ScriptedSources({
        'alpha': {None: SourcePage(items=[candidate()], next_cursor='next')},
        'beta': {None: SourcePage(items=[candidate('beta', 'b', doi='10.5555/b')])},
    })
    cid = collection(app, ['alpha', 'beta'])
    job = app.discover_papers(cid, 'retrieval', sources=['alpha', 'beta'], budget={'max_requests': 1})
    app.run_worker(job['job_id'])
    app.resume_job(job['job_id'])
    app.run_worker(job['job_id'])
    assert [c[0] for c in app.sources.calls] == ['alpha', 'beta']


def test_multisource_pages_deduplicate_and_do_not_auto_include(library):
    app = library
    app.sources = ScriptedSources({
        "alpha": {None: SourcePage(items=[candidate()], next_cursor="next", total=2),
                  "next": SourcePage(items=[candidate(source_id="two", doi="10.5555/another")], total=2)},
        "beta": {None: SourcePage(items=[candidate(source="beta", source_id="same-paper")], total=1)},
    })
    cid = collection(app, ["alpha", "beta"])
    job = app.discover_papers(cid, "retrieval evaluation", sources=["alpha", "beta"],
                              source_queries={"beta": "provider specific"}, request_id="discovery-one")
    final = app.run_worker(job["job_id"])
    assert final["state"] == "succeeded"
    assert len(final["progress"]["paper_ids"]) == 2
    assert final["progress"]["requests"] == 3
    assert app.get_collection(cid)["counts"] == {"candidate": 2}
    merged = app.get_paper(final["progress"]["paper_ids"][0])
    assert len(merged["sources"]) == 2
    beta_call = next(call for call in app.sources.calls if call[0] == "beta")
    assert beta_call[1] == "provider specific"
    assert app.run_worker(job["job_id"])["state"] == "succeeded"
    assert len(app.sources.calls) == 3  # A finished job is not replayed.


def test_discovery_budget_resumes_from_cursor_without_losing_papers(library):
    app = library
    app.sources = ScriptedSources({"alpha": {
        None: SourcePage(items=[candidate()], next_cursor="second"),
        "second": SourcePage(items=[candidate(source_id="two", doi="10.5555/two")]),
    }})
    cid = collection(app)
    job = app.discover_papers(cid, "retrieval evaluation", sources=["alpha"], budget={"max_requests": 1})
    paused = app.run_worker(job["job_id"])
    assert paused["state"] == "paused"
    assert paused["progress"]["stop_reason"] == "budget_reached"
    assert paused["progress"]["sources"]["alpha"]["cursor"] == "second"
    assert app.prepare_context("retrieval", collection_id=cid)["freshness"]["reason"] == "never_completed"
    assert len(app.sources.calls) == 1
    app.resume_job(job["job_id"])
    final = app.run_worker(job["job_id"])
    assert final["state"] == "succeeded"
    assert len(final["progress"]["paper_ids"]) == 2
    assert [call[2]["cursor"] for call in app.sources.calls] == [None, "second"]


def test_source_failure_isolated_and_resume_preserves_completed_source(library):
    app = library
    app.sources = ScriptedSources({
        "alpha": {None: SourcePage(items=[candidate()])},
        "beta": {None: ResearchError("SOURCE_RATE_LIMITED", "Try later", retryable=True)},
    })
    cid = collection(app, ["alpha", "beta"])
    job = app.discover_papers(cid, "retrieval evaluation", sources=["alpha", "beta"])
    partial = app.run_worker(job["job_id"])
    assert partial["state"] == "partial"
    assert partial["progress"]["sources"]["beta"]["error"]["code"] == "SOURCE_RATE_LIMITED"
    assert app.get_collection(cid)["counts"] == {"candidate": 1}
    assert app.prepare_context("retrieval", collection_id=cid)["freshness"]["reason"] == "never_completed"
    app.sources.pages["beta"][None] = SourcePage(items=[candidate(source="beta", source_id="two", doi="10.5555/two")])
    app.resume_job(job["job_id"])
    assert app.run_worker(job["job_id"])["state"] == "succeeded"
    assert [call[0] for call in app.sources.calls].count("alpha") == 1


def test_only_matching_complete_search_updates_plan_freshness(library):
    app = library
    cid = collection(app)
    before = app.prepare_context("retrieval", collection_id=cid)
    assert before["freshness"]["reason"] == "never_completed"
    job = app.discover_papers(cid, "retrieval evaluation", sources=["alpha"])
    assert app.run_worker(job["job_id"])["state"] == "succeeded"
    fresh = app.prepare_context("retrieval", collection_id=cid)["freshness"]
    assert fresh["needs_refresh"] is False
    assert fresh["last_complete_search_at"]
    app.update_collection(cid, config={"discovery": {"query": "new direction", "sources": ["alpha"]}})
    changed = app.prepare_context("retrieval", collection_id=cid)["freshness"]
    assert changed["reason"] == "scope_changed"
    assert changed["last_complete_search_at"] is None


def test_repeated_request_id_reuses_job_and_rejects_changed_payload(library):
    cid = collection(library)
    first = library.discover_papers(cid, "retrieval", request_id="same")
    second = library.discover_papers(cid, "retrieval", request_id="same")
    assert first["job_id"] == second["job_id"]
    with pytest.raises(ResearchError) as exc:
        library.discover_papers(cid, "different", request_id="same")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_known_preprint_and_published_work_keep_distinct_versions(library):
    app = library
    cid = collection(app)
    published = add_paper(app, cid)
    preprint = app.add_paper(Candidate(source="arxiv", source_id="2501.00001v1", title=published["title"],
        identifiers={"arxiv": "2501.00001v1"}, version_kind="preprint", extra={"published_doi": published["doi"]}), cid)
    second = app.add_paper(Candidate(source="arxiv", source_id="2501.00001v2", title=published["title"],
        identifiers={"arxiv": "2501.00001v2"}, version_kind="preprint", extra={"published_doi": published["doi"]}), cid)
    assert published["paper_id"] == preprint["paper_id"] == second["paper_id"]
    assert len(second["versions"]) == 3
    assert sorted(v["kind"] for v in second["versions"]) == ["preprint", "preprint", "published"]
    assert second["preferred_version_id"] == published["preferred_version_id"]


def test_no_fulltext_url_waits_for_browser_and_claim_is_exclusive(library):
    app = library
    _, _, job = browser_waiting(app)
    ticket = app.claim_browser_task("worker-one", session_id="browser-one", job_id=job["job_id"])
    assert Path(ticket["handoff"]["download_dir"]).is_dir()
    assert get_task(app, job)["state"] == "leased"
    assert "lease_token" not in get_task(app, job)
    with pytest.raises(ResearchError) as exc:
        app.claim_browser_task("worker-two", session_id="browser-one")
    assert exc.value.code == "BROWSER_SESSION_BUSY"
    assert app.claim_browser_task("worker-two", session_id="browser-two")["task"] is None


def test_expired_browser_lease_gets_new_directory_and_rejects_old_token(library):
    app = library
    _, _, job = browser_waiting(app)
    old = app.claim_browser_task("worker-one", session_id="browser-one")
    with app.store.connect() as db:
        db.execute("UPDATE tasks SET lease_expires=0 WHERE id=?", (old["task_id"],))
    new = app.claim_browser_task("worker-two", session_id="browser-two")
    assert new["task_id"] == old["task_id"]
    assert new["lease"]["lease_id"] != old["lease"]["lease_id"]
    assert new["handoff"]["download_dir"] != old["handoff"]["download_dir"]
    with pytest.raises(ResearchError) as exc:
        app.submit_browser_result(old["task_id"], old["lease"]["token"], "old-submission", {"outcome": "not_found"})
    assert exc.value.code == "STALE_LEASE"
    assert get_task(app, job)["state"] == "leased"


def test_browser_submission_is_persistent_idempotent_and_confirmable(library):
    app = library
    _, paper, job = browser_waiting(app)
    ticket = app.claim_browser_task("worker-one")
    downloaded = pdf(Path(ticket["handoff"]["download_dir"]) / "article.pdf", extra="Original article body without an edition identifier")
    result = {"outcome": "downloaded", "file": {"kind": "local_path", "path": str(downloaded)},
              "source_url": "https://publisher.example/paper", "reported_version": "published"}
    accepted = app.submit_browser_result(ticket["task_id"], ticket["lease"]["token"], "submission-one", result)
    assert accepted["state"] == "validating"
    downloaded.unlink()  # The core must already own its own copy.
    staged = app.read_material(accepted["material_id"], view="original")["material"]
    assert Path(staged["local_path"]).is_file()
    assert app.submit_browser_result(ticket["task_id"], "already-invalid", "submission-one", result) == accepted
    with pytest.raises(ResearchError) as exc:
        app.submit_browser_result(ticket["task_id"], ticket["lease"]["token"], "submission-one", {"outcome": "not_found"})
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    validated = app.run_worker(job["job_id"])
    # A browser-reported version is not an actual established version relation.
    assert validated["state"] == "needs_user"
    app.confirm_material_identity(accepted["material_id"], version_id=paper["preferred_version_id"])
    assert app.get_job(job["job_id"])["state"] == "succeeded"


def test_browser_cannot_submit_outside_lease_directory(library, tmp_path):
    app = library
    browser_waiting(app)
    ticket = app.claim_browser_task("worker-one")
    outside = pdf(tmp_path / "outside.pdf")
    with pytest.raises(ResearchError) as exc:
        app.submit_browser_result(ticket["task_id"], ticket["lease"]["token"], "outside", {
            "outcome": "downloaded", "file": {"kind": "local_path", "path": str(outside)}})
    assert exc.value.code == "PATH_OUTSIDE_STAGING"


def test_shared_task_survives_one_job_cancel_and_last_cancel_invalidates_lease(library):
    app = library
    cid, _, first = browser_waiting(app)
    second = app.acquire_papers(cid)
    assert get_task(app, first)["task_id"] == get_task(app, second)["task_id"]
    ticket = app.claim_browser_task("worker-one")
    assert app.cancel_job(first["job_id"])["state"] == "cancelled"
    assert get_task(app, second)["state"] == "leased"
    assert app.renew_browser_lease(ticket["task_id"], ticket["lease"]["token"])
    assert app.cancel_job(second["job_id"])["state"] == "cancelled"
    assert get_task(app, second)["state"] == "cancelled"
    with pytest.raises(ResearchError) as exc:
        app.submit_browser_result(ticket["task_id"], ticket["lease"]["token"], "late", {"outcome": "not_found"})
    assert exc.value.code == "STALE_LEASE"
    assert app.get_job(first["job_id"])["state"] == "cancelled"


def test_manual_import_satisfies_waiting_task_without_another_download(library, tmp_path):
    app = library
    _, paper, job = browser_waiting(app)
    imported = app.import_material(paper["paper_id"], path=pdf(tmp_path / "manual.pdf"), confirmed=True)
    assert imported["status"] == "available"
    assert app.get_job(job["job_id"])["state"] == "succeeded"
    assert app.claim_browser_task("worker")["task"] is None


def test_not_found_ends_browser_round_and_explicit_new_request_can_retry(library):
    app = library
    cid, _, job = browser_waiting(app)
    ticket = app.claim_browser_task("worker")
    response = app.submit_browser_result(ticket["task_id"], ticket["lease"]["token"], "not-found", {"outcome": "not_found"})
    assert response["state"] == "failed"
    assert app.get_job(job["job_id"])["state"] == "failed"
    retry = app.acquire_papers(cid)
    assert get_task(app, retry)["task_id"] != ticket["task_id"]


def test_unknown_version_still_attempts_open_fulltext_then_requires_version_confirmation(library, tmp_path):
    app = library
    cid = collection(app)
    record = candidate(fulltext_urls=["https://publisher.example/article.pdf"]).model_copy(update={"version_kind": "unknown"})
    app.add_paper(record, collection_id=cid)
    content = pdf(tmp_path / "provider.pdf").read_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=content, headers={"Content-Type": "application/pdf"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        app.client = client
        job = app.acquire_papers(cid)
        final = app.run_worker(job["job_id"])
    # Discovery sources often cannot establish edition kind. That must not prevent
    # retrieving a publicly available original, but strict target satisfaction waits.
    assert sum(request.url.host == "publisher.example" for request in requests) == 1
    assert final["state"] == "needs_user"
    assert any(material["status"] == "available" for material in app.list_materials(collection_id=cid))
