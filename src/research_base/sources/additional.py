"""Publication-only complements for OA journals and repository publications."""
from __future__ import annotations

from urllib.parse import parse_qs, quote, urlsplit

from research_base.models import Candidate, MaterialLink, ResearchError, SourcePage

from .registry import Transport, clean, doi, integer_cursor, unique, year


def page_number(cursor: str | None) -> int:
    number = integer_cursor(cursor) if cursor is not None else 1
    if number < 1:
        raise ResearchError("INVALID_ARGUMENT", "This source uses page numbers starting at 1.")
    return number


class DOAJ:
    info = {"name": "doaj", "label": "DOAJ Articles", "scope": "multilingual open-access journal articles",
            "resource_kind": "publication", "pagination": "page number; up to 50 per page",
            "optional_key_env": None, "documentation": "https://doaj.org/api/v4/docs"}

    def __init__(self, transport: Transport):
        self.t = transport

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        number, size = page_number(cursor), min(page_size, 50)
        native = query
        warnings = []
        if date_from or date_to:
            native = f"({query}) AND bibjson.year:[{(date_from or '1000')[:4]} TO {(date_to or '3000')[:4]}]"
            warnings.append("DOAJ date filters use publication year; month/day boundaries are not guaranteed.")
        data = self.t.json("doaj", "https://doaj.org/api/search/articles/" + quote(native, safe=""),
                           params={"page": number, "pageSize": size})
        if not isinstance(data.get("results"), list) or "total" not in data:
            raise ResearchError("SOURCE_INVALID_RESPONSE", "DOAJ response has no article result list.")
        raw = data.get("results") or []
        total = int(data.get("total", 0))
        result = []
        for item in raw:
            bib = item.get("bibjson") or {}
            identifiers = {"doaj": item["id"]}
            value = next((doi(x.get("id")) for x in bib.get("identifier", []) if x.get("type") == "doi"), None)
            links, pdfs = [], []
            for link in bib.get("link", []):
                url = link.get("url")
                if not url or link.get("type") not in ("fulltext", None):
                    continue
                fmt = "pdf" if "pdf" in str(link.get("content_type", "")).lower() or urlsplit(url).path.lower().endswith(".pdf") else "url"
                links.append(MaterialLink(role="main_text", format=fmt, url=url))
                if fmt == "pdf":
                    pdfs.append(url)
            journal = bib.get("journal") or {}
            result.append(Candidate(source="doaj", source_id=item["id"], title=clean(bib.get("title")),
                                    authors=[clean(a.get("name")) for a in bib.get("author", [])],
                                    year=year(bib.get("year")), doi=value, abstract=clean(bib.get("abstract")),
                                    identifiers=identifiers, version_kind="published",
                                    landing_url=links[0].url if links else f"https://doaj.org/article/{item['id']}",
                                    fulltext_urls=unique(pdfs), links=links,
                                    extra={"resource_kind": "publication", "resource_type_general": "JournalArticle",
                                           "journal": journal.get("title"), "journal_country": journal.get("country"),
                                           "languages": journal.get("language") or []}))
        next_cursor = str(number + 1) if raw and (number - 1) * size + len(raw) < total else None
        return SourcePage(items=result, next_cursor=next_cursor, total=total, warnings=warnings)


class DataCitePublications:
    """DataCite publications only. Dataset/software discovery needs a separate API."""

    resource_types = {"Text", "Preprint", "JournalArticle", "ConferencePaper", "Report", "Dissertation"}
    type_filter = "text,preprint,journal-article,conference-paper,report,dissertation"
    info = {"name": "datacite", "label": "DataCite Publications", "scope": "repository publications, preprints, reports and dissertations; excludes datasets/software",
            "resource_kind": "publication", "pagination": "relevance-ranked page number; first 10000 matches",
            "optional_key_env": None, "documentation": "https://support.datacite.org/docs/api-queries"}

    def __init__(self, transport: Transport):
        self.t = transport

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        number, size = page_number(cursor), min(page_size, 1000)
        if (number - 1) * size >= 10000:
            raise ResearchError("SOURCE_RESULT_LIMIT", "DataCite relevance pagination exposes the first 10000 matches; narrow the query or years.",
                                details={"source": "datacite", "limit": 10000})
        native = query
        warnings = ["DataCite source is restricted to publication types; datasets and software are excluded from paper counts."]
        if date_from or date_to:
            native = f"({query}) AND publicationYear:[{(date_from or '1000')[:4]} TO {(date_to or '3000')[:4]}]"
            warnings.append("DataCite date filters use publicationYear; month/day boundaries are not guaranteed.")
        data = self.t.json("datacite", "https://api.datacite.org/dois", params={
            "query": native, "resource-type-id": self.type_filter,
            "page[number]": number, "page[size]": size, "sort": "relevance"})
        if not isinstance(data.get("data"), list) or "total" not in (data.get("meta") or {}):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "DataCite response has no publication result list.")
        raw = data.get("data") or []
        result, excluded = [], 0
        for item in raw:
            attrs = item.get("attributes") or {}
            types = attrs.get("types") or {}
            resource_type = types.get("resourceTypeGeneral")
            if resource_type not in self.resource_types:
                excluded += 1
                continue
            value = doi(attrs.get("doi") or item["id"])
            titles = attrs.get("titles") or []
            title = next((clean(t.get("title")) for t in titles if t.get("title")), "")
            descriptions = attrs.get("descriptions") or []
            abstract = next((clean(d.get("description")) for d in descriptions if d.get("descriptionType") == "Abstract"), "")
            identifiers = {"datacite": value}
            links, pdfs = [], []
            content_urls = attrs.get("contentUrl") or []
            if isinstance(content_urls, str):
                content_urls = [content_urls]
            for url in content_urls:
                if not isinstance(url, str):
                    continue
                fmt = "pdf" if urlsplit(url).path.lower().endswith(".pdf") else "url"
                links.append(MaterialLink(role="main_text", format=fmt, url=url))
                if fmt == "pdf":
                    pdfs.append(url)
            kind = "preprint" if resource_type == "Preprint" else "unknown"
            # This DOI identifies the arXiv preprint itself, not a journal edition.
            if value and value.startswith("10.48550/arxiv."):
                arxiv_id = value.split("10.48550/arxiv.", 1)[1]
                identifiers["arxiv"] = arxiv_id
                pdfs.append(f"https://arxiv.org/pdf/{arxiv_id}")
                kind = "preprint"
            result.append(Candidate(source="datacite", source_id=value, title=title,
                                    authors=[clean(a.get("name") or " ".join(filter(None, [a.get("givenName"), a.get("familyName")])))
                                             for a in attrs.get("creators", [])],
                                    year=year(attrs.get("publicationYear")), doi=value, abstract=abstract,
                                    identifiers=identifiers, version_kind=kind,
                                    landing_url=attrs.get("url") or f"https://doi.org/{value}", fulltext_urls=unique(pdfs), links=links,
                                    extra={"resource_kind": "publication", "resource_type_general": resource_type,
                                           "resource_type": types.get("resourceType")}))
        if excluded:
            warnings.append(f"Excluded {excluded} non-publication DataCite records despite the requested type filter.")
        total = int((data.get("meta") or {}).get("total", 0))
        next_link = (data.get("links") or {}).get("next")
        next_cursor = None
        if next_link and raw:
            parsed = parse_qs(urlsplit(next_link).query)
            next_cursor = str(page_number((parsed.get("page[number]") or [str(number + 1)])[0]))
        return SourcePage(items=result, next_cursor=next_cursor, total=total, warnings=warnings)
