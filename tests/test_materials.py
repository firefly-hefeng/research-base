from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from research_base.core import ResearchBase
from research_base.models import ResearchError


class OfflineSources:
    def list_sources(self):
        return []


@pytest.fixture
def base(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        with ResearchBase(tmp_path / "library", source_registry=OfflineSources(), client=client) as library:
            yield library


def add(base, title="Evaluating Retrieval Quality in Research Systems", doi="10.5555/expected"):
    return base.add_paper({"source": "offline", "source_id": title + str(doi), "title": title,
                           "authors": ["Example Author"], "doi": doi, "version_kind": "published"})


def pdf(path, page_texts):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = writer._add_object(font)
    for text in page_texts:
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(("BT /F1 12 Tf 50 800 Td (" + escaped + ") Tj ET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


def test_repository_revision_checks_the_pdf_posted_date_not_just_shared_doi(base, tmp_path):
    title = "Stable Experimental Benchmark for Research Systems"
    doi = "10.5555/version-date"
    paper = base.add_paper({"source":"biorxiv", "source_id":doi+"v3", "title":title, "doi":doi,
        "version_kind":"preprint", "landing_url":"https://www.biorxiv.org/content/"+doi+"v3",
        "extra":{"posted_date":"2026-10-31"}})
    wrong = tmp_path / "old.pdf"
    pdf(wrong, [title+" " + doi + " this version posted October 19, 2026."])
    staged = base.import_material(paper["paper_id"], path=wrong, version_id=paper["preferred_version_id"])
    assert staged["status"] == "staged"
    assert any("posted-date" in issue for issue in staged["details"]["identity_issues"])
    right = tmp_path / "current.pdf"
    pdf(right, [title+" " + doi + " this version posted October 31, 2026."])
    ready = base.import_material(paper["paper_id"], path=right, version_id=paper["preferred_version_id"],
        source_url="https://www.biorxiv.org/content/"+doi+"v3.full.pdf")
    assert ready["status"] == "available"
    assert ready["details"]["observed_posted_date"] == "2026-10-31"


def test_original_and_text_paths_are_absolute_and_directly_readable(base, tmp_path):
    record = add(base)
    source = tmp_path / "article.pdf"
    pdf(source, [record["title"] + " 10.5555/expected", "The second page describes evaluation experiments."])
    material = base.import_material(record["paper_id"], path=source)
    assert material["status"] == "available"
    assert Path(material["local_path"]).is_absolute()
    assert Path(material["local_path"]).read_bytes() == source.read_bytes()
    assert "second page" in Path(material["text_path"]).read_text(encoding="utf-8")
    assert Path(material["pages_path"]).is_file()
    assert base.read_material(material["material_id"], pages=[2])["pages"][0]["page"] == 2
    hidden = base.list_materials(paper_id=record["paper_id"], include_paths=False)[0]
    assert "local_path" not in hidden
    assert "text_path" not in hidden


def test_pdf_footer_doi_label_is_not_part_of_identifier(base, tmp_path):
    record = add(base)
    source = tmp_path / "footer.pdf"
    pdf(source, [record["title"] + " https://doi.org/10.5555/expecteddoi: bioRxiv preprint"])
    material = base.import_material(record["paper_id"], path=source)
    assert material["status"] == "available"


def test_unknown_file_version_is_not_assumed_to_be_preferred_publication(base, tmp_path):
    record = add(base)
    source = tmp_path / 'unidentified-version.pdf'
    pdf(source, [record['title']])
    material = base.import_material(record['paper_id'], path=source)
    assert material['status'] == 'available'
    assert material['version_id'] is None
    cid = base.create_collection('Version-sensitive')['collection_id']
    base.set_membership(cid, [record['paper_id']])
    job = base.acquire_papers(cid, version_policy=['published'])
    assert job['state'] != 'succeeded'


def test_explicit_arxiv_stamp_cannot_silently_become_published_file(base, tmp_path):
    published = base.add_paper({'source': 'crossref', 'source_id': '10.5555/final', 'doi': '10.5555/final', 'title': 'Evaluating Retrieval Quality in Research Systems', 'version_kind': 'published'})
    preprint = base.add_paper({'source': 'arxiv', 'source_id': '2601.01234v1', 'identifiers': {'arxiv': '2601.01234v1'}, 'title': published['title'], 'version_kind': 'preprint', 'extra': {'published_doi': '10.5555/final'}})
    assert preprint['paper_id'] == published['paper_id']
    source = tmp_path / 'preprint-stamped.pdf'
    pdf(source, [published['title'] + ' arXiv:2601.01234v1 10.5555/final'])
    material = base.import_material(published['paper_id'], path=source, version_id=published['preferred_version_id'])
    assert material['status'] == 'staged'
    assert 'different preprint version identity' in ' '.join(material['details']['identity_issues'])


@pytest.mark.parametrize('observed', ['2601.01234v2', '2601.01234v10', '2601.01234', '2601.99999v1'])
def test_requested_arxiv_revision_rejects_conflicting_download_without_losing_file(base, tmp_path, observed):
    title = 'Evaluating Retrieval Quality in Research Systems'
    record = base.add_paper({'source': 'arxiv', 'source_id': '2601.01234v1', 'title': title,
                            'identifiers': {'arxiv': '2601.01234v1'}, 'version_kind': 'preprint',
                            'fulltext_urls': ['https://arxiv.org/pdf/2601.01234v1']})
    cid = base.create_collection('Strict revision')['collection_id']
    base.set_membership(cid, [record['paper_id']])
    vid = record['preferred_version_id']
    job = base.acquire_papers(cid, requested_versions={record['paper_id']: vid})
    source = tmp_path / 'different-revision.pdf'
    pdf(source, [title + ' arXiv:' + observed])
    content = source.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content))) as client:
        base.client = client
        result = base.run_worker(job['job_id'])
    assert result['state'] == 'needs_user'
    material = base.list_materials(paper_id=record['paper_id'])[0]
    assert material['status'] == 'staged'
    assert material['exists'] and Path(material['local_path']).read_bytes() == content
    assert 'arXiv identity or revision' in ' '.join(material['details']['identity_issues'])


