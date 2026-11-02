"""Read publication bibliographies without reading referenced attachment files."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from .materials import normalized_doi, safe_url
from .models import Candidate, MaterialLink, ResearchError


FORMAT_ALIASES = {
    "bib": "bibtex", "bibtex": "bibtex", "ris": "ris", "json": "json",
    "csl": "csl_json", "csl_json": "csl_json", "csl-json": "csl_json",
    "zotero": "zotero_json", "zotero_json": "zotero_json", "zotero-json": "zotero_json",
    "zotero_api_json": "zotero_json", "zotero-api-json": "zotero_json",
}
NON_PUBLICATIONS = {"dataset", "data", "software", "computerprogram", "computer_program", "comp",
                    "attachment", "note", "annotation", "webpage", "graphic", "motion_picture", "map"}


def _plain(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", str(value))).strip()


def _year(value) -> int | None:
    match = re.search(r"(?<!\d)([12]\d{3})(?!\d)", _plain(value))
    return int(match[1]) if match else None


def _tags(value, *, split_strings=True) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[;,]", value) if split_strings else [value]
    elif isinstance(value, list):
        values = []
        for item in value:
            if isinstance(item, dict):
                values.append(item.get("tag", ""))
            else:
                values.append(item)
    else:
        values = []
    return list(dict.fromkeys(tag for raw in values if (tag := _plain(raw))))


def _authors(value) -> list[str]:
    if isinstance(value, str):
        return [_plain(value)] if _plain(value) else []
    if not isinstance(value, list):
        return []
    authors = []
    for author in value:
        if isinstance(author, dict):
            if author.get("creatorType", "author") != "author":
                continue
            name = _plain(author.get("literal") or author.get("name"))
            if not name:
                given = _plain(author.get("given") or author.get("firstName"))
                family = _plain(author.get("family") or author.get("lastName"))
                name = " ".join(part for part in (given, family) if part)
        else:
            name = _plain(author)
        if name:
            authors.append(name)
    return list(dict.fromkeys(authors))


def _bib_authors(value: str) -> list[str]:
    """Split BibTeX's author separator outside brace-protected organization names."""
    parts, start, depth, index = [], 0, 0, 0
    while index < len(value):
        char = value[index]
        if char == "{" and (index == 0 or value[index - 1] != "\\"):
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        if depth == 0 and value[index:index + 5].lower() == " and ":
            parts.append(value[start:index].strip())
            start, index = index + 5, index + 4
        index += 1
    parts.append(value[start:].strip())
    from bibtexparser.latexenc import latex_to_unicode
    return list(dict.fromkeys(_plain(latex_to_unicode(part)) for part in parts if part))


def _doi(value, url, warnings) -> str | None:
    raw = _plain(value)
    if not raw and isinstance(url, str):
        parts = urlsplit(url)
        if (parts.hostname or "").lower() in ("doi.org", "dx.doi.org"):
            raw = unquote(parts.path.lstrip("/"))
    doi = normalized_doi(raw)
    if doi and not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        warnings.append("Invalid DOI was ignored")
        return None
    return doi


def _links(record, primary_url, warnings) -> tuple[str | None, list[str], list[MaterialLink]]:
    links: list[MaterialLink] = []
    fulltext: list[str] = []
    landing = None

    def append(raw, role=None):
        nonlocal landing
        if not isinstance(raw, str) or not raw.strip():
            return
        raw = raw.strip()
        if not raw.lower().startswith(("http://", "https://")):
            return
        try:
            url = safe_url(raw)
        except (ResearchError, ValueError):
            warnings.append("Invalid material URL was ignored")
            return
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        fmt = "pdf" if parts.path.lower().endswith(".pdf") else "url"
        inferred = "code" if host in ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org") else "main_text"
        role = role or inferred
        if landing is None:
            landing = url
        if role == "main_text" and fmt == "pdf":
            fulltext.append(url)
        links.append(MaterialLink(role=role, url=url, format=fmt))

    append(primary_url)
    fields = {"code": "code", "code_url": "code", "repository_url": "code", "software_url": "code",
              "data": "data", "data_url": "data", "dataset_url": "data", "supplement": "supplement", "supplement_url": "supplement"}
    for key, role in fields.items():
        value = record.get(key)
        for raw in value if isinstance(value, list) else [value]:
            append(raw, role)
    for key in ("note", "extra", "howpublished"):
        value = record.get(key)
        if isinstance(value, str):
            for url in re.findall(r"https?://[^\s<>\"{}]+", value):
                if (urlsplit(url).hostname or "").lower() in ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org"):
                    append(url.rstrip(".,;"), "code")
    attachments = record.get("attachments", [])
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            role = attachment.get("role")
            if role not in ("main_text", "supplement", "code", "data", "other"):
                title = _plain(attachment.get("title")).lower()
                role = "supplement" if re.search(r"supplement|supporting information", title) else "main_text" if re.search(r"full.?text", title) else "other"
            append(attachment.get("url"), role)
            if any(attachment.get(key) for key in ("path", "localPath", "filename")):
                warnings.append("Local attachment was not read; import it explicitly after inspection")
    if any(record.get(key) for key in ("file", "files", "path", "localPath", "pdf")):
        warnings.append("Local attachment references were not read; use explicit material import")
    unique = {(link.role, link.url): link for link in links}
    return landing, list(dict.fromkeys(fulltext)), list(unique.values())


