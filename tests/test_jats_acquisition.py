from pathlib import Path
import json

import httpx
import pytest

from research_base.core import ResearchBase
from research_base.agent import ResearchAgent
from research_base.models import ResearchError
from research_base.resolvers import PMC_BUCKET, resolve_locations
from research_base.tools import ToolRegistry
from test_materials import OfflineSources


TITLE = "A Reproducible Study of Single Cell Perturbation Prediction"
DOI = "10.5555/jats-study"


def jats(*, doi=DOI, pmcid="PMC123.2", title=TITLE, body=True):
    return (f'<article><front><article-meta><article-id pub-id-type="doi">{doi}</article-id>'
            f'<article-id pub-id-type="pmcid">PMC123</article-id><article-id pub-id-type="pmcid-ver">{pmcid}</article-id>'
            f'<title-group><article-title>{title}</article-title></title-group><abstract><p>Abstract only is insufficient.</p></abstract>'
            '</article-meta></front>' + ('<body><sec><title>Methods</title><p>' + 'Independent perturbations and cell line holdouts were used to estimate generalization. ' * 4 +
                                      '</p></sec><sec><title>Results</title><p>Results were compared with a calibrated baseline.</p></sec></body>' if body else '') +
            '<back><ref-list><ref><mixed-citation>Unrelated DOI 10.5555/other</mixed-citation></ref></ref-list></back></article>').encode()


def handler(request):
    if request.url.host == 'pmc-oa-opendata.s3.amazonaws.com':
        if request.url.path == '/':
            return httpx.Response(200, text='<ListBucketResult><CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes></ListBucketResult>')
        if request.url.path.endswith('.json'):
            return httpx.Response(200, json={'pmcid': 'PMC123', 'doi': DOI, 'is_manuscript': True,
                                            'license_code': 'TDM', 'xml_url': 's3://pmc-oa-opendata/PMC123.2/article.xml'})
        return httpx.Response(200, content=jats())
    return httpx.Response(404)


def test_official_no_pdf_manuscript_route_preserves_xml_format_and_edition():
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        locations, diagnostics = resolve_locations(client, {'doi': DOI, 'identifiers': {'pmcid': 'PMC123'}}, phase='fallback')
    xml = next(item for item in locations if item.get('format') == 'xml')
    assert xml['url'] == PMC_BUCKET + '/PMC123.2/article.xml'
    assert xml['version_kind'] == 'accepted'
    assert xml['identifiers']['pmcid'] == 'PMC123.2'
    assert not any(item.get('outcome', '').startswith('no_available') for item in diagnostics)


def test_worker_acquires_verifies_and_reads_original_xml_without_claiming_pdf_pages(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with ResearchBase(tmp_path / 'library', source_registry=OfflineSources(), client=client) as app:
            cid = app.create_collection('Full text')['collection_id']
            paper = app.add_paper({'source': 'offline', 'source_id': DOI, 'title': TITLE, 'doi': DOI,
                                   'version_kind': 'published', 'identifiers': {'pmcid': 'PMC123'}}, collection_id=cid)
            result = app.run_worker(app.acquire_papers(cid)['job_id'])
            assert result['state'] == 'succeeded'
            material = app.list_materials(paper_id=paper['paper_id'])[0]
            assert material['format'] == 'xml' and material['status'] == 'available'
            assert Path(material['local_path']).read_bytes() == jats()
            assert Path(material['text_path']).is_file() and 'jats-v1' in material['text_path']
            assert material['details']['pagination']['physical_pages'] is False
            assert material['details']['pagination']['sections'][1] == {'page': 2, 'label': 'Methods'}
            read = app.read_material(material['material_id'], pages=[2], max_chars=40)
            assert read['offset_scope']['basis'] == 'selected_logical_sections_concatenated_in_document_order'
            assert read['truncated'] and read['continuation']['pages'] == [2]
            assert app.read_material(**read['continuation'])['pages'][0]['page_char_offset'] == 40
            assert app.get_paper(paper['paper_id'])['fulltext_available']
            tools = ToolRegistry(app, collection_id=cid)
            checks = [{'check_id': 'method', 'paper_id': paper['paper_id'], 'kind': 'method',
                       'question': 'How was generalization estimated?', 'required_role': 'main_text'}]
            sid = tools.research.begin('Single cell perturbation', cid, critical_checks=checks)['session_id']
            context = tools.call('read_research_context', {'session_id': sid, 'material_id': material['material_id'],
                                 'page': 2, 'adjacent_pages': 1})['data']
            assert context['context_selection']['pagination'] == {'kind': 'logical_sections', 'physical_pages': False}
            assert context['offset_scope']['basis'] == 'selected_logical_sections_concatenated_in_document_order'
            tools.research.submit_detail(sid, 'method', 'Used held-out perturbations and cell lines.',
                [{'read_id': context['read_id'], 'quote': 'Independent perturbations and cell line holdouts'}])
            packet = tools.research.detail_review_packet(sid, 'method')
            assert packet['anchors'][0]['packet_context']['pagination']['physical_pages'] is False
            assert all(fragment['pagination']['physical_pages'] is False for fragment in packet['observed_context']['fragments'])
            with pytest.raises(ResearchError, match='Logical XML sections'):
                tools.research.observe_visual_read(sid, material['material_id'], pages=[2], image_sha256=['a' * 64])
            assert all(receipt['source'] != 'host_observed_visual' for receipt in tools.research.check(sid)['reads'])
            def model_handler(request):
                payload = json.loads(request.content)
                if 'tools' in payload:
                    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None,
                        'tool_calls': [{'id': 'xml', 'type': 'function', 'function': {'name': 'read_material',
                            'arguments': json.dumps({'material_id': material['material_id'], 'pages': [2]})}}]}}]})
                # Budget synthesis must retain the declared unit basis, too.
                delivered = json.loads(next(m['content'] for m in payload['messages'] if m['role'] == 'tool'))['data']
                assert delivered['material']['details']['pagination'] == {'kind': 'logical_sections', 'physical_pages': False}
                return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Used logical XML methods section.'}}]})
            with httpx.Client(transport=httpx.MockTransport(model_handler)) as model_client:
                output = ResearchAgent(app, api_key='offline-test', client=model_client).run('Single cell perturbation',
                    collection_id=cid, research_mode='explore', max_steps=1)
            assert output['status'] == 'exploration_completed'


