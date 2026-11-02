"""Small official-schema fixtures for ACL metadata and snapshot pagination."""
import hashlib
import json

import httpx
import pytest

from research_base.models import ResearchError
from research_base.sources.acl import AclAnthology
from research_base.sources.registry import Transport


def xml(collection="2026.emnlp", *, month="October", papers=None):
    papers = papers if papers is not None else [
        '<paper id="1"><title><fixed-case>RAG</fixed-case> Evaluation</title><author><first>Jane</first><last>Doe</last><affiliation>Not A Name</affiliation></author><abstract>Retrieval augmented generation and calibration.</abstract><pdf hash="a1"/><doi>10.18653/2026.emnlp-main.1</doi></paper>',
        '<paper id="2"><title>Noise in RAG</title><author><first>John</first><last>Doe</last></author><abstract>Benchmarking robust evaluation.</abstract><pdf hash="a2"/></paper>',
        '<paper id="3"><title>Fragment analysis</title><abstract>Fragment statistics.</abstract><pdf hash="a3"/></paper>',
    ]
    return (f'<collection id="{collection}"><volume id="main" type="proceedings"><meta><year>{collection[:4]}</year><month>{month}</month></meta>'
            + "".join(papers) + '</volume></collection>').encode()


def query(*collections, any_terms=None, all_terms=None):
    return json.dumps({"collections": list(collections or ["2026.emnlp"]),
                       "any": ["RAG"] if any_terms is None else any_terms, "all": all_terms or []})


