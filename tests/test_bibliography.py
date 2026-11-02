import builtins
import json
from pathlib import Path

import pytest

from research_base.bibliography import parse_bibliography
from research_base.models import Candidate, ResearchError


def write(tmp_path, filename, text):
    path = tmp_path / filename
    path.write_text(text, encoding="utf-8")
    return path


def test_csl_json_authors_dates_identifiers_tags_and_external_links(tmp_path):
    path = write(tmp_path, "papers.json", json.dumps([{
        "id": "article1", "type": "article-journal", "title": "检索增强评估",
        "author": [{"given": "Ada", "family": "Lovelace"}, {"literal": "Research Consortium"}],
        "issued": {"date-parts": [[2026, 10, 1]]}, "DOI": "https://doi.org/10.5555/EXAMPLE",
        "abstract": "A useful abstract", "URL": "https://publisher.example/paper.pdf",
        "keyword": "评估;retrieval", "code_url": "https://github.com/example/research",
        "data_url": "https://data.example/dataset"
    }], ensure_ascii=False))
    entry = parse_bibliography(path)[0]
    candidate = entry["candidate"]
    assert isinstance(candidate, Candidate)
    assert candidate.title == "检索增强评估"
    assert candidate.authors == ["Ada Lovelace", "Research Consortium"]
    assert candidate.year == 2026
    assert candidate.doi == "10.5555/example"
    assert candidate.abstract == "A useful abstract"
    assert entry["tags"] == ["评估", "retrieval"]
    assert candidate.fulltext_urls == ["https://publisher.example/paper.pdf"]
    assert {link.role for link in candidate.links} == {"main_text", "code", "data"}


def test_zotero_api_wrapper_and_child_attachment_do_not_read_file(tmp_path, monkeypatch):
    secret_attachment = write(tmp_path, "private.txt", "must not be read automatically")
    records = [{"key": "PARENT", "data": {
        "itemType": "journalArticle", "title": "Metadata Import", "creators": [
            {"creatorType": "author", "firstName": "Ada", "lastName": "Example"},
            {"creatorType": "editor", "firstName": "Not", "lastName": "AnAuthor"}],
        "date": "2025-12-31", "DOI": "10.5555/import", "abstractNote": "Original abstract",
        "tags": [{"tag": "method"}, {"tag": "中文标签"}], "url": "https://publisher.example/import"}},
        {"key": "FILE", "data": {"itemType": "attachment", "parentItem": "PARENT", "path": str(secret_attachment), "url": "https://publisher.example/supplement.pdf"}},
        {"key": "NOTE", "data": {"itemType": "note", "parentItem": "PARENT", "note": "Not a publication"}}]
    source = write(tmp_path, "zotero.json", json.dumps(records))
    original_read = Path.read_text
    reads = []
    def tracked_read(path, *args, **kwargs):
        reads.append(path)
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", tracked_read)
    entries = parse_bibliography(source)
    assert len(entries) == 1
    candidate = entries[0]["candidate"]
    assert candidate.source == "zotero_json"
    assert candidate.authors == ["Ada Example"]
    assert candidate.year == 2025
    assert entries[0]["tags"] == ["method", "中文标签"]
    assert secret_attachment not in reads
    assert any("not read" in warning for warning in entries[0]["warnings"])


def test_zotero_raw_item_and_export_items_container_are_supported(tmp_path):
    record = {"key": "ABC", "itemType": "conferencePaper", "title": "Conference Study", "creators": [{"name": "Study Group", "creatorType": "author"}], "date": "2024"}
    single = write(tmp_path, "single.json", json.dumps(record))
    bundled = write(tmp_path, "items.json", json.dumps({"items": [record]}))
    assert parse_bibliography(single)[0]["candidate"].authors == ["Study Group"]
    assert parse_bibliography(single)[0]["candidate"].source_id == parse_bibliography(bundled)[0]["candidate"].source_id


def test_ris_repeated_authors_multiline_abstract_tags_and_doi(tmp_path):
    path = write(tmp_path, "library.ris", """TY  - JOUR
TI  - Reproducible Evaluation
AU  - Example, Ada
AU  - Example, Bo
PY  - 2026/10/01
DO  - 10.5555/RIS
AB  - First abstract line.
      A second abstract line.
KW  - reproducibility
KW  - evaluation
UR  - https://publisher.example/ris.pdf
ER  -
""")
    entry = parse_bibliography(path)[0]
    candidate = entry["candidate"]
    assert candidate.authors == ["Example, Ada", "Example, Bo"]
    assert candidate.year == 2026
    assert candidate.doi == "10.5555/ris"
    assert "A second abstract line." in candidate.abstract
    assert entry["tags"] == ["reproducibility", "evaluation"]
    assert candidate.fulltext_urls == ["https://publisher.example/ris.pdf"]