def _candidate(record, source, source_id, *, title, authors, year, doi=None, abstract="", url=None, tags=None, warnings=None, kind="unknown"):
    warnings = list(warnings or [])
    title = _plain(title)
    if not title:
        raise ResearchError("INVALID_BIBLIOGRAPHY", "A publication record has no title")
    doi = _doi(doi, url, warnings)
    landing, fulltext, links = _links(record, url, warnings)
    normalized = {"source_id": _plain(source_id), "title": title, "authors": authors, "year": year, "doi": doi}
    # Source IDs are stable across file moves and repeated imports; citation keys
    # from unrelated bibliographies cannot silently collide with different titles.
    identifier = doi or hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
    return {"candidate": Candidate(source=source, source_id=identifier, title=title, authors=authors,
            year=year, doi=doi, abstract=_plain(abstract), identifiers={"doi": doi} if doi else {},
            landing_url=landing, fulltext_urls=fulltext, links=links, version_kind=kind,
            extra={"import_record_id": _plain(source_id), "bibliography_format": source}),
            "tags": list(dict.fromkeys(tags or [])), "warnings": list(dict.fromkeys(warnings))}


def _json_records(value, selected_format):
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        values = value["items"]
    elif isinstance(value, dict) and (isinstance(value.get("data"), dict) or "itemType" in value):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ResearchError("INVALID_BIBLIOGRAPHY", "JSON must contain a CSL item list or Zotero API items")
    result, skipped, attachments = [], 0, {}
    for raw in values:
        if not isinstance(raw, dict):
            raise ResearchError("INVALID_BIBLIOGRAPHY", "Each JSON bibliography record must be an object")
        record = raw.get("data", raw)
        if not isinstance(record, dict):
            raise ResearchError("INVALID_BIBLIOGRAPHY", "Zotero data must be an object")
        if _plain(record.get("itemType")).lower() == "attachment":
            parent = record.get("parentItem")
            if parent:
                attachments.setdefault(str(parent), []).append(record)
    for raw in values:
        record = raw.get("data", raw)
        zotero = selected_format == "zotero_json" or (selected_format == "json" and ("itemType" in record or "data" in raw))
        record_type = _plain(record.get("itemType") if zotero else record.get("type")).lower()
        if record_type in NON_PUBLICATIONS:
            skipped += 1
            continue
        working = dict(record)
        external_key = raw.get("key") or record.get("key") or record.get("id")
        if zotero and str(external_key) in attachments:
            working["attachments"] = list(record.get("attachments") or []) + attachments[str(external_key)]
        if zotero:
            title, authors = record.get("title"), _authors(record.get("creators"))
            year = _year(record.get("date"))
            abstract, doi, url = record.get("abstractNote"), record.get("DOI"), record.get("url")
            tags = _tags(record.get("tags"), split_strings=False)
        else:
            title, authors = record.get("title"), _authors(record.get("author"))
            issued = record.get("issued") or {}
            parts = issued.get("date-parts", []) if isinstance(issued, dict) else []
            year = _year(parts[0][0]) if parts and isinstance(parts[0], list) and parts[0] else _year(record.get("year"))
            abstract, doi, url = record.get("abstract"), record.get("DOI") or record.get("doi"), record.get("URL") or record.get("url")
            tags = _tags(record.get("keyword") or record.get("keywords") or record.get("tags"))
        warnings = [] if record_type else ["Publication type is unspecified"]
        if zotero and str(external_key) in attachments:
            warnings.append("Zotero attachment records were linked as metadata only; local files were not read")
        result.append(_candidate(working, "zotero_json" if zotero else "csl_json", external_key,
            title=title, authors=authors, year=year, doi=doi, abstract=abstract, url=url, tags=tags, warnings=warnings,
            kind="preprint" if record_type in ("preprint", "manuscript") else "unknown"))
    if skipped and result:
        result[0]["warnings"].append(f"Skipped {skipped} attachment, note or non-publication records")
    return result


