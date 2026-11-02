from __future__ import annotations

import json

import httpx
import pytest

from research_base.models import ResearchError
from research_base.resolvers import METADATA_LIMIT, PMC_BUCKET, resolve_locations


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def html(doi='10.5555/article', pdf='/files/main.pdf', title='A Research Study'):
    return f'<html><meta name="citation_doi" content="{doi}"><meta name="citation_title" content="{title}"><meta name="citation_pdf_url" content="{pdf}"></html>'


def test_fallback_uses_observed_highwire_pdf_and_keeps_edition_unknown():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=html())
    with client_for(handle) as client:
        locations, _ = resolve_locations(client, {'doi': '10.5555/article', 'landing_url': 'https://publisher.example/article'}, phase='fallback')
    item = next(x for x in locations if x['url'] == 'https://publisher.example/files/main.pdf')
    assert item['doi'] == '10.5555/article'
    assert item['version_kind'] == 'unknown'
    assert not any('api.openalex.org' in call for call in calls)


def test_fallback_accepts_explicit_alternate_pdf_link():
    body = '<meta name="citation_doi" content="10.5555/article"><link rel="alternate" type="application/pdf" href="https://repo.example/original.pdf">'
    with client_for(lambda request: httpx.Response(200, text=body)) as client:
        locations, _ = resolve_locations(client, {'doi': '10.5555/article', 'landing_url': 'https://publisher.example/article'}, phase='fallback')
    assert any(item['url'] == 'https://repo.example/original.pdf' for item in locations)


@pytest.mark.parametrize('body', [html(doi='10.5555/other'), '<meta name="citation_pdf_url" content="/unrelated.pdf">'])
def test_landing_pdf_requires_matching_identity(body):
    with client_for(lambda request: httpx.Response(200, text=body)) as client:
        locations, notes = resolve_locations(client, {'doi': '10.5555/article', 'landing_url': 'https://publisher.example/article'}, phase='fallback')
    assert not locations
    assert any(n.get('outcome') == 'identity_not_confirmed' for n in notes)


@pytest.mark.parametrize('url', ['https://127.0.0.1/private.pdf', 'https://localhost/private.pdf', 'https://10.0.0.2/private.pdf', 'javascript:fetch(1)', 'file:///secrets.pdf'])
def test_embedded_private_and_non_http_locations_are_not_returned(url):
    with client_for(lambda request: httpx.Response(200, text=html(pdf=url))) as client:
        locations, _ = resolve_locations(client, {'doi': '10.5555/article', 'landing_url': 'https://publisher.example/article'}, phase='fallback')
    assert not locations


def test_biorxiv_relation_accepts_current_field_and_retains_preprint_identity():
    formal, preprint = '10.1016/j.cell.2026.07.052', '10.1101/2025.06.26.661135'
    def handle(request):
        if request.url.host == 'api.biorxiv.org':
            return httpx.Response(200, json={'messages': [{'status': 'ok'}], 'collection': [{'preprint_doi': preprint, 'published_doi': formal, 'preprint_title': 'State study'}]})
        if request.url.host == 'doi.org' and preprint in str(request.url):
            return httpx.Response(200, text=html(doi=preprint, pdf='https://www.biorxiv.org/content/actual-version.full.pdf'))
        return httpx.Response(404)
    with client_for(handle) as client:
        locations, notes = resolve_locations(client, {'doi': formal, 'identifiers': {'pmid': '42673963'}}, phase='fallback')
    item = next(x for x in locations if x.get('related_published_doi'))
    assert item['doi'] == preprint
    assert item['version_kind'] == 'preprint'
    assert item['related_published_doi'] == formal
    assert any(x.get('outcome') == 'explicit_version_relation' for x in notes)


def test_biorxiv_relation_is_preserved_even_when_browser_is_needed_for_pdf():
    formal, preprint = '10.1016/j.cell.2026.07.052', '10.1101/2025.06.26.661135'
    def handle(request):
        if request.url.host == 'api.biorxiv.org':
            return httpx.Response(200, json={'messages': [{'status': 'ok'}], 'collection': [{'biorxiv_doi': preprint, 'published_doi': formal}]})
        return httpx.Response(403)
    with client_for(handle) as client:
        locations, notes = resolve_locations(client, {'doi': formal, 'identifiers': {'pmid': '42673963'}}, phase='fallback')
    assert not locations
    relation = next(x for x in notes if x.get('outcome') == 'explicit_version_relation')
    assert relation['preprint_doi'] == preprint
    assert relation['landing_url'] == 'https://doi.org/' + preprint


