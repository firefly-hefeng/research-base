from __future__ import annotations

import httpx
import pytest

from research_base.models import Candidate, ResearchError, SourcePage
from research_base.sources import SourceRegistry


def registry(handler):
    return SourceRegistry(httpx.Client(transport=httpx.MockTransport(handler)), throttle=False)


def test_openalex_normalization_dates_and_cursor(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "fixture-key")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"meta": {"count": 2, "next_cursor": "page-two"}, "results": [{
            "id": "https://openalex.org/W123", "title": "A paper", "publication_year": 2025,
            "doi": "https://doi.org/10.1234/EXAMPLE", "abstract_inverted_index": {"second": [1], "first": [0]},
            "authorships": [{"author": {"display_name": "A Scientist"}}],
            "best_oa_location": {"landing_page_url": "https://example.org/paper", "pdf_url": "https://example.org/paper.pdf",
                                 "version": "acceptedVersion"},
            "content_urls": {"pdf": "https://content.openalex.org/works/W123.pdf", "grobid_xml": "https://content.openalex.org/W123.xml"},
            "referenced_works": ["https://openalex.org/W999"]}]})

    source = registry(handler)
    page = source.search_page("openalex", "A query", date_from="2024-01-01", date_to="2026-01-01", page_size=1)
    item = page.items[0]
    assert item.doi == "10.1234/example"
    assert item.identifiers == {"openalex": "W123"}
    assert item.abstract == "first second"
    assert item.version_kind == "accepted"
    assert len(item.fulltext_urls) == 2
    assert item.links[0].format == "xml"
    assert page.next_cursor == "page-two"
    assert "from_publication_date:2024-01-01" in requests[0].url.params["filter"]
    source.search_page("openalex", "A query", cursor=page.next_cursor)
    assert requests[1].url.params["cursor"] == "page-two"
    assert source.request_count == 2