@pytest.mark.parametrize('payload', [b'<html><body>' + b'blocked ' * 50 + b'</body></html>', jats(body=False),
                                      b'<!DOCTYPE article [<!ENTITY injected "bad">]>' + jats(),
                                      ('<!DOCTYPE article [<!ENTITY injected "bad">]>' + jats().decode()).encode('utf-16'),
                                      b'<article><front>broken'])
def test_xml_download_rejects_html_abstract_only_entities_and_malformed_documents(tmp_path, payload):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))) as client:
        with ResearchBase(tmp_path / 'library', source_registry=OfflineSources(), client=client) as app:
            with pytest.raises(ResearchError) as error:
                app._download_file('https://repository.example/original.xml', tmp_path / 'article.xml')
            assert error.value.code == 'INVALID_FILE'


@pytest.mark.parametrize('payload,issue', [(jats(doi='10.5555/wrong'), 'DOI conflicts'),
                                          (jats(pmcid='PMC123.1'), 'repository identity or revision'),
                                          (jats(title='An Entirely Different Topic'), 'identity needs confirmation')])
def test_jats_identity_is_front_article_metadata_and_exact_repository_revision(tmp_path, payload, issue):
    with ResearchBase(tmp_path / 'library', source_registry=OfflineSources()) as app:
        paper = app.add_paper({'source': 'offline', 'source_id': DOI, 'title': TITLE, 'doi': DOI,
                               'version_kind': 'accepted', 'identifiers': {'pmcid': 'PMC123.2'}})
        source = tmp_path / 'article.xml'
        source.write_bytes(payload)
        material = app.import_material(paper['paper_id'], path=source, version_id=paper['preferred_version_id'])
        assert material['status'] == 'staged'
        assert any(issue in value for value in material['details']['identity_issues'])


def test_matching_doi_jats_can_satisfy_exact_accepted_version_but_not_published_target(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with ResearchBase(tmp_path / 'library', source_registry=OfflineSources(), client=client) as app:
            cid = app.create_collection('Version sensitive')['collection_id']
            accepted = app.add_paper({'source': 'offline', 'source_id': DOI, 'title': TITLE, 'doi': DOI,
                                      'version_kind': 'accepted', 'identifiers': {'pmcid': 'PMC123.2'}}, collection_id=cid)
            vid = accepted['preferred_version_id']
            result = app.run_worker(app.acquire_papers(cid, requested_versions={accepted['paper_id']: vid})['job_id'])
            assert result['state'] == 'succeeded'
            assert app.list_materials(paper_id=accepted['paper_id'])[0]['version_id'] == vid
            published_job = app.run_worker(app.acquire_papers(cid, version_policy=['published'])['job_id'])
            assert published_job['state'] != 'succeeded'