def test_biorxiv_does_not_link_a_different_published_doi():
    def handle(request):
        if request.url.host == 'api.biorxiv.org':
            return httpx.Response(200, json={'messages': [], 'collection': [{'preprint_doi': '10.1101/2025.06.26.661135', 'published_doi': '10.5555/unrelated'}]})
        return httpx.Response(404)
    with client_for(handle) as client:
        locations, notes = resolve_locations(client, {'doi': '10.5555/target', 'identifiers': {'pmid': '123'}}, phase='fallback')
    assert not locations
    assert not any(x.get('outcome') == 'explicit_version_relation' for x in notes)


def test_known_biorxiv_doi_uses_page_metadata_without_pdf_suffix_guessing():
    preprint = '10.1101/2024.12.23.630036'
    body = html(doi=preprint, pdf='https://www.biorxiv.org/content/actual-returned-route.pdf')
    with client_for(lambda request: httpx.Response(200, text=body)) as client:
        locations, _ = resolve_locations(client, {'doi': preprint}, phase='fallback')
    assert any(x['url'].endswith('/actual-returned-route.pdf') and x['version_kind'] == 'preprint' for x in locations)


def test_current_pmc_cloud_uses_metadata_and_does_not_guess_web_pdf():
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if request.url.host == 'pmc-oa-opendata.s3.amazonaws.com' and request.url.path == '/':
            return httpx.Response(200, text='<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes><CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes></ListBucketResult>')
        if request.url.path.endswith('PMC123.1.json'):
            return httpx.Response(200, json={'pmcid': 'PMC123', 'doi': '10.5555/article', 'title': 'A Study', 'is_manuscript': 'no', 'pdf_url': 's3://pmc-oa-opendata/PMC123.1/final.pdf?md5=abc'})
        if request.url.path.endswith('PMC123.2.json'):
            return httpx.Response(200, json={'pmcid': 'PMC123', 'doi': '10.5555/article', 'is_manuscript': 'yes', 'pdf_url': 'https://pmc-oa-opendata.s3.amazonaws.com/PMC123.2/manuscript.pdf'})
        return httpx.Response(404)
    with client_for(handle) as client:
        locations, _ = resolve_locations(client, {'doi': '10.5555/article', 'identifiers': {'pmcid': 'PMC123'}}, phase='fallback')
    cloud = [x for x in locations if x.get('resolver') == 'pmc_cloud']
    assert {x['version_kind'] for x in cloud} == {'published', 'accepted'}
    assert all(x['doi'] == '10.5555/article' for x in cloud)
    assert any(x['url'].endswith('final.pdf?md5=abc') for x in cloud)
    assert not any('oa.fcgi' in call or '/articles/PMC123/pdf' in call for call in calls)


def test_pmc_converter_and_cloud_preserve_explicit_article_identity():
    def handle(request):
        if request.url.host == 'pmc.ncbi.nlm.nih.gov':
            assert request.url.params['idtype'] == 'pmid'
            return httpx.Response(200, json={'status': 'ok', 'records': [{'pmid': '123', 'pmcid': 'PMC999', 'doi': '10.5555/article'}]})
        if request.url.host == 'pmc-oa-opendata.s3.amazonaws.com' and request.url.path == '/':
            return httpx.Response(200, text='<ListBucketResult><CommonPrefixes><Prefix>PMC999.1/</Prefix></CommonPrefixes></ListBucketResult>')
        if request.url.path.endswith('PMC999.1.json'):
            return httpx.Response(200, json={'pmcid': 'PMC999', 'doi': '10.5555/article', 'is_manuscript': 'no', 'pdf_url': PMC_BUCKET + '/PMC999.1/paper.pdf'})
        return httpx.Response(404)
    with client_for(handle) as client:
        locations, _ = resolve_locations(client, {'doi': '10.5555/article', 'identifiers': {'pmid': '123'}}, phase='fallback')
    assert any(x.get('identifiers', {}).get('pmcid') == 'PMC999.1' for x in locations)


