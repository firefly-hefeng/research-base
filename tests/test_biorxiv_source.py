"""bioRxiv feeds are paged metadata scans, not a fictional text-search API."""
import json

import httpx
import pytest

from research_base.discovery_plans import frontier_query_plans
from research_base.models import ResearchError
from research_base.sources.registry import SourceRegistry


QUERY = json.dumps({"categories": ["bioinformatics"], "any": ["perturbation"], "all": []})


def record(index=1, title="Single-cell perturbation prediction", **kwargs):
    return {"doi": f"10.1101/2026.10.01.{index:06d}", "title": title, "authors": "First, A.; Second, B.",
        "abstract": "A relevant model", "date": "2026-10-05", "version": "2", "category": "bioinformatics",
        "published": "NA", "jatsxml": "https://www.biorxiv.org/content/early/2026/10/05/example.source.xml", **kwargs}


def response(items, offset=0, total=None, **message):
    return httpx.Response(200, json={"messages": [{"status": "ok", "category": "bioinformatics", "cursor": offset, "count": len(items),
        "total": len(items) if total is None else total, **message}], "collection": items})


def search(registry, query=QUERY, **kwargs):
    return registry.search_page("biorxiv", query, date_from=kwargs.pop("date_from", "2026-10-01"),
        date_to=kwargs.pop("date_to", "2026-10-31"), **kwargs)


def test_official_metadata_preserves_posted_revision_publication_and_links():
    def handler(request):
        assert request.url.path == "/details/biorxiv/2026-10-01/2026-10-31/0/json"
        assert dict(request.url.params) == {"category": "bioinformatics"}
        return response([record(published="10.1234/formal", license="cc_by")])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, SourceRegistry(client=client, throttle=False) as registry:
        page = search(registry)
        item = page.items[0]
        assert item.source == "biorxiv" and item.source_id.endswith("v2")
        assert item.version_kind == "preprint" and item.extra["archive_posted_date"] == "2026-10-05"
        assert item.extra["published_doi"] == "10.1234/formal"
        assert item.identifiers["biorxiv_revision"].endswith("v2")
        assert item.fulltext_urls[0].endswith("v2.full.pdf") and item.links[0].format == "xml"
        assert item.authors == ["First, A.", "Second, B."]
        assert page.total == 1 and page.next_cursor is None
        info = next(s for s in registry.list_sources() if s["name"] == "biorxiv")
        assert not info["default_enabled"] and info["requires_native_query"]


def test_empty_matched_page_still_continues_without_dropping_provider_records():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return response([record(i, title="Protein fragments") for i in range(30)], total=31)
        return response([record(31)], offset=30, total=31)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, SourceRegistry(client=client, throttle=False) as registry:
        first = search(registry, page_size=2)
        assert first.items == [] and first.next_cursor and first.total is None
        second = search(registry, cursor=first.next_cursor, page_size=2)
        assert len(second.items) == 1 and second.total == 1 and second.next_cursor is None
        assert calls[-1].endswith("/30/json")


def test_all_30_matches_are_returned_even_if_caller_requests_fewer():
    with httpx.Client(transport=httpx.MockTransport(lambda r: response([record(i) for i in range(30)]))) as client, SourceRegistry(client=client, throttle=False) as registry:
        assert len(search(registry, page_size=2).items) == 30


def test_actual_official_empty_day_shape_is_not_a_source_error():
    empty = httpx.Response(200, json={"messages": [{"status": "no posts found"}], "collection": []})
    with httpx.Client(transport=httpx.MockTransport(lambda r: empty)) as client, SourceRegistry(client=client, throttle=False) as registry:
        page = search(registry)
        assert page.items == [] and page.next_cursor is None and page.total == 0


def test_word_boundaries_prevent_fragment_from_matching_rag():
    query = json.dumps({"categories": ["bioinformatics"], "any": ["RAG"], "all": []})
    with httpx.Client(transport=httpx.MockTransport(lambda r: response([record(title="Protein fragment"), record(2, title="RAG evaluation")]))) as client, SourceRegistry(client=client, throttle=False) as registry:
        assert [i.title for i in search(registry, query).items] == ["RAG evaluation"]