def adapter(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AclAnthology(Transport(client, throttle=False), **kwargs)


def search(source, q=None, *, cursor=None, page_size=50, date_from="2025-01-01", date_to="2026-11-03"):
    return source.search(q or query(), cursor=cursor, page_size=page_size, date_from=date_from, date_to=date_to)


def test_real_schema_inline_title_author_doi_and_original_pdf():
    raw = xml()
    source = adapter(lambda request: httpx.Response(200, content=raw))
    result = search(source)
    assert len(result.items) == 2  # RAG never matches fragment
    paper = result.items[0]
    assert paper.title == "RAG Evaluation"
    assert paper.authors == ["Jane Doe"]
    assert paper.source_id == "2026.emnlp-main.1"
    assert paper.doi == "10.18653/2026.emnlp-main.1"
    assert paper.fulltext_urls == ["https://aclanthology.org/2026.emnlp-main.1.pdf"]
    assert paper.extra["snapshot_sha256"] == hashlib.sha256(raw).hexdigest()
    assert paper.extra["publication_month"] == "October"
    assert paper.extra["publication_date_precision"] == "month"
    assert "publication_date" not in paper.extra  # No invented exact day
    assert result.next_cursor is None and result.total == 2


def test_literal_phrases_any_and_all_match_title_and_abstract():
    source = adapter(lambda request: httpx.Response(200, content=xml()))
    result = search(source, query(any_terms=["retrieval augmented"], all_terms=["calibration"]))
    assert [paper.source_id for paper in result.items] == ["2026.emnlp-main.1"]
    assert not search(source, query(any_terms=["retrieval augmented"], all_terms=["not present"])).items


def test_collection_pagination_uses_same_snapshot_and_later_collection_is_not_skipped():
    requests = []
    def handler(request):
        requests.append(str(request.url))
        name = request.url.path.rsplit("/", 1)[-1][:-4]
        return httpx.Response(200, content=xml(name))
    source = adapter(handler)
    q = query("2025.acl", "2026.emnlp")
    pages, cursor = [], None
    while True:
        page = search(source, q, cursor=cursor, page_size=1)
        pages.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            assert page.total == 4
            break
        assert page.total is None
    assert len(pages) == 4 and len(requests) == 2
    assert pages[0].source_id.startswith("2025.acl-")
    assert pages[-1].source_id.startswith("2026.emnlp-")


def test_empty_valid_collection_page_retains_next_collection_cursor():
    source = adapter(lambda request: httpx.Response(200, content=xml("2025.acl", papers=[]) if "2025.acl" in str(request.url) else xml()))
    q = query("2025.acl", "2026.emnlp")
    first = search(source, q)
    assert not first.items and first.next_cursor is not None and first.total is None
    second = search(source, q, cursor=first.next_cursor)
    assert second.total == 2 and second.next_cursor is None


def test_cross_process_cursor_refetch_verifies_snapshot_before_advancing():
    q = query()
    old = adapter(lambda request: httpx.Response(200, content=xml()))
    cursor = search(old, q, page_size=1).next_cursor
    unchanged = adapter(lambda request: httpx.Response(200, content=xml()))
    assert search(unchanged, q, cursor=cursor, page_size=1).items[0].source_id.endswith(".2")
    changed = adapter(lambda request: httpx.Response(200, content=xml().replace(b"Noise in", b"Changed")))
    with pytest.raises(ResearchError) as error:
        search(changed, q, cursor=cursor, page_size=1)
    assert error.value.code == "SOURCE_SNAPSHOT_CHANGED"


@pytest.mark.parametrize("change", ["query", "page_size", "date"])
def test_cursor_cannot_change_query_or_date_or_page_size(change):
    source = adapter(lambda request: httpx.Response(200, content=xml()))
    cursor = search(source, page_size=1).next_cursor
    kwargs = {"cursor": cursor, "page_size": 2 if change == "page_size" else 1}
    if change == "date":
        kwargs["date_from"] = "2026-01-01"
    with pytest.raises(ResearchError) as error:
        search(source, query(any_terms=["evaluation"]) if change == "query" else None, **kwargs)
    assert error.value.code == "INVALID_ARGUMENT"


def test_month_precision_uses_overlap_and_does_not_fabricate_day():
    source = adapter(lambda request: httpx.Response(200, content=xml(month="November")))
    assert search(source, date_to="2026-10-31").total == 0
    included = search(source, date_to="2026-11-03")
    assert included.total == 2
    assert included.items[0].extra["publication_interval_end"] == "2026-11-30"
    assert any("interval overlap" in warning for warning in included.warnings)


def test_days_embedded_in_month_text_do_not_expand_to_unrelated_months():
    source = adapter(lambda request: httpx.Response(200, content=xml(month="November 3-5")))
    assert search(source, date_to="2026-10-31").total == 0
    assert search(source).items[0].extra["publication_interval_start"] == "2026-11-01"


@pytest.mark.parametrize("body", [b"<collection", b"<html>not XML metadata</html>", xml("2026.acl"),
    b'<collection id="2026.emnlp"></collection>',
    b'<!DOCTYPE collection [<!ENTITY x "RAG">]><collection id="2026.emnlp"><volume id="main"/></collection>'])
def test_invalid_or_wrong_identity_xml_is_never_empty_complete(body):
    source = adapter(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ResearchError) as error:
        search(source)
    assert error.value.code == "SOURCE_INVALID_RESPONSE"


def test_missing_collection_propagates_error_and_file_size_is_bounded():
    missing = adapter(lambda request: httpx.Response(404))
    with pytest.raises(ResearchError) as error:
        search(missing)
    assert error.value.details["http_status"] == 404
    oversized = adapter(lambda request: httpx.Response(200, content=xml()), max_file_bytes=50)
    with pytest.raises(ResearchError) as error:
        search(oversized)
    assert error.value.code == "SOURCE_RESULT_LIMIT"


@pytest.mark.parametrize("q", ["RAG", '{"collections":["../../secret"],"any":["RAG"]}',
    '{"collections":["https://example.com"],"any":["RAG"]}',
    '{"collections":["2026.emnlp"],"any":[]}',
    '{"collections":["2026.emnlp"],"any":["RAG"],"url":"https://example.com"}'])
def test_query_accepts_only_declared_official_collection_names(q):
    source = adapter(lambda request: pytest.fail("Invalid query must not make a request"))
    with pytest.raises(ResearchError) as error:
        search(source, q)
    assert error.value.code == "SOURCE_INVALID_QUERY"