def test_pmc_wrong_doi_is_not_used_even_if_pmcid_matches():
    def handle(request):
        if request.url.path == '/':
            return httpx.Response(200, text='<ListBucketResult><CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes></ListBucketResult>')
        return httpx.Response(200, json={'pmcid': 'PMC123', 'doi': '10.5555/other', 'pdf_url': PMC_BUCKET + '/other.pdf'})
    with client_for(handle) as client:
        locations, notes = resolve_locations(client, {'doi': '10.5555/article', 'identifiers': {'pmcid': 'PMC123'}}, phase='fallback')
    assert not locations
    assert any(x.get('outcome') == 'doi_mismatch' for x in notes)


def test_oversized_html_and_malformed_cloud_are_isolated():
    def handle(request):
        if request.url.host == 'pmc-oa-opendata.s3.amazonaws.com':
            return httpx.Response(200, text='<Error><Code>Unavailable</Code></Error>')
        return httpx.Response(200, content=b'x' * (METADATA_LIMIT + 1))
    with client_for(handle) as client:
        locations, notes = resolve_locations(client, {'doi': '10.5555/article', 'landing_url': 'https://publisher.example/article', 'identifiers': {'pmcid': 'PMC123'}}, phase='fallback')
    assert not locations
    assert any(x.get('resolver') == 'pmc_cloud' and x.get('error') for x in notes)
    assert any(x.get('resolver') == 'landing_metadata' and x.get('error') for x in notes)


def test_invalid_phase_is_an_explicit_error():
    with client_for(lambda request: httpx.Response(404)) as client:
        with pytest.raises(ResearchError) as error:
            resolve_locations(client, {}, phase='not-implemented')
    assert error.value.code == 'INVALID_ARGUMENT'


def test_identical_route_can_gain_explicit_repository_edition_metadata():
    url = 'https://repo.example/paper.pdf'
    def handle(request):
        return httpx.Response(200, json={'content_urls': {'pdf': url}, 'best_oa_location': {'pdf_url': url, 'version': 'publishedVersion', 'is_oa': True}})
    with client_for(handle) as client:
        locations, _ = resolve_locations(client, {'doi': '10.5555/article'})
    assert locations == [{'url': url, 'version_kind': 'published'}]


def test_exact_arxiv_resolver_route_satisfies_the_requested_version(tmp_path):
    from research_base.core import ResearchBase
    from test_materials import OfflineSources, pdf
    title, arxiv = 'Evaluating Retrieval Quality in Research Systems', '2601.01234v1'
    source = tmp_path / 'exact.pdf'
    pdf(source, [title + ' arXiv:' + arxiv])
    with client_for(lambda r: httpx.Response(200, content=source.read_bytes()) if r.url.host == 'arxiv.org' else httpx.Response(404)) as client:
        with ResearchBase(tmp_path / 'library', source_registry=OfflineSources(), client=client) as app:
            cid = app.create_collection('Exact version')['collection_id']
            record = app.add_paper({'source': 'arxiv', 'source_id': arxiv, 'title': title,
                                    'identifiers': {'arxiv': arxiv}, 'version_kind': 'preprint'}, collection_id=cid)
            vid = record['preferred_version_id']
            result = app.run_worker(app.acquire_papers(cid, requested_versions={record['paper_id']: vid})['job_id'])
            assert result['state'] == 'succeeded'
            material = app.list_materials(paper_id=record['paper_id'])[0]
            assert material['status'] == 'available' and material['version_id'] == vid


@pytest.mark.parametrize('provider', ['openalex', 'unpaywall'])
def test_response_verified_doi_route_satisfies_requested_publication(tmp_path, monkeypatch, provider):
    from research_base.core import ResearchBase
    from test_materials import OfflineSources, pdf
    monkeypatch.setenv('UNPAYWALL_EMAIL', 'researcher@example.org')
    title, doi = 'Evaluating Retrieval Quality in Research Systems', '10.5555/article'
    source = tmp_path / 'publication.pdf'
    pdf(source, [title + ' ' + doi])
    def handle(request):
        if request.url.host == 'api.openalex.org':
            return httpx.Response(200, json={'doi': 'https://doi.org/' + doi, 'best_oa_location': {'pdf_url': 'https://repo.example/paper.pdf', 'is_oa': True, 'version': 'publishedVersion'}}) if provider == 'openalex' else httpx.Response(404)
        if request.url.host == 'api.unpaywall.org':
            return httpx.Response(200, json={'doi': doi, 'oa_locations': [{'url_for_pdf': 'https://repo.example/paper.pdf', 'version': 'publishedVersion'}]})
        return httpx.Response(200, content=source.read_bytes()) if request.url.host == 'repo.example' else httpx.Response(404)
    with client_for(handle) as client:
        with ResearchBase(tmp_path / 'library', source_registry=OfflineSources(), client=client) as app:
            cid = app.create_collection('Exact publication')['collection_id']
            record = app.add_paper({'source': 'crossref', 'source_id': doi, 'doi': doi, 'title': title, 'version_kind': 'published'}, collection_id=cid)
            vid = record['preferred_version_id']
            result = app.run_worker(app.acquire_papers(cid, requested_versions={record['paper_id']: vid})['job_id'])
            assert result['state'] == 'succeeded'
            assert app.list_materials(paper_id=record['paper_id'])[0]['version_id'] == vid


