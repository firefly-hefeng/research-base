import httpx

from research_base.resolvers import resolve_locations


def test_known_arxiv_version_resolves_without_search_request():
    def never(request):
        raise AssertionError('Known arXiv identifiers need no metadata search')
    with httpx.Client(transport=httpx.MockTransport(never)) as client:
        locations, diagnostics = resolve_locations(client, {'identifiers': {'arxiv': '2408.08067v2'}})
    assert locations == [{'url': 'https://arxiv.org/pdf/2408.08067v2', 'version_kind': 'preprint', 'identifiers': {'arxiv': '2408.08067v2'}}]
    assert not diagnostics


def test_doi_resolver_returns_alternate_oa_locations_without_using_model_key(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'model-secret-must-not-be-sent')
    def handle(request):
        assert 'model-secret' not in str(request.url)
        assert 'Authorization' not in request.headers
        assert request.url.host == 'api.openalex.org'
        return httpx.Response(200, json={'best_oa_location': {'pdf_url': 'https://repo.example/paper.pdf', 'version': 'acceptedVersion', 'is_oa': True}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        locations, _ = resolve_locations(client, {'doi': '10.5555/example'})
    assert locations[0]['version_kind'] == 'accepted'


def test_resolver_errors_are_isolated_and_do_not_copy_response_bodies():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text='secret-response'))) as client:
        locations, diagnostics = resolve_locations(client, {'doi': '10.5555/example'})
    assert not locations
    assert diagnostics == [{'resolver': 'openalex', 'http_status': 403}]


def test_unpaywall_only_called_with_explicit_contact_configuration(monkeypatch):
    monkeypatch.setenv('UNPAYWALL_EMAIL', 'researcher@example.org')
    calls = []
    def handle(request):
        calls.append(request.url.host)
        if request.url.host == 'api.openalex.org':
            return httpx.Response(404)
        return httpx.Response(200, json={'oa_locations': [{'url_for_pdf': 'https://repo.example/article.pdf', 'version': 'publishedVersion'}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        locations, _ = resolve_locations(client, {'doi': '10.5555/example'})
    assert calls == ['api.openalex.org', 'api.unpaywall.org']
    assert locations[0]['version_kind'] == 'published'
