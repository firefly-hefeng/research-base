import base64
import json

import httpx
import pytest

from research_base.models import Candidate, ResearchError
from research_base.scholarly import ScholarlyClient, normalized_seed, seed_match


TITLE = "A Fine-Grained Framework for Evaluating Retrieval Systems"


def work(number=1, *, title=TITLE, doi="10.5555/example", refs=None, kind="article"):
    return {"id": f"https://openalex.org/W{number}", "title": title, "doi": f"https://doi.org/{doi}" if doi else None,
            "type": kind, "publication_year": 2025, "publication_date": "2025-05-01", "authorships": [{"author": {"display_name": "Example Scientist"}}],
            "referenced_works": [f"https://openalex.org/W{r}" for r in refs or []], "best_oa_location": {"version": "publishedVersion", "landing_page_url": "https://publisher.example/article", "pdf_url": "https://repo.example/article.pdf"}}


def oa_list(items, next_cursor=None):
    return {"results": items, "meta": {"count": len(items), "next_cursor": next_cursor}}


def crossref(*, title=TITLE, doi="10.5555/example"):
    return {"status": "ok", "message": {"DOI": doi, "title": [title], "author": [{"given": "Example", "family": "Scientist"}],
                                         "type": "journal-article", "published": {"date-parts": [[2025]]}}}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_lookup_returns_actual_provider_record_not_seed_metadata():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=work(title=TITLE + " Revised"))
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        result = service.lookup({"doi": "https://doi.org/10.5555/EXAMPLE", "title": "User supplied title"})
    assert result["matched"]
    assert result["candidates"][0].title == TITLE + " Revised"
    assert result["candidates"][0].source == "openalex"
    assert service.requests_count == 1
    assert result["attempts"][0]["match_methods"] == ["doi"]


def test_missing_identifier_does_not_fabricate_a_candidate():
    with client(lambda request: httpx.Response(404)) as transport:
        result = ScholarlyClient(transport, throttle=False).lookup({"doi": "10.5555/missing"})
    assert result["matched"] is False
    assert result["candidates"] == []
    assert all(a["status"] == "not_found" for a in result["attempts"])


@pytest.mark.parametrize("status,payload,code,retryable", [
    (503, {}, "SOURCE_UNAVAILABLE", True),
    (429, {}, "SOURCE_RATE_LIMITED", True),
    (200, {"error": "temporarily unavailable"}, "SOURCE_INVALID_RESPONSE", False),
])
def test_graph_seed_source_failure_preserves_error_and_retryability(status, payload, code, retryable):
    with client(lambda request: httpx.Response(status, json=payload)) as transport:
        with pytest.raises(ResearchError) as exc:
            ScholarlyClient(transport, throttle=False, max_attempts=1).neighbors({"doi": "10.5555/example"})
    assert exc.value.code == code
    assert exc.value.retryable is retryable
    assert exc.value.details["attempts"][0]["code"] == code
    assert exc.value.details["attempts"][0]["retryable"] is retryable
    if status == 503:
        assert exc.value.details["http_status"] == 503
        assert exc.value.details["provider"] == "openalex"


def test_graph_seed_mixed_missing_and_source_failure_is_not_not_found():
    def handler(request):
        return httpx.Response(404 if request.url.path == "/works/W1" else 503)

    with client(handler) as transport:
        with pytest.raises(ResearchError) as exc:
            ScholarlyClient(transport, throttle=False, max_attempts=1).neighbors({"openalex": "W1", "doi": "10.5555/example"})
    assert exc.value.code == "SOURCE_UNAVAILABLE" and exc.value.retryable
    assert [attempt["status"] for attempt in exc.value.details["attempts"]] == ["not_found", "error"]