def test_arxiv_v10_cannot_be_inferred_as_only_known_v1(base, tmp_path):
    title = 'Evaluating Retrieval Quality in Research Systems'
    record = base.add_paper({'source': 'arxiv', 'source_id': '2601.01234v1', 'title': title,
                            'identifiers': {'arxiv': '2601.01234v1'}, 'version_kind': 'preprint'})
    cid = base.create_collection('Strict revision')['collection_id']
    base.set_membership(cid, [record['paper_id']])
    job = base.acquire_papers(cid, requested_versions={record['paper_id']: record['preferred_version_id']})
    source = tmp_path / 'v10.pdf'
    pdf(source, [title + ' arXiv:2601.01234v10'])
    material = base.import_material(record['paper_id'], path=source)
    assert material['exists'] and material['version_id'] is None
    assert base.get_job(job['job_id'])['state'] != 'succeeded'


@pytest.mark.parametrize('known,stamp', [('2601.01234v1', 'arXiv:2601.01234v1'),
                                        ('2601.01234', 'https://arxiv.org/pdf/2601.01234v10.pdf'),
                                        ('hep-th/9901001v2', 'arXiv:hep-th/9901001v2')])
def test_complete_matching_arxiv_stamp_can_infer_requested_version(base, tmp_path, known, stamp):
    record = base.add_paper({'source': 'arxiv', 'source_id': known,
                            'title': 'Evaluating Retrieval Quality in Research Systems',
                            'identifiers': {'arxiv': known}, 'version_kind': 'preprint'})
    source = tmp_path / 'matching.pdf'
    pdf(source, [record['title'] + ' ' + stamp])
    material = base.import_material(record['paper_id'], path=source)
    assert material['status'] == 'available'
    assert material['version_id'] == record['preferred_version_id']


def test_header_doi_conflict_cannot_be_overridden_by_identical_title(base, tmp_path):
    record = add(base)
    source = tmp_path / "wrong.pdf"
    pdf(source, [record["title"] + " 10.5555/another-paper"])
    material = base.import_material(record["paper_id"], path=source)
    assert material["status"] == "staged"
    assert material["details"]["identity_issues"]
    assert base.get_paper(record["paper_id"])["fulltext_available"] is False


def test_reference_title_on_second_page_does_not_validate_wrong_paper(base, tmp_path):
    record = add(base)
    source = tmp_path / "references.pdf"
    pdf(source, ["A completely unrelated investigation by Another Author", "References " + record["title"] + " 10.5555/expected"])
    material = base.import_material(record["paper_id"], path=source)
    assert material["status"] == "staged"
    assert material["details"]["identity_issues"]


def test_blank_scanned_pdf_is_available_after_explicit_confirmation_without_fake_text(base, tmp_path):
    record = add(base)
    source = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with source.open("wb") as output:
        writer.write(output)
    material = base.import_material(record["paper_id"], path=source)
    assert material["status"] == "staged"
    confirmed = base.confirm_material_identity(material["material_id"])
    assert confirmed["status"] == "available"
    assert confirmed["parse_status"] == "empty"
    record = base.get_paper(record["paper_id"])
    assert record["fulltext_available"] is True
    assert record["text_searchable"] is False
    assert Path(confirmed["local_path"]).exists()