def test_months_are_latest_first_and_empty_categories_do_not_complete_whole_scope():
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return response([], category=request.url.params["category"])
    query = json.dumps({"categories": ["bioinformatics", "genomics"], "any": ["perturbation"]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, SourceRegistry(client=client, throttle=False) as registry:
        cursor = None
        for i in range(4):
            page = search(registry, query, date_from="2026-09-15", cursor=cursor)
            assert bool(page.next_cursor) == (i < 3)
            cursor = page.next_cursor
        assert "/2026-10-01/2026-10-31/" in requests[0] and "category=bioinformatics" in requests[0]
        assert "category=genomics" in requests[1]
        assert "/2026-09-15/2026-09-30/" in requests[2]


@pytest.mark.parametrize("bad", ["plain search", '{"categories":["https://evil.invalid"],"any":["x"]}',
    '{"categories":["bioinformatics"],"url":"https://evil.invalid","any":["x"]}',
    '{"categories":["not_a_category"],"any":["x"]}',
    '{"categories":["bioinformatics"],"any":[]}'])
def test_native_query_is_controlled_and_cannot_supply_arbitrary_urls(bad):
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("Invalid query must not reach network"))) as client, SourceRegistry(client=client, throttle=False) as registry:
        with pytest.raises(ResearchError) as error:
            search(registry, bad)
        assert error.value.code == "SOURCE_INVALID_QUERY"


@pytest.mark.parametrize("change", ["query", "date", "page_size"])
def test_cursor_cannot_be_reused_for_another_scope(change):
    with httpx.Client(transport=httpx.MockTransport(lambda r: response([record(i) for i in range(30)], total=31))) as client, SourceRegistry(client=client, throttle=False) as registry:
        cursor = search(registry).next_cursor
        kwargs = {"query": json.dumps({"categories": ["genomics"], "any": ["perturbation"]})} if change == "query" else {"date_to": "2026-10-30"} if change == "date" else {"page_size": 2}
        with pytest.raises(ResearchError) as error:
            search(registry, cursor=cursor, **kwargs)
        assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("payload", [response([], total=2), response([record()], count=0), response([record()], cursor=4),
    httpx.Response(200, json={"messages": [{"status": "failed"}], "collection": []}),
    response([record(date="2026-11-01")]), response([record(version="bad")]), response([], category="all"),
    response([], interval="2026-09-01:2026-09-30")])
def test_invalid_failed_or_empty_before_total_is_not_success(payload):
    with httpx.Client(transport=httpx.MockTransport(lambda r: payload)) as client, SourceRegistry(client=client, throttle=False) as registry:
        with pytest.raises(ResearchError) as error:
            search(registry)
        assert error.value.code == "SOURCE_INVALID_RESPONSE"


def test_changed_provider_total_blocks_continuation_completion():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return response([record(i) for i in range(30)], total=31) if calls == 1 else response([record(31)], offset=30, total=32)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, SourceRegistry(client=client, throttle=False) as registry:
        cursor = search(registry).next_cursor
        with pytest.raises(ResearchError) as error:
            search(registry, cursor=cursor)
        assert error.value.code == "SOURCE_SNAPSHOT_CHANGED"


def test_suggestions_include_preprints_and_do_not_require_only_evaluation_title_terms():
    plans = frontier_query_plans("single_cell", date_from="2025-01-01", date_to="2026-11-03")
    assert {s for p in plans for s in p["sources"]} == {"pubmed", "europe_pmc", "arxiv", "biorxiv"}
    assert all(p["date_from"] == "2025-01-01" for p in plans)
    assert "virtual cell" in plans[1]["source_queries"]["arxiv"]
    rag = frontier_query_plans("rag", date_from="2025-01-01", date_to="2026-11-03")[0]
    assert "abs:diagnosing" in rag["source_queries"]["arxiv"] and "abs:evidence" in rag["source_queries"]["arxiv"]


def test_official_candidate_revision_rejects_same_doi_pdf_with_wrong_posted_date(tmp_path):
    from research_base.core import ResearchBase
    from test_materials import pdf
    with httpx.Client(transport=httpx.MockTransport(lambda request: response([record()]))) as client:
        with SourceRegistry(client=client, throttle=False) as registry:
            candidate = search(registry).items[0]
            assert candidate.extra["archive_posted_date"] == "2026-10-05"
            with ResearchBase(tmp_path / "library", source_registry=registry, client=client) as core:
                cid = core.create_collection("Revision identity regression")["collection_id"]
                paper = core.add_paper(candidate.model_dump(), cid)
                wrong = tmp_path / "wrong-revision.pdf"
                pdf(wrong, [f"{candidate.title}\n bioRxiv preprint doi: {candidate.doi}\n this version posted October 1, 2026"])
                material = core.import_material(paper["paper_id"], path=wrong,
                                                version_id=paper["preferred_version_id"])
                assert material["status"] == "staged"
                assert any("posted-date" in issue for issue in material["details"]["identity_issues"])