def test_graph_seed_valid_negative_responses_remain_not_found():
    with client(lambda request: httpx.Response(404)) as transport:
        with pytest.raises(ResearchError) as exc:
            ScholarlyClient(transport, throttle=False).neighbors({"doi": "10.5555/missing"})
    assert exc.value.code == "SEED_NOT_FOUND" and not exc.value.retryable
    assert all(attempt["status"] == "not_found" for attempt in exc.value.details["attempts"])


def test_graph_arxiv_identity_fallback_retains_rate_limit_retryability():
    def handler(request):
        return httpx.Response(429 if request.url.host == "export.arxiv.org" else 404)

    with client(handler) as transport:
        with pytest.raises(ResearchError) as exc:
            ScholarlyClient(transport, throttle=False, max_attempts=1).neighbors({"arxiv": "2408.08067"})
    assert exc.value.code == "SOURCE_RATE_LIMITED" and exc.value.retryable
    assert exc.value.details["provider"] == "arxiv"
    attempts = exc.value.details["attempts"]
    assert [attempt["status"] for attempt in attempts] == ["not_found", "error"]
    assert attempts[-1]["route"] == "arxiv_id" and attempts[-1]["retryable"] is True
    assert attempts[-1]["details"] == {"provider": "arxiv"}


def test_conflicting_doi_does_not_become_hit_even_when_title_matches():
    with client(lambda request: httpx.Response(200, json=work(doi="10.5555/different"))) as transport:
        result = ScholarlyClient(transport, throttle=False).lookup({"doi": "10.5555/example", "title": TITLE}, sources=["openalex"])
    assert not result["matched"]
    assert result["candidates"] == []


def test_title_fallback_requires_full_title_not_similar_words():
    def handle(request):
        return httpx.Response(200, json=oa_list([work(title=TITLE + " for Medical Studies"), work(2, title=TITLE.upper() + ".")]))
    with client(handle) as transport:
        result = ScholarlyClient(transport, throttle=False).lookup({"title": TITLE}, sources=["openalex"])
    assert [candidate.source_id for candidate in result["candidates"]] == ["W2"]
    assert result["attempts"][0]["match_methods"] == ["exact_title"]


def test_generic_title_or_wrong_author_cannot_identify_paper():
    with client(lambda request: httpx.Response(200, json=oa_list([work(title="Editorial")]))) as transport:
        assert not ScholarlyClient(transport, throttle=False).lookup({"title": "Editorial"}, sources=["openalex"])["matched"]
    with client(lambda request: httpx.Response(200, json=oa_list([work()]))) as transport:
        assert not ScholarlyClient(transport, throttle=False).lookup({"title": TITLE, "authors": ["Other Author"]}, sources=["openalex"])["matched"]


def test_identifier_seed_without_returned_identifiers_cannot_fallback_to_title():
    with client(lambda request: httpx.Response(200, json=oa_list([work(doi=None)]))) as transport:
        service = ScholarlyClient(transport, throttle=False)
        result = service.lookup({"doi": "10.5555/example", "title": TITLE}, sources=["openalex"])
    assert not result["matched"]


def test_provider_failure_falls_back_and_counts_real_http_attempts(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "openalex-only")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-key-never-sent")
    def handle(request):
        assert "model-key" not in str(request.url)
        assert "Authorization" not in request.headers
        if request.url.host == "api.openalex.org":
            assert request.url.params["api_key"] == "openalex-only"
            return httpx.Response(503, text="response-body-with-secret")
        assert "api_key" not in request.url.params
        return httpx.Response(200, json=crossref())
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        result = service.lookup({"doi": "10.5555/example"})
    assert service.requests_count == 3
    assert result["matched"] and result["candidates"][0].source == "crossref"
    assert result["attempts"][0]["code"] == "SOURCE_UNAVAILABLE"
    assert "secret" not in json.dumps(result["attempts"])