def test_html_with_pdf_extension_is_rejected(base, tmp_path):
    record = add(base)
    source = tmp_path / "login.pdf"
    source.write_text("<html><body>Sign in to continue</body></html>", encoding="utf-8")
    with pytest.raises(ResearchError) as error:
        base.import_material(record["paper_id"], path=source)
    assert error.value.code == "INVALID_FILE"
    assert not base.get_paper(record["paper_id"])["fulltext_available"]


def test_duplicate_bytes_share_object_without_deleting_source(base, tmp_path):
    record = add(base)
    source = tmp_path / "code.py"
    source.write_text("print('example')\n", encoding="utf-8")
    one = base.import_material(record["paper_id"], path=source, role="code")
    two = base.import_material(record["paper_id"], path=source, role="code")
    assert one["material_id"] != two["material_id"]
    assert one["sha256"] == two["sha256"]
    assert one["local_path"] == two["local_path"]
    assert source.exists()


def test_external_code_link_stays_external_without_false_local_path(base):
    record = add(base)
    material = base.import_material(record["paper_id"], url="https://code.example/repository", role="code")
    assert material["status"] == "external"
    assert material["local_path"] is None
    assert material["source_url"] == "https://code.example/repository"
    read = base.read_material(material["material_id"], view="original")
    assert read["content_available"] is False


def test_long_single_page_can_be_read_to_completion_with_offsets(base, tmp_path):
    record = add(base)
    source = tmp_path / "long.txt"
    text = "0123456789" * 45
    source.write_text(text, encoding="utf-8")
    material = base.import_material(record["paper_id"], path=source, confirmed=True)
    cursor, pieces = 0, []
    for _ in range(10):
        result = base.read_material(material["material_id"], pages=[1], max_chars=100, char_offset=cursor)
        pieces.extend(page["text"] for page in result["pages"])
        if not result["truncated"]:
            break
        cursor = result["next_char_offset"]
    assert "".join(pieces) == text


def test_continuation_keeps_selected_pages_across_a_partial_second_page(base, tmp_path):
    from research_base.tools import ToolRegistry

    record = add(base)
    source = tmp_path / "selected-pages.pdf"
    pdf(source, [record["title"] + " 10.5555/expected", "A" * 4800, "B" * 3600, "UNSELECTED"])
    material = base.import_material(record["paper_id"], path=source)
    registry = ToolRegistry(base)
    first = registry.call("read_material", {"material_id": material["material_id"], "pages": [2, 3], "max_chars": 6000})["data"]
    assert first["truncated"] and first["next_page"] == 3
    assert first["continuation"] == {"material_id": material["material_id"], "view": "text", "pages": [2, 3], "max_chars": 6000, "char_offset": 6000}
    assert first["offset_scope"]["pages"] == [2, 3]
    second = registry.call("read_material", first["continuation"])["data"]
    assert second["continuation"] is None and not second["truncated"]
    text = "".join(p["text"] for result in (first, second) for p in result["pages"])
    assert text.replace("\n", "") == "A" * 4800 + "B" * 3600
    assert all(p["page"] in (2, 3) for result in (first, second) for p in result["pages"])


def test_continuation_without_page_selection_replays_through_tool_schema(base, tmp_path):
    from research_base.tools import ToolRegistry

    record = add(base)
    source = tmp_path / "whole-material.txt"
    text = "Whole material without a page filter. " * 20
    source.write_text(text, encoding="utf-8")
    material = base.import_material(record["paper_id"], path=source, confirmed=True)
    registry = ToolRegistry(base)
    arguments = {"material_id": material["material_id"], "max_chars": 100}
    pieces = []
    while arguments is not None:
        response = registry.call("read_material", arguments)
        assert "error" not in response
        result = response["data"]
        pieces.extend(page["text"] for page in result["pages"])
        arguments = result["continuation"]
        if arguments is not None:
            assert "pages" not in arguments  # null would be rejected by the tool schema.
    assert "".join(pieces) == text


def test_partial_download_file_is_not_imported(base, tmp_path):
    record = add(base)
    source = tmp_path / "article.pdf.crdownload"
    source.write_bytes(b"unfinished")
    with pytest.raises(ResearchError) as error:
        base.import_material(record["paper_id"], path=source)
    assert error.value.code == "FILE_NOT_READY"
