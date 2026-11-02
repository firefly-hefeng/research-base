from __future__ import annotations

import os
import re
import time
from datetime import date
from html import unescape
from typing import Any, Protocol
from xml.etree import ElementTree as ET

import httpx

from research_base.models import Candidate, MaterialLink, ResearchError, SourcePage


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def xml_text(node: ET.Element | None) -> str:
    return clean(" ".join(node.itertext())) if node is not None else ""


def year(value: Any) -> int | None:
    match = re.search(r"\b(18|19|20)\d{2}\b", str(value or ""))
    return int(match.group()) if match else None


def doi(value: Any) -> str | None:
    text = str(value or "").strip()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text, flags=re.I)
    return text.lower() or None


def unique(values: list[str | None]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def integer_cursor(cursor: str | None) -> int:
    try:
        result = int(cursor or "0")
        if result < 0:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise ResearchError("INVALID_ARGUMENT", "Pagination cursor must be a non-negative integer.") from None


def require_count(value: Any, source: str) -> int:
    """Accept provider integer/string counts, never missing/boolean/negative values."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ResearchError("SOURCE_INVALID_RESPONSE", f"{source} response has no valid result count.")
    try:
        count = int(value)
    except ValueError:
        raise ResearchError("SOURCE_INVALID_RESPONSE", f"{source} response has no valid result count.") from None
    if count < 0:
        raise ResearchError("SOURCE_INVALID_RESPONSE", f"{source} returned a negative result count.")
    return count


def require_list(value: Any, source: str) -> list:
    if not isinstance(value, list):
        raise ResearchError("SOURCE_INVALID_RESPONSE", f"{source} response has no valid result list.")
    return value


class SourceAdapter(Protocol):
    info: dict[str, Any]

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage: ...


class Transport:
    """Requests never include response bodies or secret-bearing URLs in errors."""

    intervals = {"arxiv": 3.1, "pubmed": 0.35, "semantic_scholar": 1.1,
                 "openalex": 0.1, "crossref": 0.1, "europe_pmc": 0.1}

    def __init__(self, client: httpx.Client, throttle: bool = True):
        self.client = client
        self.throttle = throttle
        self.last_request: dict[str, float] = {}
        self.request_count = 0

    def get(self, source: str, url: str, *, params: dict | None = None,
            headers: dict | None = None) -> httpx.Response:
        interval = self.intervals.get(source, 0.1)
        if source == "pubmed" and os.getenv("NCBI_API_KEY"):
            interval = 0.11
        for attempt in range(3):
            if self.throttle:
                remaining = interval - (time.monotonic() - self.last_request.get(source, 0))
                if remaining > 0:
                    time.sleep(remaining)
            self.last_request[source] = time.monotonic()
            self.request_count += 1
            try:
                response = self.client.get(url, params=params, headers=headers,
                                           timeout=30, follow_redirects=True)
            except httpx.HTTPError:
                if attempt < 2:
                    if self.throttle:
                        time.sleep(0.25 * (attempt + 1))
                    continue
                raise ResearchError("SOURCE_UNAVAILABLE", f"{source} request failed.",
                                    retryable=True, details={"source": source}) from None
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                # Let the scheduler resume after long provider-directed waits.
                raise ResearchError("SOURCE_RATE_LIMITED", f"{source} rate limited the request.",
                                    retryable=True, details={"source": source, "retry_after": retry_after})
            if response.status_code >= 500 and attempt < 2:
                if self.throttle:
                    time.sleep(0.25 * (attempt + 1))
                continue
            if not response.is_success:
                raise ResearchError("SOURCE_UNAVAILABLE", f"{source} returned HTTP {response.status_code}.",
                                    retryable=response.status_code >= 500,
                                    details={"source": source, "http_status": response.status_code})
            return response
        raise AssertionError("unreachable")

    def json(self, source: str, url: str, **kwargs: Any) -> dict:
        try:
            value = self.get(source, url, **kwargs).json()
        except ValueError:
            raise ResearchError("SOURCE_INVALID_RESPONSE", f"{source} returned malformed JSON.") from None
        if not isinstance(value, dict):
            raise ResearchError("SOURCE_INVALID_RESPONSE", f"{source} returned an unexpected JSON shape.")
        if value.get("error") or value.get("errors"):
            raise ResearchError("SOURCE_INVALID_RESPONSE", f"{source} returned an error payload.",
                                details={"source": source})
        return value


class OpenAlex:
    info = {"name": "openalex", "label": "OpenAlex", "scope": "multidisciplinary",
            "pagination": "basic (keyless, first 10000) / cursor (key)", "optional_key_env": "OPENALEX_API_KEY",
            "documentation": "https://help.openalex.org/api/"}

    def __init__(self, transport: Transport):
        self.t = transport

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        size = min(page_size, 100)
        params: dict[str, Any] = {"search": query, "per-page": size}
        basic_page = None
        if cursor and cursor.startswith("oa-basic:"):
            try:
                _, number, saved_size = cursor.split(":")
                basic_page = int(number)
                if basic_page < 1 or int(saved_size) != size:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ResearchError("INVALID_ARGUMENT", "Invalid OpenAlex basic paging state or changed page size") from None
        elif not os.getenv("OPENALEX_API_KEY"):
            if cursor and cursor != "*":
                raise ResearchError("SOURCE_AUTH_REQUIRED", "OpenAlex remote cursors require OPENALEX_API_KEY; configure a key or start a new keyless search.")
            basic_page = 1
        if basic_page is not None:
            if basic_page * size > 10000:
                raise ResearchError("SOURCE_RESULT_LIMIT", "OpenAlex basic paging reached 10000 records; narrow the query or start cursor paging with a free API key.")
            params["page"] = basic_page
        else:
            params["cursor"] = cursor or "*"
        filters = []
        if date_from:
            filters.append(f"from_publication_date:{date_from}")
        if date_to:
            filters.append(f"to_publication_date:{date_to}")
        if filters:
            params["filter"] = ",".join(filters)
        if os.getenv("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        data = self.t.json("openalex", "https://api.openalex.org/works", params=params)
        if not isinstance(data.get("meta"), dict):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "openalex response has no metadata envelope.")
        total = require_count(data["meta"].get("count"), "openalex")
        raw = require_list(data.get("results"), "openalex")
        if basic_page is not None and not raw and total > (basic_page - 1) * size:
            raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex returned an empty basic page before its reported total.")
        result = []
        for item in raw:
            source_id = str(item["id"]).rsplit("/", 1)[-1]
            inv = item.get("abstract_inverted_index") or {}
            positions = [(pos, token) for token, pos_list in inv.items() for pos in pos_list]
            abstract = " ".join(token for _, token in sorted(positions))
            location = item.get("best_oa_location") or item.get("primary_location") or {}
            pdfs = unique([(item.get("content_urls") or {}).get("pdf"), location.get("pdf_url")]
                          + [loc.get("pdf_url") for loc in item.get("locations", []) if loc.get("is_oa")])
            ids = {"openalex": source_id}
            if item.get("ids", {}).get("pmid"):
                ids["pmid"] = str(item["ids"]["pmid"]).rsplit("/", 1)[-1]
            if item.get("ids", {}).get("pmcid"):
                ids["pmcid"] = str(item["ids"]["pmcid"]).rsplit("/", 1)[-1]
            links = []
            xml_url = (item.get("content_urls") or {}).get("grobid_xml")
            if xml_url:
                links.append(MaterialLink(role="main_text", format="xml", url=xml_url))
            kind = {"publishedVersion": "published", "acceptedVersion": "accepted",
                    "submittedVersion": "preprint"}.get(location.get("version"), "unknown")
            if item.get("type") == "preprint":
                kind = "preprint"
            result.append(Candidate(source="openalex", source_id=source_id, title=clean(item.get("title")),
                                    authors=[clean(a.get("author", {}).get("display_name"))
                                             for a in item.get("authorships", [])], year=item.get("publication_year"),
                                    doi=doi(item.get("doi")), abstract=abstract, identifiers=ids,
                                    version_kind=kind, landing_url=location.get("landing_page_url") or item.get("doi")
                                    or item.get("id"), fulltext_urls=pdfs, links=links,
                                    references=item.get("referenced_works") or []))
        if basic_page is not None:
            next_cursor = f"oa-basic:{basic_page + 1}:{size}" if result and basic_page * size < total else None
            warnings = ["OpenAlex keyless basic paging covers at most 10000 results; indexed results can change between pages."]
        else:
            next_cursor = (data.get("meta") or {}).get("next_cursor") if result else None
            warnings = []
        return SourcePage(items=result, next_cursor=next_cursor, total=total, warnings=warnings)


class Crossref:
    info = {"name": "crossref", "label": "Crossref", "scope": "DOI-registered publications",
            "pagination": "cursor", "optional_key_env": None,
            "documentation": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/"}

    def __init__(self, transport: Transport):
        self.t = transport

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        # With deep cursors the provider's implicit sort can differ from a plain
        # query. Explicit relevance avoids filling a small budget with recent,
        # weakly matching records; publication date remains a filter below.
        params: dict[str, Any] = {"query.bibliographic": query, "cursor": cursor or "*",
                                  "rows": min(page_size, 1000), "sort": "score", "order": "desc"}
        filters = []
        if date_from:
            filters.append(f"from-pub-date:{date_from}")
        if date_to:
            filters.append(f"until-pub-date:{date_to}")
        if filters:
            params["filter"] = ",".join(filters)
        envelope = self.t.json("crossref", "https://api.crossref.org/works", params=params)
        if envelope.get("status", "ok") != "ok" or not isinstance(envelope.get("message"), dict):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "crossref response has no successful works envelope.")
        data = envelope["message"]
        if data.get("error") or data.get("errors"):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "crossref returned an error payload.")
        raw = require_list(data.get("items"), "crossref")
        total = require_count(data.get("total-results"), "crossref")
        result = []
        for item in raw:
            value = doi(item.get("DOI"))
            if not value:
                continue
            date_parts = (item.get("published") or item.get("issued") or {}).get("date-parts") or [[]]
            pdfs, links = [], []
            for link in item.get("link", []):
                url = link.get("URL")
                if not url:
                    continue
                fmt = "pdf" if "pdf" in link.get("content-type", "").lower() or url.lower().endswith(".pdf") else "url"
                if fmt == "pdf":
                    pdfs.append(url)
                links.append(MaterialLink(url=url, format=fmt))
            typ = item.get("type")
            kind = "preprint" if typ == "posted-content" else (
                "published" if typ in {"journal-article", "proceedings-article", "book-chapter", "book"} else "unknown")
            result.append(Candidate(source="crossref", source_id=value, title=clean((item.get("title") or [""])[0]),
                                    authors=[clean(" ".join(filter(None, [a.get("given"), a.get("family")]))) or clean(a.get("name"))
                                             for a in item.get("author", [])], year=year(date_parts[0][0] if date_parts[0] else None),
                                    doi=value, abstract=clean(item.get("abstract")), identifiers={"crossref": value},
                                    version_kind=kind, landing_url=item.get("URL") or f"https://doi.org/{value}",
                                    fulltext_urls=unique(pdfs), links=links,
                                    references=[str(ref["DOI"]) for ref in item.get("reference", []) if ref.get("DOI")]))
        return SourcePage(items=result, next_cursor=data.get("next-cursor") if result else None,
                          total=total)


class Arxiv:
    info = {"name": "arxiv", "label": "arXiv", "scope": "physics, mathematics, computing and related preprints",
            "pagination": "offset", "optional_key_env": None,
            "documentation": "https://info.arxiv.org/help/api/user-manual.html"}

    def __init__(self, transport: Transport):
        self.t = transport

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        start = integer_cursor(cursor)
        # Plain text is expanded to all: terms; advanced arXiv field syntax stays intact.
        if re.search(r"\b(?:all|ti|au|abs|cat|id):", query):
            native = query
        else:
            terms = re.findall(r'"[^"]+"|\(|\)|[^\s()]+', query)
            expanded = []
            previous_operand = False
            for term in terms:
                if term in {"AND", "OR", "ANDNOT"}:
                    expanded.append(term)
                    previous_operand = False
                elif term == "(":
                    if previous_operand:
                        expanded.append("AND")
                    expanded.append(term)
                    previous_operand = False
                elif term == ")":
                    expanded.append(term)
                    previous_operand = True
                else:
                    if previous_operand:
                        expanded.append("AND")
                    expanded.append(f"all:{term}")
                    previous_operand = True
            native = " ".join(expanded)
        if date_from or date_to:
            lo = (date_from or "1900-01-01").replace("-", "") + "0000"
            hi = (date_to or date.today().isoformat()).replace("-", "") + "2359"
            native = f"({native}) AND submittedDate:[{lo} TO {hi}]"
        params = {"search_query": native, "start": start, "max_results": min(page_size, 1000),
                  "sortBy": "submittedDate", "sortOrder": "descending"}
        root = ET.fromstring(self.t.get("arxiv", "https://export.arxiv.org/api/query", params=params).content)
        ns = {"a": "http://www.w3.org/2005/Atom", "op": "http://a9.com/-/spec/opensearch/1.1/",
              "ax": "http://arxiv.org/schemas/atom"}
        if root.tag != "{http://www.w3.org/2005/Atom}feed":
            raise ResearchError("SOURCE_INVALID_RESPONSE", "arxiv returned an unexpected XML envelope.")
        total = require_count(root.findtext("op:totalResults", None, ns), "arxiv")
        result = []
        for entry in root.findall("a:entry", ns):
            url = entry.findtext("a:id", "", ns)
            if url.endswith("/errors"):
                raise ResearchError("SOURCE_INVALID_QUERY", "arXiv rejected the search query.")
            arxiv_id = url.rsplit("/abs/", 1)[-1]
            links = entry.findall("a:link", ns)
            pdfs = unique([link.get("href") for link in links if link.get("title") == "pdf"
                           or link.get("type") == "application/pdf"])
            if not pdfs and arxiv_id:
                pdfs = [f"https://arxiv.org/pdf/{arxiv_id}"]
            result.append(Candidate(source="arxiv", source_id=arxiv_id,
                                    title=xml_text(entry.find("a:title", ns)),
                                    authors=[xml_text(node) for node in entry.findall("a:author/a:name", ns)],
                                    year=year(entry.findtext("a:published", "", ns)),
                                    doi=None,
                                    abstract=xml_text(entry.find("a:summary", ns)),
                                    identifiers={"arxiv": arxiv_id}, version_kind="preprint",
                                    landing_url=url, fulltext_urls=pdfs,
                                    extra={"published_doi": doi(entry.findtext("ax:doi", "", ns))}))
        offset = start + len(result)
        return SourcePage(items=result, next_cursor=str(offset) if result and offset < total else None, total=total,
                          warnings=["arXiv date filtering uses submission date, not journal publication date."]
                          if date_from or date_to else [])


class EuropePMC:
    info = {"name": "europe_pmc", "label": "Europe PMC", "scope": "life sciences and biomedical literature",
            "pagination": "cursor", "optional_key_env": None,
            "documentation": "https://europepmc.org/RestfulWebService"}

    def __init__(self, transport: Transport):
        self.t = transport

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        native = query
        if date_from or date_to:
            native = f"({query}) AND FIRST_PDATE:[{date_from or '1000-01-01'} TO {date_to or '3000-12-31'}]"
        params = {"query": native, "format": "json", "resultType": "core", "pageSize": min(page_size, 1000),
                  "cursorMark": cursor or "*"}
        data = self.t.json("europe_pmc", "https://www.ebi.ac.uk/europepmc/webservices/rest/search", params=params)
        if not isinstance(data.get("resultList"), dict):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "europe_pmc response has no result envelope.")
        total = require_count(data.get("hitCount"), "europe_pmc")
        result_list = data["resultList"]
        if result_list.get("error") or result_list.get("errors"):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "europe_pmc returned an error payload.")
        raw = require_list(result_list.get("result", [] if total == 0 else None), "europe_pmc")
        result = []
        for item in raw:
            source_id = f"{item.get('source', 'UNKNOWN')}:{item['id']}"
            ids = {"europe_pmc": source_id}
            if item.get("source") == "MED":
                ids["pmid"] = str(item["id"])
            if item.get("pmcid"):
                ids["pmcid"] = item["pmcid"]
            pdfs, links = [], []
            for link in (item.get("fullTextUrlList") or {}).get("fullTextUrl", []):
                url = link.get("url")
                if not url:
                    continue
                fmt = "pdf" if str(link.get("documentStyle", "")).lower() == "pdf" else "url"
                links.append(MaterialLink(url=url, format=fmt))
                if fmt == "pdf":
                    pdfs.append(url)
            if item.get("pmcid") and item.get("isOpenAccess") == "Y":
                links.append(MaterialLink(format="xml", url=f"https://www.ebi.ac.uk/europepmc/webservices/rest/{item['pmcid']}/fullTextXML"))
            authors = [a.get("fullName") or clean(" ".join(filter(None, [a.get("firstName"), a.get("lastName")])))
                       for a in (item.get("authorList") or {}).get("author", [])]
            if not authors and item.get("authorString"):
                authors = [a.strip() for a in item["authorString"].split(",")]
            types = [t.lower() for t in (item.get("pubTypeList") or {}).get("pubType", [])]
            kind = "preprint" if "preprint" in types or item.get("source") == "PPR" else "unknown"
            result.append(Candidate(source="europe_pmc", source_id=source_id, title=clean(item.get("title")),
                                    authors=authors, year=year(item.get("pubYear")), doi=doi(item.get("doi")),
                                    abstract=clean(item.get("abstractText")), identifiers=ids, version_kind=kind,
                                    landing_url=f"https://europepmc.org/article/{item.get('source', 'MED')}/{item['id']}",
                                    fulltext_urls=unique(pdfs), links=links))
        next_cursor = data.get("nextCursorMark")
        if not result or next_cursor == (cursor or "*"):
            next_cursor = None
        return SourcePage(items=result, next_cursor=next_cursor, total=total)


class PubMed:
    info = {"name": "pubmed", "label": "PubMed", "scope": "biomedical and life-science citations",
            "pagination": "offset; first 10000 matches per query", "optional_key_env": "NCBI_API_KEY",
            "documentation": "https://dataguide.nlm.nih.gov/eutilities/utilities.html"}

    def __init__(self, transport: Transport):
        self.t = transport

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        start = integer_cursor(cursor)
        if start >= 10000:
            raise ResearchError("SOURCE_RESULT_LIMIT", "PubMed ESearch is limited to the first 10000 matches; narrow the date range or query.",
                                details={"source": "pubmed", "limit": 10000})
        common = {"db": "pubmed", "tool": "research-base"}
        if os.getenv("NCBI_API_KEY"):
            common["api_key"] = os.environ["NCBI_API_KEY"]
        if os.getenv("NCBI_EMAIL"):
            common["email"] = os.environ["NCBI_EMAIL"]
        params = {**common, "term": query, "retmode": "json", "retstart": start,
                  "retmax": min(page_size, 1000, 10000 - start), "sort": "pub date"}
        if date_from or date_to:
            params.update({"datetype": "pdat", "mindate": date_from or "1000/01/01",
                           "maxdate": date_to or date.today().isoformat()})
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        data = self.t.json("pubmed", base + "esearch.fcgi", params=params)
        if not isinstance(data.get("esearchresult"), dict):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "pubmed response has no ESearch envelope.")
        esearch = data["esearchresult"]
        if esearch.get("errorlist") or esearch.get("error") or esearch.get("errors"):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "pubmed ESearch returned an error payload.")
        ids = require_list(esearch.get("idlist"), "pubmed")
        total = require_count(esearch.get("count"), "pubmed")
        if any(not isinstance(value, str) or not value.isdigit() for value in ids) or len(set(ids)) != len(ids):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "pubmed returned invalid record identifiers.")
        warnings = ["PubMed shares substantial coverage with Europe PMC; sources are not independent corpora."]
        translation = esearch.get("querytranslation")
        if translation:
            # Expose NCBI's automatic term mapping instead of silently replacing
            # its semantics with an application-specific AND/phrase policy.
            warnings.append("PubMed ESearch querytranslation: " + str(translation))
        for key, values in (esearch.get("warninglist") or {}).items():
            if isinstance(values, list):
                warnings.append(f"PubMed ESearch {key}: " + "; ".join(str(v) for v in values))
            elif values:
                warnings.append(f"PubMed ESearch {key}: {values}")
        if not ids:
            if start < total:
                raise ResearchError("SOURCE_INVALID_RESPONSE", "pubmed ESearch returned an empty page before its reported total.", retryable=True,
                                    details={"source": "pubmed", "offset": start, "total": total})
            return SourcePage(total=total, warnings=warnings)
        root = ET.fromstring(self.t.get("pubmed", base + "efetch.fcgi",
                                       params={**common, "id": ",".join(ids), "retmode": "xml"}).content)
        if root.tag != "PubmedArticleSet" or root.find(".//ERROR") is not None:
            raise ResearchError("SOURCE_INVALID_RESPONSE", "pubmed EFetch returned an unexpected XML envelope.")
        result = []
        for article in root.findall(".//PubmedArticle"):
            citation = article.find("MedlineCitation")
            if citation is None:
                continue
            pmid = citation.findtext("PMID", "")
            body = citation.find("Article")
            if body is None:
                continue
            identifiers = {"pmid": pmid, "pubmed": pmid}
            value = None
            links = []
            for node in article.findall("PubmedData/ArticleIdList/ArticleId"):
                text = (node.text or "").strip()
                if node.get("IdType") == "doi":
                    value = doi(text)
                elif node.get("IdType") == "pmc":
                    identifiers["pmcid"] = text
                    links.append(MaterialLink(format="url", url=f"https://pmc.ncbi.nlm.nih.gov/articles/{text}/"))
            authors = []
            for node in body.findall("AuthorList/Author"):
                authors.append(clean(node.findtext("CollectiveName")) or clean(" ".join(filter(None, [
                    node.findtext("ForeName") or node.findtext("Initials"), node.findtext("LastName")]))))
            abstract = " ".join(xml_text(node) for node in body.findall("Abstract/AbstractText"))
            published = body.find("Journal/JournalIssue/PubDate")
            pub_year = year(xml_text(published))
            types = [xml_text(node).lower() for node in body.findall("PublicationTypeList/PublicationType")]
            result.append(Candidate(source="pubmed", source_id=pmid, title=xml_text(body.find("ArticleTitle")),
                                    authors=authors, year=pub_year, doi=value, abstract=abstract, identifiers=identifiers,
                                    version_kind="preprint" if "preprint" in types else "unknown",
                                    landing_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", links=links))
        returned = [item.identifiers["pmid"] for item in result]
        requested = set(ids)
        if set(returned) != requested or len(returned) != len(requested):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "pubmed EFetch did not return exactly the requested identifiers.", retryable=True,
                                details={"source": "pubmed", "requested_count": len(requested), "returned_count": len(returned),
                                         "missing_pmids": sorted(requested - set(returned)), "unexpected_pmids": sorted(set(returned) - requested),
                                         "duplicate_pmids": sorted(value for value in set(returned) if returned.count(value) > 1)})
        offset = start + len(ids)
        if total > 10000:
            warnings.append("PubMed ESearch exposes at most 10000 matches per query; partition by date for broader retrieval.")
        return SourcePage(items=result, next_cursor=str(offset) if offset < total else None,
                          total=total, warnings=warnings)


class SemanticScholar:
    info = {"name": "semantic_scholar", "label": "Semantic Scholar", "scope": "multidisciplinary scholarly graph",
            "pagination": "bulk token; provider-sized batches up to 1000", "optional_key_env": "SEMANTIC_SCHOLAR_API_KEY",
            "documentation": "https://api.semanticscholar.org/api-docs/graph"}

    def __init__(self, transport: Transport):
        self.t = transport

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        params = {"query": query, "fields": "title,authors,year,abstract,externalIds,openAccessPdf,url,publicationDate,publicationTypes",
                  "sort": "publicationDate:desc"}
        if cursor:
            params["token"] = cursor
        if date_from or date_to:
            params["publicationDateOrYear"] = f"{date_from or ''}:{date_to or ''}"
        headers = {}
        if os.getenv("SEMANTIC_SCHOLAR_API_KEY"):
            headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
        data = self.t.json("semantic_scholar", "https://api.semanticscholar.org/graph/v1/paper/search/bulk",
                           params=params, headers=headers)
        raw = require_list(data.get("data"), "semantic_scholar")
        total = require_count(data.get("total"), "semantic_scholar")
        result = []
        for item in raw:
            source_id = item["paperId"]
            external = item.get("externalIds") or {}
            identifiers = {"semantic_scholar": source_id}
            for key, namespace in [("ArXiv", "arxiv"), ("PubMed", "pmid"), ("PubMedCentral", "pmcid")]:
                if external.get(key):
                    identifiers[namespace] = str(external[key])
            kind = "preprint" if "Preprint" in (item.get("publicationTypes") or []) else "unknown"
            pdf = (item.get("openAccessPdf") or {}).get("url")
            result.append(Candidate(source="semantic_scholar", source_id=source_id, title=clean(item.get("title")),
                                    authors=[clean(a.get("name")) for a in item.get("authors", [])],
                                    year=item.get("year"), doi=doi(external.get("DOI")), abstract=clean(item.get("abstract")),
                                    identifiers=identifiers, version_kind=kind, landing_url=item.get("url"),
                                    fulltext_urls=[pdf] if pdf else []))
        warnings = ["Semantic Scholar bulk pagination uses provider-sized batches up to 1000; page_size is a hint and no results are discarded."]
        return SourcePage(items=result, next_cursor=data.get("token") if result else None,
                          total=total, warnings=warnings)


class SourceRegistry:
    """Explicit source registry. One page may require several HTTP requests."""

    def __init__(self, client: httpx.Client | None = None, *, throttle: bool = True):
        from .additional import DOAJ, DataCitePublications

        self._owned_client = client is None
        self.client = client or httpx.Client(headers={"User-Agent": "ResearchBase/0.1 (scholarly metadata retrieval)"})
        self.transport = Transport(self.client, throttle=throttle)
        self.adapters: dict[str, SourceAdapter] = {}
        for cls in [OpenAlex, Crossref, Arxiv, EuropePMC, PubMed, SemanticScholar, DOAJ, DataCitePublications]:
            adapter = cls(self.transport)
            self.register(adapter.info["name"], adapter)

    def register(self, name: str, adapter: SourceAdapter) -> None:
        if not name or not callable(getattr(adapter, "search", None)):
            raise ResearchError("INVALID_ARGUMENT", "Source adapter must provide search().")
        self.adapters[name] = adapter

    def list_sources(self) -> list[dict]:
        return [dict(adapter.info, name=name, resource_kind=adapter.info.get("resource_kind", "publication"))
                for name, adapter in self.adapters.items()]

    @property
    def request_count(self) -> int:
        return self.transport.request_count

    def search_page(self, name: str, query: str, *, cursor: str | None = None,
                    page_size: int = 25, date_from: str | None = None,
                    date_to: str | None = None) -> SourcePage:
        if name == "europepmc":
            name = "europe_pmc"
        if name not in self.adapters:
            raise ResearchError("INVALID_ARGUMENT", "Unknown literature source.", details={"source": name})
        if not isinstance(query, str) or not query.strip() or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise ResearchError("INVALID_ARGUMENT", "Query must be nonempty and page_size must be between 1 and 1000.")
        for value in [date_from, date_to]:
            if value:
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError):
                    raise ResearchError("INVALID_ARGUMENT", "Date filters must use YYYY-MM-DD.") from None
        if date_from and date_to and date_from > date_to:
            raise ResearchError("INVALID_ARGUMENT", "date_from must not follow date_to.")
        try:
            return self.adapters[name].search(query, cursor=cursor, page_size=page_size,
                                              date_from=date_from, date_to=date_to)
        except ResearchError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, ET.ParseError):
            # Never include provider data or HTTP URL: either may contain credentials.
            raise ResearchError("SOURCE_INVALID_RESPONSE", f"{name} returned an unsupported response shape.",
                                details={"source": name}) from None

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    def __enter__(self) -> "SourceRegistry":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