def test_arxiv_lookup_uses_provider_atom_and_retains_publication_relation():
    atom = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:op="http://a9.com/-/spec/opensearch/1.1/" xmlns:ax="http://arxiv.org/schemas/atom">
      <op:totalResults>1</op:totalResults><entry><id>http://arxiv.org/abs/2408.08067v1</id><title>Actual Provider Title</title><author><name>Example Scientist</name></author><published>2024-08-01</published><summary>Provider abstract</summary><ax:doi>10.5555/published</ax:doi><link title="pdf" href="https://arxiv.org/pdf/2408.08067v1"/></entry></feed>'''
    def handle(request):
        assert request.url.params["id_list"] == "2408.08067v1"
        return httpx.Response(200, content=atom)
    with client(handle) as transport:
        result = ScholarlyClient(transport, throttle=False).lookup({"arxiv": "2408.08067v1"}, sources=["arxiv"])
    record = result["candidates"][0]
    assert record.doi is None
    assert record.extra["published_doi"] == "10.5555/published"
    assert record.source_id == "2408.08067v1"


def test_arxiv_doi_normalizes_to_identifier_route():
    seed = normalized_seed({"doi": "https://doi.org/10.48550/arXiv.2408.08067"})
    assert seed["arxiv"] == "2408.08067"


def test_lookup_errors_stay_distinct_from_a_valid_empty_result():
    with client(lambda request: httpx.Response(200, json={"error": "temporarily unavailable"})) as transport:
        result = ScholarlyClient(transport, throttle=False).lookup({"doi": "10.5555/example"}, sources=["openalex"])
    assert not result["matched"]
    assert result["attempts"][0]["status"] == "error"
    assert result["attempts"][0]["code"] == "SOURCE_INVALID_RESPONSE"