def _ris_records(text):
    raw_records, current, last = [], None, None
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^([A-Z0-9]{2})\s*-\s?(.*)$", line)
        if match:
            tag, value = match.groups()
            if tag == "TY":
                if current is not None:
                    raise ResearchError("INVALID_BIBLIOGRAPHY", "RIS record started before the previous ER terminator")
                current, last = {"TY": [value.strip()]}, "TY"
            elif tag == "ER":
                if current is None:
                    raise ResearchError("INVALID_BIBLIOGRAPHY", "RIS ER has no record")
                raw_records.append(current)
                current, last = None, None
            elif current is not None:
                current.setdefault(tag, []).append(value.strip())
                last = tag
            else:
                raise ResearchError("INVALID_BIBLIOGRAPHY", "RIS records must begin with TY")
        elif current is not None and last:
            current[last][-1] += "\n" + line.strip()
        else:
            raise ResearchError("INVALID_BIBLIOGRAPHY", "Unrecognized text outside a RIS record")
    if current is not None:
        raise ResearchError("INVALID_BIBLIOGRAPHY", "RIS record is missing its ER terminator")
    if not raw_records:
        raise ResearchError("INVALID_BIBLIOGRAPHY", "No RIS records were found")
    result = []
    for index, record in enumerate(raw_records):
        first = lambda *keys: next((record[key][0] for key in keys if record.get(key)), "")
        if first("TY").lower() in NON_PUBLICATIONS or first("TY").upper() in ("DATA", "COMP", "ELEC"):
            continue
        url = first("UR", "L1")
        link_record = {"note": "\n".join(record.get("N1", []))}
        if record.get("L1"):
            link_record["attachments"] = [{"url": item, "role": "main_text"} if item.lower().startswith(("http://", "https://")) else {"path": item} for item in record["L1"]]
        result.append(_candidate(link_record, "ris", first("ID", "AN") or str(index), title=first("TI", "T1", "CT"),
            authors=_authors(record.get("AU", []) + record.get("A1", [])), year=_year(first("PY", "Y1", "DA")),
            doi=first("DO"), abstract=first("AB", "N2"), url=url, tags=_tags(record.get("KW", []))))
    return result


def _bibtex_records(text):
    try:
        import bibtexparser
        from bibtexparser.bparser import BibTexParser
        from bibtexparser.latexenc import latex_to_unicode
    except ImportError:
        raise ResearchError("DEPENDENCY", "BibTeX import requires bibtexparser>=1.4,<2") from None
    try:
        parser = BibTexParser(common_strings=True)
        parser.ignore_nonstandard_types = False
        entries = bibtexparser.loads(text, parser=parser).entries
    except Exception:
        raise ResearchError("INVALID_BIBLIOGRAPHY", "BibTeX could not be parsed") from None
    if not entries:
        raise ResearchError("INVALID_BIBLIOGRAPHY", "No BibTeX publication records were found")
    result = []
    for record in entries:
        if str(record.get("ENTRYTYPE", "")).lower() in NON_PUBLICATIONS or str(record.get("type", "")).lower() in NON_PUBLICATIONS:
            continue
        converted = {key: latex_to_unicode(value) if isinstance(value, str) and key not in ("url", "doi", "file") else value for key, value in record.items()}
        result.append(_candidate(converted, "bibtex", record.get("ID"), title=converted.get("title"),
            authors=_bib_authors(record.get("author", "")), year=_year(record.get("year") or record.get("date")),
            doi=record.get("doi"), abstract=converted.get("abstract"), url=record.get("url"),
            tags=_tags(converted.get("keywords") or converted.get("keyword"))))
    return result


def parse_bibliography(path, format=None) -> list[dict]:
    """Return normalized candidates/tags/warnings; never follow attachment paths."""
    try:
        source = Path(path).expanduser()
        if not source.is_file():
            raise ResearchError("NOT_FOUND", "Bibliography file does not exist")
        if source.stat().st_size > 50 * 1024 * 1024:
            raise ResearchError("INVALID_BIBLIOGRAPHY", "Bibliography exceeds the 50 MiB import limit; split it into files")
    except (TypeError, ValueError, OSError):
        raise ResearchError("INVALID_ARGUMENT", "Bibliography path is invalid or unreadable") from None
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ResearchError("INVALID_BIBLIOGRAPHY", "Bibliography must be a readable UTF-8 text file") from None
    if not text.strip():
        raise ResearchError("INVALID_BIBLIOGRAPHY", "Bibliography is empty")
    selected = str(format).lower() if format else {".bib": "bibtex", ".bibtex": "bibtex", ".ris": "ris", ".json": "json"}.get(source.suffix.lower())
    if selected is None:
        stripped = text.lstrip()
        selected = "json" if stripped.startswith(("[", "{")) else "bibtex" if stripped.startswith("@") else "ris" if re.match(r"TY\s*-", stripped) else None
    selected = FORMAT_ALIASES.get(selected)
    if selected is None:
        raise ResearchError("INVALID_ARGUMENT", "Expected BibTeX, RIS, CSL JSON or Zotero API JSON")
    if selected == "bibtex":
        return _bibtex_records(text)
    if selected == "ris":
        return _ris_records(text)
    try:
        records = json.loads(text)
    except (ValueError, RecursionError):
        raise ResearchError("INVALID_BIBLIOGRAPHY", "Bibliography JSON is invalid") from None
    return _json_records(records, selected)