def test_openalex_keyless_paging_uses_documented_basic_pages(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    requests = []

    def handler(request):
        requests.append(request)
        number = int(request.url.params["page"])
        assert "cursor" not in request.url.params
        assert "api_key" not in request.url.params
        return httpx.Response(200, json={"meta": {"count": 2}, "results": [{"id": f"https://openalex.org/W{number}", "title": "Indexed paper"}]})

    source = registry(handler)
    first = source.search_page("openalex", "query", page_size=1)
    second = source.search_page("openalex", "query", page_size=1, cursor=first.next_cursor)
    assert [first.items[0].source_id, second.items[0].source_id] == ["W1", "W2"]
    assert second.next_cursor is None
    assert len(requests) == 2


def test_openalex_keyless_limit_and_old_remote_cursor_are_explicit(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    source = registry(lambda request: pytest.fail("No request should be issued"))
    with pytest.raises(ResearchError) as error:
        source.search_page("openalex", "query", page_size=100, cursor="oa-basic:101:100")
    assert error.value.code == "SOURCE_RESULT_LIMIT"
    with pytest.raises(ResearchError) as error:
        source.search_page("openalex", "query", cursor="old-remote-cursor")
    assert error.value.code == "SOURCE_AUTH_REQUIRED"


def test_openalex_keyless_empty_page_before_total_is_not_completion(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    source = registry(lambda request: httpx.Response(200, json={"meta": {"count": 20}, "results": []}))
    with pytest.raises(ResearchError) as error:
        source.search_page("openalex", "query", page_size=10, cursor="oa-basic:2:10")
    assert error.value.code == "SOURCE_INVALID_RESPONSE"


def test_openalex_nullable_optional_fields():
    source = registry(lambda request: httpx.Response(200, json={"meta": {"count": 1}, "results": [{"id": "https://openalex.org/W2",
        "title": "Minimal", "content_urls": None, "best_oa_location": None, "primary_location": None}]}))
    assert source.search_page("openalex", "q").items[0].fulltext_urls == []


def test_crossref_dates_abstract_and_pdf_link():
    def handler(request):
        assert request.url.params["cursor"] == "cursor-next"
        assert request.url.params["sort"] == "score"
        assert request.url.params["order"] == "desc"
        assert request.url.params["filter"] == "from-pub-date:2020-01-01,until-pub-date:2026-01-01"
        return httpx.Response(200, json={"message": {"total-results": 1, "next-cursor": "more", "items": [{
            "DOI": "10.1234/AB", "title": ["A &amp; B"], "abstract": "<jats:p>Useful abstract</jats:p>",
            "author": [{"given": "A", "family": "Scientist"}, {"name": "Team"}], "type": "journal-article",
            "published": {"date-parts": [[2025, 1, 2]]}, "link": [{"URL": "https://example.org/article.pdf", "content-type": "application/pdf"}],
            "reference": [{"DOI": "10.1234/ref"}]}]}})

    item = registry(handler).search_page("crossref", "q", cursor="cursor-next", date_from="2020-01-01", date_to="2026-01-01").items[0]
    assert item.title == "A & B"
    assert item.abstract == "Useful abstract"
    assert item.authors == ["A Scientist", "Team"]
    assert item.doi == "10.1234/ab"
    assert item.year == 2025
    assert item.version_kind == "published"
    assert item.fulltext_urls == ["https://example.org/article.pdf"]


ARXIV_XML = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:op="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom">
<op:totalResults>5</op:totalResults><entry><id>http://arxiv.org/abs/2501.00001v2</id><title>Example preprint</title>
<published>2025-01-01T00:00:00Z</published><updated>2026-10-12T00:00:00Z</updated><author><name>Example Author</name></author><summary>A preprint summary.</summary>
<arxiv:doi>10.1234/published-version</arxiv:doi><link title="pdf" type="application/pdf" href="https://arxiv.org/pdf/2501.00001v2"/>
</entry></feed>'''


def test_arxiv_version_and_published_doi_are_not_conflated():
    def handler(request):
        assert request.url.params["start"] == "2"
        assert "all:retrieval AND all:evaluation" in request.url.params["search_query"]
        assert "submittedDate:[202401010000 TO 202512312359]" in request.url.params["search_query"]
        return httpx.Response(200, content=ARXIV_XML)

    page = registry(handler).search_page("arxiv", "retrieval evaluation", cursor="2", date_from="2024-01-01", date_to="2025-12-31")
    item = page.items[0]
    assert item.source_id == "2501.00001v2"
    assert item.version_kind == "preprint"
    assert item.doi is None
    assert item.extra["published_doi"] == "10.1234/published-version"
    assert item.extra["first_submitted_at"] == "2025-01-01T00:00:00Z"
    assert item.extra["version_updated_at"] == "2026-10-12T00:00:00Z"
    assert page.next_cursor == "3"
    assert page.warnings


def test_europepmc_core_response_and_alias():
    def handler(request):
        assert request.url.params["cursorMark"] == "*"
        assert "FIRST_PDATE:[2024-01-01 TO 3000-12-31]" in request.url.params["query"]
        return httpx.Response(200, json={"hitCount": 1, "nextCursorMark": "next", "resultList": {"result": [{
            "id": "111", "source": "MED", "title": "Biology paper", "doi": "10.1234/bio", "pubYear": "2025",
            "pmcid": "PMC123", "isOpenAccess": "Y", "abstractText": "<p>Biology abstract</p>",
            "authorList": {"author": [{"fullName": "A Biologist"}]},
            "fullTextUrlList": {"fullTextUrl": [{"documentStyle": "pdf", "url": "https://example.org/bio.pdf"}]}}]}})

    page = registry(handler).search_page("europepmc", "biology", date_from="2024-01-01")
    item = page.items[0]
    assert item.source == "europe_pmc"
    assert item.identifiers["pmid"] == "111"
    assert item.identifiers["pmcid"] == "PMC123"
    assert item.fulltext_urls == ["https://example.org/bio.pdf"]
    assert any(link.format == "xml" for link in item.links)
    assert page.next_cursor == "next"


PUBMED_XML = b'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
<ArticleTitle>Study of <i>cells</i></ArticleTitle><Abstract><AbstractText Label="METHODS">A method.</AbstractText>
<AbstractText Label="RESULTS">A result.</AbstractText></Abstract><AuthorList><Author><ForeName>A</ForeName><LastName>Biologist</LastName></Author></AuthorList>
<Journal><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue></Journal><PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/CELLS</ArticleId><ArticleId IdType="pmc">PMC555</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>'''


def test_pubmed_search_fetch_and_identifiers():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("esearch.fcgi"):
            assert request.url.params["retstart"] == "0"
            assert request.url.params["datetype"] == "pdat"
            return httpx.Response(200, json={"esearchresult": {"count": "2", "idlist": ["123"]}})
        assert request.url.params["id"] == "123"
        return httpx.Response(200, content=PUBMED_XML)

    source = registry(handler)
    page = source.search_page("pubmed", "cells", date_from="2020-01-01")
    item = page.items[0]
    assert item.title == "Study of cells"
    assert item.abstract == "A method. A result."
    assert item.doi == "10.1234/cells"
    assert item.identifiers == {"pmid": "123", "pubmed": "123", "pmcid": "PMC555"}
    assert item.year == 2025
    assert page.next_cursor == "1"
    assert len(requests) == source.request_count == 2


def test_pubmed_result_cap_is_reported_not_silently_exhausted():
    with pytest.raises(ResearchError) as exc:
        registry(lambda request: pytest.fail("must not request beyond provider cap")).search_page("pubmed", "cancer", cursor="10000")
    assert exc.value.code == "SOURCE_RESULT_LIMIT"


def test_pubmed_preserves_query_semantics_and_exposes_ncbi_translation():
    query = 'retrieval augmented generation'
    translated = '("retrieval"[All Fields] AND "augmented"[All Fields]) OR "generation"[MeSH Terms]'

    def handler(request):
        assert request.url.params["term"] == query
        return httpx.Response(200, json={"esearchresult": {
            "count": "0", "idlist": [], "querytranslation": translated,
            "warninglist": {"phrasesignored": ["retrieval augmented generation"]}}})

    page = registry(handler).search_page("pubmed", query)
    assert page.items == []
    assert any(translated in warning for warning in page.warnings)
    assert any("phrasesignored" in warning for warning in page.warnings)


def test_semantic_scholar_bulk_preserves_all_results_and_token():
    def handler(request):
        assert request.url.params["token"] == "opaque"
        assert request.url.params["publicationDateOrYear"] == "2024-01-01:"
        assert "limit" not in request.url.params
        return httpx.Response(200, json={"total": 20, "token": "next", "data": [
            {"paperId": "abc", "title": "One", "year": 2025, "authors": [{"name": "A"}],
             "externalIds": {"DOI": "10.1234/ONE", "ArXiv": "2501.00001", "PubMed": "123"},
             "openAccessPdf": {"url": "https://example.org/one.pdf"}},
            {"paperId": "def", "title": "Two", "openAccessPdf": None, "externalIds": None}]})

    page = registry(handler).search_page("semantic_scholar", "q", cursor="opaque", page_size=1, date_from="2024-01-01")
    assert len(page.items) == 2  # Provider batches must not be sliced and lost.
    assert page.items[0].doi == "10.1234/one"
    assert page.items[0].identifiers["arxiv"] == "2501.00001"
    assert page.next_cursor == "next"
    assert page.warnings


@pytest.mark.parametrize("name", ["openalex", "crossref", "arxiv", "europe_pmc", "pubmed", "semantic_scholar"])
def test_source_http_errors_are_sanitized(name, monkeypatch):
    secret = "test-secret-not-for-output"
    monkeypatch.setenv("OPENALEX_API_KEY", secret)
    monkeypatch.setenv("NCBI_API_KEY", secret)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", secret)
    source = registry(lambda request: httpx.Response(403, text=secret))
    with pytest.raises(ResearchError) as exc:
        source.search_page(name, "q")
    assert secret not in str(exc.value.to_dict())
    assert exc.value.details["http_status"] == 403


def test_rate_limit_has_retry_after_and_no_retry_loop():
    source = registry(lambda request: httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(ResearchError) as exc:
        source.search_page("openalex", "q")
    assert exc.value.code == "SOURCE_RATE_LIMITED"
    assert exc.value.details["retry_after"] == "120"
    assert source.request_count == 1


def test_malformed_xml_has_domain_error():
    with pytest.raises(ResearchError) as exc:
        registry(lambda request: httpx.Response(200, text="not xml")).search_page("arxiv", "q")
    assert exc.value.code == "SOURCE_INVALID_RESPONSE"


def test_registry_extensions_and_validation():
    class Custom:
        info = {"label": "Custom", "scope": "test"}

        def search(self, query, **kwargs):
            return SourcePage(items=[Candidate(source="custom", source_id="1", title=query)])

    source = registry(lambda request: pytest.fail("custom adapter has no network"))
    assert len(source.list_sources()) == 10
    assert next(item for item in source.list_sources() if item["name"] == "acl_anthology")["default_enabled"] is False
    source.register("custom", Custom())
    assert source.search_page("custom", "A query").items[0].title == "A query"
    for kwargs in [{"page_size": 0}, {"date_from": "tomorrow"}, {"date_from": "2026-01-01", "date_to": "2025-01-01"}]:
        with pytest.raises(ResearchError) as exc:
            source.search_page("custom", "q", **kwargs)
        assert exc.value.code == "INVALID_ARGUMENT"


def test_doaj_multilingual_journal_article_dates_and_pagination():
    from urllib.parse import unquote

    def handler(request):
        assert request.url.params["page"] == "2"
        assert request.url.params["pageSize"] == "50"  # Official v4 schema limit.
        query = unquote(request.url.raw_path.decode().split("?", 1)[0])
        assert "bibjson.year:[2024 TO 2026]" in query
        return httpx.Response(200, json={"total": 150, "page": 2, "results": [{
            "id": "article-one", "bibjson": {"title": "Community health research", "abstract": "A study.",
                "year": "2025", "author": [{"name": "A Researcher"}],
                "identifier": [{"type": "doi", "id": "10.1234/community"}],
                "journal": {"title": "Regional Health Journal", "country": "NG", "language": ["EN", "FR"]},
                "link": [{"type": "fulltext", "content_type": "PDF", "url": "https://example.org/article.pdf"},
                         {"type": "fulltext", "content_type": "HTML", "url": "https://example.org/article"}]}}]})

    page = registry(handler).search_page("doaj", "community", cursor="2", page_size=100,
                                         date_from="2024-03-01", date_to="2026-06-30")
    item = page.items[0]
    assert page.next_cursor == "3"
    assert item.version_kind == "published"
    assert item.extra["resource_kind"] == "publication"
    assert item.extra["journal_country"] == "NG"
    assert item.extra["languages"] == ["EN", "FR"]
    assert item.fulltext_urls == ["https://example.org/article.pdf"]
    assert len(item.links) == 2
    assert any("publication year" in warning for warning in page.warnings)


def test_doaj_last_page_is_terminal_and_zero_cursor_is_invalid():
    source = registry(lambda request: httpx.Response(200, json={"total": 0, "results": []}))
    assert source.search_page("doaj", "no match").next_cursor is None
    with pytest.raises(ResearchError) as exc:
        source.search_page("doaj", "q", cursor="0")
    assert exc.value.code == "INVALID_ARGUMENT"


def test_datacite_publications_filter_excludes_dataset_and_keeps_preprint_doi():
    def handler(request):
        assert "text" in request.url.params["resource-type-id"]
        assert "dataset" not in request.url.params["resource-type-id"]
        assert request.url.params["sort"] == "relevance"
        assert request.url.params["page[number]"] == "2"
        assert "publicationYear:[2024 TO 2026]" in request.url.params["query"]
        return httpx.Response(200, json={"meta": {"total": 20}, "links": {
            "next": "https://api.datacite.org/dois?page%5Bnumber%5D=3&page%5Bsize%5D=2"}, "data": [
                {"id": "10.48550/arxiv.2501.00001", "attributes": {"doi": "10.48550/arxiv.2501.00001",
                    "types": {"resourceTypeGeneral": "Text", "resourceType": "Article"},
                    "titles": [{"title": "Example Preprint"}], "publicationYear": 2025,
                    "creators": [{"givenName": "A", "familyName": "Scientist"}],
                    "descriptions": [{"descriptionType": "Abstract", "description": "Abstract text."}],
                    "url": "https://arxiv.org/abs/2501.00001"}},
                {"id": "10.5281/zenodo.123", "attributes": {"types": {"resourceTypeGeneral": "Dataset"},
                    "titles": [{"title": "A Dataset That Must Not Become a Paper"}]}}]})

    page = registry(handler).search_page("datacite", "retrieval", cursor="2", page_size=2,
                                         date_from="2024-03-01", date_to="2026-06-30")
    assert len(page.items) == 1
    item = page.items[0]
    assert item.version_kind == "preprint"
    assert item.doi == "10.48550/arxiv.2501.00001"
    assert item.identifiers["arxiv"] == "2501.00001"
    assert item.fulltext_urls == ["https://arxiv.org/pdf/2501.00001"]
    assert item.extra["resource_kind"] == "publication"
    assert page.next_cursor == "3"
    assert any("Excluded 1" in warning for warning in page.warnings)


def test_datacite_limits_and_nonarxiv_publication_content_url():
    def handler(request):
        return httpx.Response(200, json={"meta": {"total": 1}, "links": {"next": None}, "data": [{
            "id": "10.1234/report", "attributes": {"types": {"resourceTypeGeneral": "Report"},
                "titles": [{"title": "A scientific report"}], "publicationYear": 2025,
                "creators": [{"name": "Research Team"}], "contentUrl": ["https://example.org/report.pdf"]}}]})

    source = registry(handler)
    page = source.search_page("datacite", "q")
    assert page.next_cursor is None
    assert page.items[0].fulltext_urls == ["https://example.org/report.pdf"]
    assert page.items[0].version_kind == "unknown"
    with pytest.raises(ResearchError) as exc:
        source.search_page("datacite", "q", cursor="10001", page_size=1)
    assert exc.value.code == "SOURCE_RESULT_LIMIT"


@pytest.mark.parametrize("name", ["openalex", "crossref", "arxiv", "europe_pmc", "pubmed", "semantic_scholar", "doaj", "datacite"])
def test_sources_do_not_treat_error_payload_as_exhausted(name):
    source = registry(lambda request: httpx.Response(200, json={"error": "temporarily unavailable"}))
    with pytest.raises(ResearchError) as exc:
        source.search_page(name, "q")
    assert exc.value.code == "SOURCE_INVALID_RESPONSE"


@pytest.mark.parametrize("name,payload", [
    ("openalex", {}),
    ("openalex", {"results": []}),
    ("openalex", {"results": None, "meta": {"count": 0}}),
    ("openalex", {"results": [], "meta": {"count": -1}}),
    ("crossref", {"message": {}}),
    ("crossref", {"status": "error", "message": {"items": [], "total-results": 0}}),
    ("crossref", {"message": {"items": [], "total-results": 0, "error": "unavailable"}}),
    ("crossref", {"message": {"items": {}, "total-results": 0}}),
    ("europe_pmc", {"hitCount": 0}),
    ("europe_pmc", {"hitCount": 1, "resultList": {}}),
    ("europe_pmc", {"hitCount": True, "resultList": {"result": []}}),
    ("pubmed", {}),
    ("pubmed", {"esearchresult": {"count": "0"}}),
    ("pubmed", {"esearchresult": {"count": "0", "idlist": [], "errorlist": {"fieldnotfound": ["invalid"]}}}),
    ("pubmed", {"esearchresult": {"count": "not-an-integer", "idlist": []}}),
    ("semantic_scholar", {"data": []}),
    ("semantic_scholar", {"data": None, "total": 0}),
    ("semantic_scholar", {"data": [], "total": 0, "errors": ["unavailable"]}),
])
def test_missing_or_invalid_required_envelopes_are_errors(name, payload):
    with pytest.raises(ResearchError) as exc:
        registry(lambda request: httpx.Response(200, json=payload)).search_page(name, "q")
    assert exc.value.code == "SOURCE_INVALID_RESPONSE"


@pytest.mark.parametrize("name,payload", [
    ("openalex", {"meta": {"count": 0, "next_cursor": None}, "results": []}),
    ("crossref", {"status": "ok", "message": {"items": [], "total-results": 0}}),
    ("europe_pmc", {"hitCount": 0, "resultList": {"result": []}}),
    ("europe_pmc", {"hitCount": 0, "resultList": {}}),
    ("pubmed", {"esearchresult": {"count": "0", "idlist": []}}),
    ("semantic_scholar", {"total": 0, "data": []}),
    ("doaj", {"total": 0, "results": []}),
    ("datacite", {"meta": {"total": 0}, "data": [], "links": {"next": None}}),
])
def test_explicit_valid_zero_results_are_still_terminal(name, payload):
    page = registry(lambda request: httpx.Response(200, json=payload)).search_page(name, "q")
    assert page.items == []
    assert page.next_cursor is None
    assert page.total == 0


def test_arxiv_success_status_with_wrong_xml_envelope_is_not_empty_completion():
    with pytest.raises(ResearchError) as exc:
        registry(lambda request: httpx.Response(200, text="<error>Unavailable</error>")).search_page("arxiv", "q")
    assert exc.value.code == "SOURCE_INVALID_RESPONSE"


def test_pubmed_efetch_error_cannot_drop_searched_identifiers():
    def handler(request):
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": ["123"]}})
        return httpx.Response(200, text="<eFetchResult><ERROR>Unavailable</ERROR></eFetchResult>")

    with pytest.raises(ResearchError) as exc:
        registry(handler).search_page("pubmed", "q")
    assert exc.value.code == "SOURCE_INVALID_RESPONSE"


@pytest.mark.parametrize("returned_ids", [["123"], ["123", "999"], ["123", "123"]])
def test_pubmed_efetch_requires_exact_requested_pmid_set(returned_ids):
    def handler(request):
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"count": "2", "idlist": ["123", "124"]}})
        articles = "".join(f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>Indexed paper {pmid}</ArticleTitle></Article></MedlineCitation></PubmedArticle>" for pmid in returned_ids)
        return httpx.Response(200, text="<PubmedArticleSet>" + articles + "</PubmedArticleSet>")

    with pytest.raises(ResearchError) as exc:
        registry(handler).search_page("pubmed", "q")
    assert exc.value.code == "SOURCE_INVALID_RESPONSE"
    assert exc.value.retryable
    assert exc.value.details["missing_pmids"] == ["124"]


def test_pubmed_partial_efetch_cannot_refresh_coverage_and_resume_refetches_page(tmp_path):
    from research_base.core import ResearchBase

    complete = False
    fetched = []

    def handler(request):
        if request.url.path.endswith("esearch.fcgi"):
            assert request.url.params["retstart"] == "0"
            return httpx.Response(200, json={"esearchresult": {"count": "2", "idlist": ["123", "124"]}})
        fetched.append(request.url.params["id"])
        returned_ids = ["123", "124"] if complete else ["123"]
        articles = "".join(f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>Indexed paper {pmid}</ArticleTitle></Article></MedlineCitation></PubmedArticle>" for pmid in returned_ids)
        return httpx.Response(200, text="<PubmedArticleSet>" + articles + "</PubmedArticleSet>")

    source = registry(handler)
    with ResearchBase(tmp_path / "partial-fetch", source_registry=source, client=source.transport.client) as base:
        cid = base.create_collection("Coverage", config={"discovery": {"query": "indexed", "sources": ["pubmed"]}})["collection_id"]
        job = base.discover_papers(cid, "indexed", sources=["pubmed"])
        failed = base.run_worker(job["job_id"])
        assert failed["state"] == "failed"
        assert failed["progress"]["sources"]["pubmed"]["complete"] is False
        assert base.prepare_context("indexed", collection_id=cid)["freshness"]["needs_refresh"] is True
        with base.store.connect() as db:
            assert db.execute("SELECT count(*) FROM search_runs").fetchone()[0] == 0
        complete = True
        base.resume_job(job["job_id"])
        final = base.run_worker(job["job_id"])
        assert final["state"] == "succeeded"
        assert final["progress"]["sources"]["pubmed"]["records"] == 2
        assert fetched == ["123,124", "123,124"]
        assert base.prepare_context("indexed", collection_id=cid)["freshness"]["needs_refresh"] is False


def test_pubmed_empty_esearch_before_total_cannot_complete():
    with pytest.raises(ResearchError) as exc:
        registry(lambda request: httpx.Response(200, json={"esearchresult": {"count": "2", "idlist": []}})).search_page("pubmed", "q")
    assert exc.value.code == "SOURCE_INVALID_RESPONSE"
    assert exc.value.retryable