def test_forward_citations_are_not_references_and_cursor_resumes(monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY', 'fixture-key')
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == "/works/W1":
            return httpx.Response(200, json=work(refs=[2, 3]))
        assert request.url.params["filter"] == "cites:W1,from_publication_date:2024-01-01"
        assert request.url.params["sort"] == "publication_date:desc"
        if request.url.params["cursor"] == "*":
            return httpx.Response(200, json=oa_list([work(4)], "provider-next"))
        assert request.url.params["cursor"] == "provider-next"
        return httpx.Response(200, json=oa_list([work(5)]))
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        first = service.neighbors({"identifiers": {"openalex": "W1"}}, date_from="2024-01-01", page_size=1)
        second = service.neighbors({"identifiers": {"openalex": "W1"}}, date_from="2024-01-01", page_size=1, cursor=first["page"].next_cursor)
    assert first["page"].items[0].source_id == "W4"
    assert second["page"].items[0].source_id == "W5"
    assert second["page"].next_cursor is None
    assert service.requests_count == 3


def test_cursor_cannot_be_used_with_a_different_seed_or_direction(monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY', 'fixture-key')
    def handle(request):
        return httpx.Response(200, json=work()) if request.url.path == "/works/W1" else httpx.Response(200, json=oa_list([work(2)], "next"))
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        first = service.neighbors({"openalex": "W1"})
        with pytest.raises(ResearchError) as error:
            service.neighbors({"openalex": "W2"}, cursor=first["page"].next_cursor)
        assert error.value.code == "INVALID_ARGUMENT"
        with pytest.raises(ResearchError):
            service.neighbors({"openalex": "W1"}, direction="references", cursor=first["page"].next_cursor)


def test_backward_references_batch_exact_ids_and_resume_without_fabrication():
    def handle(request):
        if request.url.path == "/works/W1":
            return httpx.Response(200, json=work(refs=[2, 3, 4]))
        values = request.url.params["filter"]
        if values == "openalex:W2|W3":
            return httpx.Response(200, json=oa_list([work(2), work(3)]))
        assert values == "openalex:W4"
        return httpx.Response(200, json=oa_list([work(4)]))
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        first = service.neighbors({"openalex": "W1"}, direction="references", page_size=2)
        second = service.neighbors({"openalex": "W1"}, direction="references", page_size=2, cursor=first["page"].next_cursor)
    assert [c.source_id for c in first["page"].items] == ["W2", "W3"]
    assert [c.source_id for c in second["page"].items] == ["W4"]
    assert first["page"].total == 3 and second["page"].next_cursor is None


def test_reference_metadata_change_marks_cursor_stale():
    seed_reads = 0
    def handle(request):
        nonlocal seed_reads
        if request.url.path == "/works/W1":
            seed_reads += 1
            return httpx.Response(200, json=work(refs=[2, 3] if seed_reads == 1 else [2, 3, 4]))
        return httpx.Response(200, json=oa_list([work(2)]))
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        first = service.neighbors({"openalex": "W1"}, direction="references", page_size=1)
        with pytest.raises(ResearchError) as error:
            service.neighbors({"openalex": "W1"}, direction="references", page_size=1, cursor=first["page"].next_cursor)
    assert error.value.code == "STALE_CURSOR"


def test_missing_reference_list_does_not_mean_zero_references():
    broken = work()
    broken.pop("referenced_works")
    with client(lambda request: httpx.Response(200, json=broken)) as transport:
        with pytest.raises(ResearchError) as error:
            ScholarlyClient(transport, throttle=False).neighbors({"openalex": "W1"}, direction="references")
    assert error.value.code == "SOURCE_INVALID_RESPONSE"


def test_bad_graph_list_cannot_mark_a_page_complete():
    def handle(request):
        return httpx.Response(200, json=work()) if request.url.path == "/works/W1" else httpx.Response(200, json={"error": "temporarily unavailable"})
    with client(handle) as transport:
        with pytest.raises(ResearchError) as error:
            ScholarlyClient(transport, throttle=False).neighbors({"openalex": "W1"})
    assert error.value.code == "SOURCE_INVALID_RESPONSE"


def test_reference_batch_cannot_inject_a_nonreferenced_work():
    def handle(request):
        return httpx.Response(200, json=work(refs=[2])) if request.url.path == "/works/W1" else httpx.Response(200, json=oa_list([work(999)]))
    with client(handle) as transport:
        with pytest.raises(ResearchError) as error:
            ScholarlyClient(transport, throttle=False).neighbors({"openalex": "W1"}, direction="references")
    assert error.value.code == "SOURCE_INVALID_RESPONSE"


def test_expired_provider_cursor_is_an_error_not_completion(monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY', 'fixture-key')
    def handle(request):
        if request.url.path == "/works/W1":
            return httpx.Response(200, json=work())
        if request.url.params["cursor"] == "*":
            return httpx.Response(200, json=oa_list([work(2)], "expired"))
        return httpx.Response(400)
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        first = service.neighbors({"openalex": "W1"})
        with pytest.raises(ResearchError) as error:
            service.neighbors({"openalex": "W1"}, cursor=first["page"].next_cursor)
    assert error.value.code == "STALE_CURSOR"


@pytest.mark.parametrize("seed", [{}, {"doi": "not-a-doi"}, {"arxiv": "../../other"}, {"openalex": "A1"}, {"pmid": "bad"}])
def test_invalid_seed_is_rejected_before_network(seed):
    def never(request):
        raise AssertionError("Must not issue a request")
    with client(never) as transport:
        with pytest.raises(ResearchError):
            ScholarlyClient(transport, throttle=False).lookup(seed)


def test_malformed_cursor_rejected_before_network():
    with client(lambda r: (_ for _ in ()).throw(AssertionError())) as transport:
        with pytest.raises(ResearchError):
            ScholarlyClient(transport, throttle=False).neighbors({"openalex": "W1"}, cursor="not-a-cursor")


def test_arxiv_doi_alias_matches_provider_canonical_publication_with_real_arxiv_identifier():
    record = Candidate(source='openalex', source_id='W1', title=TITLE,
                       doi='10.52202/079017-0692', identifiers={'openalex': 'W1', 'arxiv': '2408.08067'})
    assert seed_match(record, {'doi': '10.48550/arXiv.2408.08067'}) == 'arxiv'
    assert record.doi == '10.52202/079017-0692'
    assert seed_match(record, {'doi': '10.5555/unrelated', 'arxiv': '2408.08067'}) is None


def test_arxiv_doi_alias_does_not_remove_revision_constraint():
    record = Candidate(source='arxiv', source_id='2408.08067v2', title=TITLE,
                       doi='10.52202/079017-0692', identifiers={'arxiv': '2408.08067v2'})
    assert seed_match(record, {'doi': '10.48550/arXiv.2408.08067v1'}) is None
    assert seed_match(record, {'doi': '10.48550/arXiv.2408.08067v2'}) == 'arxiv'


def test_keyless_forward_citations_use_basic_pages_and_preserve_mode(monkeypatch):
    monkeypatch.delenv('OPENALEX_API_KEY', raising=False)
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == '/works/W1':
            return httpx.Response(200, json=work())
        assert 'cursor' not in request.url.params
        page = int(request.url.params['page'])
        return httpx.Response(200, json={'meta': {'count': 3}, 'results': [work(2), work(3)] if page == 1 else [work(4)]})
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        first = service.neighbors({'openalex': 'W1'}, page_size=2)
        state = service._decode_cursor(first['page'].next_cursor)
        assert state['mode'] == 'basic' and state['page'] == 2 and 'remote' not in state
        # Adding a key midway must not reinterpret the saved basic-page position.
        monkeypatch.setenv('OPENALEX_API_KEY', 'fixture-key')
        second = service.neighbors({'openalex': 'W1'}, page_size=2, cursor=first['page'].next_cursor)
    assert [r.url.params.get('page') for r in calls if r.url.path == '/works'] == ['1', '2']
    assert [p.source_id for p in second['page'].items] == ['W4']
    assert second['page'].next_cursor is None


def test_saved_authenticated_cursor_requires_key_instead_of_false_completion(monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY', 'fixture-key')
    def handle(request):
        return httpx.Response(200, json=work()) if request.url.path == '/works/W1' else httpx.Response(200, json=oa_list([work(2)], 'provider-next'))
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        first = service.neighbors({'openalex': 'W1'})
        monkeypatch.delenv('OPENALEX_API_KEY')
        with pytest.raises(ResearchError) as error:
            service.neighbors({'openalex': 'W1'}, cursor=first['page'].next_cursor)
    assert error.value.code == 'SOURCE_AUTH_REQUIRED'


def test_basic_citation_window_limit_remains_pending_not_completed(monkeypatch):
    monkeypatch.delenv('OPENALEX_API_KEY', raising=False)
    def handle(request):
        return httpx.Response(200, json=work()) if request.url.path == '/works/W1' else httpx.Response(200, json={'meta': {'count': 20000}, 'results': [work(2)]})
    with client(handle) as transport:
        service = ScholarlyClient(transport, throttle=False)
        first = service.neighbors({'openalex': 'W1'}, page_size=100)
        state = service._decode_cursor(first['page'].next_cursor)
        state['page'] = 101
        with pytest.raises(ResearchError) as error:
            service.neighbors({'openalex': 'W1'}, page_size=100, cursor=service._cursor(state))
    assert error.value.code == 'SOURCE_RESULT_LIMIT'


def test_empty_basic_page_before_provider_count_is_invalid(monkeypatch):
    monkeypatch.delenv('OPENALEX_API_KEY', raising=False)
    def handle(request):
        return httpx.Response(200, json=work()) if request.url.path == '/works/W1' else httpx.Response(200, json={'meta': {'count': 3}, 'results': []})
    with client(handle) as transport:
        with pytest.raises(ResearchError) as error:
            ScholarlyClient(transport, throttle=False).neighbors({'openalex': 'W1'}, page_size=2)
    assert error.value.code == 'SOURCE_INVALID_RESPONSE'