def test_bibtex_nested_braces_organization_author_unicode_and_file_warning(tmp_path):
    path = write(tmp_path, "library.bib", r"""@string{topic = "Retrieval"}
@article{ada2026,
 title = {An {Efficient} Method for {Evaluation}},
 author = {{Research and Development Group} and Example, Ada},
 year = {2026},
 doi = {10.5555/BIB},
 abstract = {Nested {technical} details},
 keywords = {retrieval;中文},
 url = {https://publisher.example/article},
 file = {C:/Local/private.pdf},
 note = {Code: https://github.com/example/retrieval}
}
""")
    entry = parse_bibliography(path)[0]
    candidate = entry["candidate"]
    assert candidate.title == "An Efficient Method for Evaluation"
    assert candidate.authors == ["Research and Development Group", "Example, Ada"]
    assert candidate.year == 2026
    assert candidate.doi == "10.5555/bib"
    assert entry["tags"] == ["retrieval", "中文"]
    assert any(link.role == "code" for link in candidate.links)
    assert any("not read" in warning for warning in entry["warnings"])


def test_repeat_import_and_moving_file_keep_source_identity(tmp_path):
    text = json.dumps([{"id": "key", "type": "article", "title": "Stable Source", "author": [{"family": "Author"}], "issued": {"date-parts": [[2026]]}}])
    one = write(tmp_path, "one.json", text)
    two = write(tmp_path, "two.json", text)
    first = parse_bibliography(one)[0]["candidate"]
    assert first.source_id == parse_bibliography(two)[0]["candidate"].source_id
    assert first.source_id == parse_bibliography(one)[0]["candidate"].source_id


def test_unrelated_same_citation_key_does_not_collide(tmp_path):
    one = write(tmp_path, "one.bib", "@article{same, title={First Different Paper}, year={2026}}")
    two = write(tmp_path, "two.bib", "@article{same, title={Second Different Paper}, year={2026}}")
    assert parse_bibliography(one)[0]["candidate"].source_id != parse_bibliography(two)[0]["candidate"].source_id


@pytest.mark.parametrize("filename,text", [
    ("dataset.json", '[{"id":"data1","type":"dataset","title":"Measurements"}]'),
    ("dataset.ris", 'TY  - DATA\nTI  - Measurements\nER  -\n'),
    ("software.bib", '@software{tool,title={A Computer Program}}'),
])
def test_non_publications_do_not_become_papers(tmp_path, filename, text):
    assert parse_bibliography(write(tmp_path, filename, text)) == []


@pytest.mark.parametrize("filename,text", [
    ("broken.json", '{"broken":'),
    ("wrong-shape.json", '{"title":"not a list"}'),
    ("wrong-entry.json", '[1]'),
    ("missing-title.json", '[{"type":"article"}]'),
    ("broken.ris", 'TY  - JOUR\nTI  - Missing End'),
    ("empty.bib", 'ordinary text instead of bibtex'),
    ("empty.json", ''),
])
def test_invalid_inputs_are_explicit_errors(tmp_path, filename, text):
    with pytest.raises(ResearchError) as error:
        parse_bibliography(write(tmp_path, filename, text))
    assert error.value.code == "INVALID_BIBLIOGRAPHY"


def test_unknown_format_and_missing_file_are_clear(tmp_path):
    path = write(tmp_path, "anything.txt", "[]")
    with pytest.raises(ResearchError) as error:
        parse_bibliography(path, format="invented")
    assert error.value.code == "INVALID_ARGUMENT"
    with pytest.raises(ResearchError) as error:
        parse_bibliography(tmp_path / "absent.json")
    assert error.value.code == "NOT_FOUND"


def test_doi_url_fallback_and_sensitive_query_redaction(tmp_path):
    path = write(tmp_path, "metadata.json", json.dumps([{"type": "article", "title": "A Metadata Record", "URL": "https://doi.org/10.5555/fallback", "code_url": "https://github.com/example/repo?api_key=not-a-real-key&view=tree"}]))
    candidate = parse_bibliography(path)[0]["candidate"]
    assert candidate.doi == "10.5555/fallback"
    assert "not-a-real-key" not in candidate.model_dump_json()
    assert any(link.url.endswith("?view=tree") for link in candidate.links)


def test_bibtex_missing_dependency_has_actionable_error(tmp_path, monkeypatch):
    path = write(tmp_path, "article.bib", "@article{key,title={Example}}")
    original_import = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == "bibtexparser" or name.startswith("bibtexparser."):
            raise ImportError("simulated missing optional dependency")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ResearchError) as error:
        parse_bibliography(path)
    assert error.value.code == "DEPENDENCY"