def test_second_provider_can_verify_identity_of_already_classified_route(monkeypatch):
    monkeypatch.setenv('UNPAYWALL_EMAIL', 'researcher@example.org')
    doi, url = '10.5555/article', 'https://repo.example/paper.pdf'
    def handle(request):
        if request.url.host == 'api.openalex.org':
            return httpx.Response(200, json={'best_oa_location': {'pdf_url': url, 'is_oa': True, 'version': 'publishedVersion'}})
        return httpx.Response(200, json={'doi': doi, 'oa_locations': [{'url_for_pdf': url, 'version': 'publishedVersion'}]})
    with client_for(handle) as client:
        locations, _ = resolve_locations(client, {'doi': doi})
    assert locations == [{'url': url, 'version_kind': 'published', 'doi': doi, 'identifiers': {'doi': doi}}]


@pytest.mark.parametrize('provider', ['openalex', 'unpaywall'])
def test_doi_resolver_does_not_promote_conflicting_response_identity(monkeypatch, provider):
    monkeypatch.setenv('UNPAYWALL_EMAIL', 'researcher@example.org')
    def handle(request):
        if request.url.host == 'api.openalex.org':
            return httpx.Response(200, json={'doi': '10.5555/wrong', 'best_oa_location': {'pdf_url': 'https://wrong.example/paper.pdf', 'is_oa': True, 'version': 'publishedVersion'}}) if provider == 'openalex' else httpx.Response(404)
        return httpx.Response(200, json={'doi': '10.5555/wrong', 'oa_locations': [{'url_for_pdf': 'https://wrong.example/paper.pdf', 'version': 'publishedVersion'}]}) if provider == 'unpaywall' else httpx.Response(404)
    with client_for(handle) as client:
        locations, notes = resolve_locations(client, {'doi': '10.5555/article'})
    assert not locations
    assert any(note.get('resolver') == provider and note.get('error') for note in notes)


@pytest.mark.parametrize('pmcid,expected', [('PMC123', ['PMC123.4', 'PMC123.3', 'PMC123.2']), ('PMC123.1', ['PMC123.1'])])
def test_pmc_bounded_scan_prefers_latest_unless_exact_repository_version_requested(pmcid, expected):
    metadata_calls = []
    def handle(request):
        if request.url.host != 'pmc-oa-opendata.s3.amazonaws.com':
            return httpx.Response(404)
        if request.url.path == '/':
            return httpx.Response(200, text='<ListBucketResult>' + ''.join('<CommonPrefixes><Prefix>PMC123.' + str(n) + '/</Prefix></CommonPrefixes>' for n in [1, 3, 2, 4]) + '</ListBucketResult>')
        version = request.url.path.rsplit('/', 1)[-1].removesuffix('.json')
        metadata_calls.append(version)
        return httpx.Response(200, json={'pmcid': 'PMC123', 'doi': '10.5555/article', 'is_manuscript': 'no', 'pdf_url': PMC_BUCKET + '/' + version + '/paper.pdf'}) if version in {'PMC123.4', 'PMC123.1'} else httpx.Response(404)
    with client_for(handle) as client:
        locations, notes = resolve_locations(client, {'doi': '10.5555/article', 'identifiers': {'pmcid': pmcid}}, phase='fallback')
    assert metadata_calls == expected
    assert locations[0]['identifiers']['pmcid'] == expected[0]
    if pmcid == 'PMC123':
        assert any(note.get('outcome') == 'version_window_limited' and note['version_count'] == 4 for note in notes)
