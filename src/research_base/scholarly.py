"""Verified seed lookup and indexed citation neighborhoods.

Only provider responses become Candidates. A user-provided seed is a query,
never an automatically fabricated bibliographic record.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import date
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from .materials import normalized_doi
from .models import Candidate, MaterialLink, ResearchError, SourcePage
from .sources.registry import clean, doi, unique, xml_text, year


NON_PUBLICATIONS = {"dataset", "software", "grant", "peer-review", "paratext", "supplementary-material"}


def _title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in value if not unicodedata.combining(c)), re.UNICODE))


def _arxiv(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    text = re.sub(r"^(?:https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:)\s*", "", text, flags=re.I)
    text = re.sub(r"\.pdf$", "", text)
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?", text):
        raise ResearchError("INVALID_ARGUMENT", "Invalid arXiv seed identifier")
    return text


def _openalex(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"W\d+", text, re.I):
        raise ResearchError("INVALID_ARGUMENT", "Invalid OpenAlex work identifier")
    return text.upper()


def normalized_seed(seed: dict) -> dict:
    if not isinstance(seed, dict):
        raise ResearchError("INVALID_ARGUMENT", "A seed must be a bibliographic object")
    ids = seed.get("identifiers") or {}
    if not isinstance(ids, dict):
        raise ResearchError("INVALID_ARGUMENT", "Seed identifiers must be an object")
    result = {"title": str(seed.get("title") or "").strip(), "authors": seed.get("authors") or [], "year": seed.get("year")}
    result["doi"] = normalized_doi(seed.get("doi") or ids.get("doi"))
    if result["doi"] and not re.fullmatch(r"10\.\d{4,9}/\S+", result["doi"]):
        raise ResearchError("INVALID_ARGUMENT", "Invalid DOI seed identifier")
    result["arxiv"] = _arxiv(seed.get("arxiv") or ids.get("arxiv") or ids.get("arxiv_work"))
    if not result["arxiv"] and (result["doi"] or "").startswith("10.48550/arxiv."):
        result["arxiv"] = _arxiv(result["doi"].split("arxiv.", 1)[1])
    result["openalex"] = _openalex(seed.get("openalex") or ids.get("openalex"))
    pmid = seed.get("pmid") or ids.get("pmid")
    if pmid:
        pmid = str(pmid).strip().rstrip("/").rsplit("/", 1)[-1]
        if not pmid.isdigit():
            raise ResearchError("INVALID_ARGUMENT", "Invalid PMID seed identifier")
    result["pmid"] = pmid or None
    if result["year"] is not None and (not isinstance(result["year"], int) or isinstance(result["year"], bool)):
        raise ResearchError("INVALID_ARGUMENT", "Seed year must be an integer")
    if not isinstance(result["authors"], list) or any(not isinstance(a, str) for a in result["authors"]):
        raise ResearchError("INVALID_ARGUMENT", "Seed authors must be strings")
    if not any(result.get(k) for k in ("title", "doi", "arxiv", "pmid", "openalex")):
        raise ResearchError("INVALID_ARGUMENT", "Seed needs a title or a known identifier")
    if len(result["title"]) > 2000:
        raise ResearchError("INVALID_ARGUMENT", "Seed title is too long")
    return result


def seed_match(candidate: Candidate, seed: dict) -> str | None:
    """Return the identity basis, rejecting conflicting known identifiers."""
    expected = normalized_seed(seed)
    actual = {**candidate.identifiers, "doi": normalized_doi(candidate.doi)}
    actual_arxiv = actual.get("arxiv")
    if expected["openalex"] and actual.get("openalex") and expected["openalex"] == _openalex(actual["openalex"]):
        # The graph is work-level; a verified work ID need not use the same
        # canonical DOI as every cached preprint/publication record.
        return "openalex"
    arxiv_alias = False
    if (expected["doi"] or "").startswith("10.48550/arxiv.") and expected["arxiv"] and actual_arxiv:
        want_arxiv, got_arxiv = expected["arxiv"], _arxiv(actual_arxiv)
        arxiv_alias = want_arxiv == got_arxiv if re.search(r"v\d+$", want_arxiv) else re.sub(r"v\d+$", "", want_arxiv) == re.sub(r"v\d+$", "", got_arxiv)
    if expected["doi"] and actual.get("doi") and expected["doi"] != actual["doi"]:
        # A preprint may explicitly point to its publication DOI; otherwise conflict.
        if not arxiv_alias and normalized_doi(candidate.extra.get("published_doi")) != expected["doi"]:
            return None
    if expected["openalex"] and actual.get("openalex") and expected["openalex"] != _openalex(actual["openalex"]):
        return None
    if expected["pmid"] and actual.get("pmid") and expected["pmid"] != str(actual["pmid"]):
        return None
    if expected["arxiv"] and actual_arxiv:
        want, got = expected["arxiv"], _arxiv(actual_arxiv)
        if re.search(r"v\d+$", want):
            if want != got:
                return None
        elif re.sub(r"v\d+$", "", want) != re.sub(r"v\d+$", "", got):
            return None
        return "arxiv"
    for field in ("doi", "pmid", "openalex"):
        if expected[field] and expected[field] == actual.get(field):
            return field
    if expected["doi"] and normalized_doi(candidate.extra.get("published_doi")) == expected["doi"]:
        return "explicit_publication_relation"
    if any(expected[field] for field in ("doi", "arxiv", "pmid", "openalex")):
        return None
    wanted_title = _title(expected["title"])
    if not wanted_title or wanted_title != _title(candidate.title):
        return None
    # Very short generic headings are not useful identity evidence.
    if len(wanted_title) < 25 and len(wanted_title.split()) < 5 and len(re.findall(r"[\u3400-\u9fff]", wanted_title)) < 12:
        return None
    if expected["year"] is not None and candidate.year is not None and abs(expected["year"] - candidate.year) > 1:
        return None
    if expected["authors"]:
        wanted_names = {_title(a).split()[-1] for a in expected["authors"] if _title(a)}
        actual_names = {_title(a).split()[-1] for a in candidate.authors if _title(a)}
        if not wanted_names & actual_names:
            return None
    return "exact_title"


def candidate_from_openalex(item: dict) -> Candidate:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex returned an invalid work")
    try:
        wid = _openalex(item["id"])
    except ResearchError:
        raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex work has an invalid identifier") from None
    title = clean(item.get("title") or item.get("display_name"))
    if not title:
        raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex work has no title")
    ids = {"openalex": wid}
    for key in ("pmid", "pmcid"):
        value = (item.get("ids") or {}).get(key)
        if value:
            ids[key] = str(value).rstrip("/").rsplit("/", 1)[-1]
    work_doi = doi(item.get("doi"))
    if work_doi and work_doi.startswith("10.48550/arxiv."):
        ids["arxiv"] = work_doi.split("arxiv.", 1)[1]
    locations = item.get("locations") or []
    location = item.get("best_oa_location") or item.get("primary_location") or {}
    for loc in locations + [location]:
        landing = loc.get("landing_page_url") or ""
        if re.match(r"https?://(?:export\.)?arxiv\.org/abs/", landing):
            ids.setdefault("arxiv", _arxiv(landing))
    inverted = item.get("abstract_inverted_index") or {}
    if not isinstance(inverted, dict):
        raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex abstract is malformed")
    abstract = " ".join(token for _, token in sorted((pos, token) for token, positions in inverted.items() for pos in positions))
    pdfs = unique([(item.get("content_urls") or {}).get("pdf"), location.get("pdf_url")]
                  + [loc.get("pdf_url") for loc in locations if loc.get("is_oa")])
    kind = {"publishedVersion": "published", "acceptedVersion": "accepted", "submittedVersion": "preprint"}.get(location.get("version"), "unknown")
    if item.get("type") == "preprint" or (work_doi or "").startswith("10.48550/arxiv."):
        kind = "preprint"
    links = []
    xml = (item.get("content_urls") or {}).get("grobid_xml")
    if xml:
        links.append(MaterialLink(role="main_text", url=xml, format="xml"))
    references = item.get("referenced_works") or []
    if not isinstance(references, list):
        raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex references are malformed")
    return Candidate(source="openalex", source_id=wid, title=title,
                     authors=[clean((a.get("author") or {}).get("display_name")) for a in item.get("authorships") or []],
                     year=item.get("publication_year"), doi=work_doi, abstract=abstract, identifiers=ids,
                     version_kind=kind, landing_url=location.get("landing_page_url") or item.get("doi") or item["id"],
                     fulltext_urls=pdfs, links=links, references=references,
                     extra={"publication_date": item.get("publication_date"), "resource_type": item.get("type"), "citation_count": item.get("cited_by_count")})


def candidate_from_crossref(item: dict) -> Candidate:
    if not isinstance(item, dict) or not doi(item.get("DOI")) or not isinstance(item.get("title"), list) or not item["title"]:
        raise ResearchError("SOURCE_INVALID_RESPONSE", "Crossref returned an invalid work")
    value = doi(item["DOI"])
    typ = item.get("type")
    links, pdfs = [], []
    for link in item.get("link") or []:
        url = link.get("URL")
        if url:
            fmt = "pdf" if "pdf" in (link.get("content-type") or "").lower() or url.lower().endswith(".pdf") else "url"
            links.append(MaterialLink(url=url, format=fmt))
            if fmt == "pdf":
                pdfs.append(url)
    dates = (item.get("published") or item.get("issued") or {}).get("date-parts") or [[]]
    return Candidate(source="crossref", source_id=value, title=clean(item["title"][0]),
                     authors=[clean(" ".join(filter(None, (a.get("given"), a.get("family"))))) or clean(a.get("name")) for a in item.get("author") or []],
                     year=year(dates[0][0] if dates[0] else None), doi=value, abstract=clean(item.get("abstract")), identifiers={"crossref": value},
                     version_kind="preprint" if typ == "posted-content" else "published" if typ in {"journal-article", "proceedings-article", "book", "book-chapter"} else "unknown",
                     landing_url=item.get("URL") or f"https://doi.org/{value}", fulltext_urls=unique(pdfs), links=links,
                     references=[r["DOI"] for r in item.get("reference") or [] if r.get("DOI")], extra={"resource_type": typ})


class ScholarlyClient:
    SUPPORTED_LOOKUP = {"openalex", "crossref", "arxiv"}

    def __init__(self, client: httpx.Client, *, throttle=True, max_attempts=2):
        self.client = client
        self.throttle = throttle
        self.max_attempts = max(1, min(int(max_attempts), 3))
        self.requests_count = 0
        self._last_request = {}

    @property
    def request_count(self):
        return self.requests_count

    def _get(self, provider, url, params=None):
        params = dict(params or {})
        if provider == "openalex" and os.getenv("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        for attempt in range(self.max_attempts):
            if self.throttle:
                interval = 3.1 if provider == "arxiv" else .1
                pause = interval - (time.monotonic() - self._last_request.get(provider, 0))
                if pause > 0:
                    time.sleep(pause)
            self._last_request[provider] = time.monotonic()
            self.requests_count += 1
            try:
                response = self.client.get(url, params=params, timeout=25, follow_redirects=True)
            except httpx.HTTPError:
                if attempt + 1 < self.max_attempts:
                    continue
                raise ResearchError("SOURCE_UNAVAILABLE", f"{provider} seed request failed", retryable=True, details={"provider": provider}) from None
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                raise ResearchError("SOURCE_RATE_LIMITED", f"{provider} rate limited the request", retryable=True, details={"provider": provider})
            if response.status_code >= 500 and attempt + 1 < self.max_attempts:
                continue
            if not response.is_success:
                raise ResearchError("SOURCE_UNAVAILABLE", f"{provider} returned HTTP {response.status_code}", retryable=response.status_code >= 500,
                                    details={"provider": provider, "http_status": response.status_code})
            return response
        raise AssertionError("unreachable")

    def _json(self, provider, url, params=None):
        response = self._get(provider, url, params)
        if response is None:
            return None
        try:
            data = response.json()
        except ValueError:
            raise ResearchError("SOURCE_INVALID_RESPONSE", f"{provider} returned malformed JSON") from None
        if not isinstance(data, dict) or data.get("error") or data.get("errors"):
            raise ResearchError("SOURCE_INVALID_RESPONSE", f"{provider} returned an invalid or error JSON envelope")
        return data

    def _oa_one(self, identifier):
        data = self._json("openalex", "https://api.openalex.org/works/" + quote(identifier, safe="/:"))
        return [candidate_from_openalex(data)] if data else []

    def _oa_list(self, params):
        data = self._json("openalex", "https://api.openalex.org/works", params)
        if data is None:
            raise ResearchError("SOURCE_UNAVAILABLE", "OpenAlex list endpoint returned HTTP 404", details={"provider": "openalex", "http_status": 404})
        if not isinstance(data.get("results"), list) or not isinstance(data.get("meta"), dict):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex returned an invalid list envelope")
        count = data["meta"].get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex list has no valid count")
        return data

    def _cr_one(self, identifier):
        data = self._json("crossref", "https://api.crossref.org/works/" + quote(identifier, safe=""))
        if data is None:
            return []
        if data.get("status", "ok") != "ok" or not isinstance(data.get("message"), dict):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "Crossref returned an invalid singleton envelope")
        return [candidate_from_crossref(data["message"])]

    def _cr_title(self, title):
        data = self._json("crossref", "https://api.crossref.org/works", {"query.title": title, "rows": 5, "sort": "score", "order": "desc"})
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict) or not isinstance(message.get("items"), list):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "Crossref returned an invalid search envelope")
        return [candidate_from_crossref(item) for item in message["items"]]

    def _ax(self, identifier=None, title=None):
        params = {"id_list": identifier, "max_results": 5} if identifier else {"search_query": 'ti:"' + title.replace('"', ' ') + '"', "max_results": 5, "sortBy": "relevance"}
        response = self._get("arxiv", "https://export.arxiv.org/api/query", params)
        if response is None:
            return []
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise ResearchError("SOURCE_INVALID_RESPONSE", "arXiv returned malformed XML") from None
        ns = {"a": "http://www.w3.org/2005/Atom", "ax": "http://arxiv.org/schemas/atom", "op": "http://a9.com/-/spec/opensearch/1.1/"}
        if root.tag != "{http://www.w3.org/2005/Atom}feed" or root.find("op:totalResults", ns) is None:
            raise ResearchError("SOURCE_INVALID_RESPONSE", "arXiv returned an invalid Atom envelope")
        try:
            total = int(root.findtext("op:totalResults", "", ns))
            if total < 0:
                raise ValueError()
        except ValueError:
            raise ResearchError("SOURCE_INVALID_RESPONSE", "arXiv returned an invalid result count") from None
        items = []
        for entry in root.findall("a:entry", ns):
            url = entry.findtext("a:id", "", ns)
            if url.endswith("/errors"):
                raise ResearchError("SOURCE_INVALID_QUERY", "arXiv rejected the seed query")
            axid = _arxiv(url)
            pdfs = unique([l.get("href") for l in entry.findall("a:link", ns) if l.get("title") == "pdf" or l.get("type") == "application/pdf"])
            items.append(Candidate(source="arxiv", source_id=axid, title=xml_text(entry.find("a:title", ns)),
                                   authors=[xml_text(a) for a in entry.findall("a:author/a:name", ns)], year=year(entry.findtext("a:published", "", ns)),
                                   abstract=xml_text(entry.find("a:summary", ns)), identifiers={"arxiv": axid}, version_kind="preprint",
                                   landing_url=url, fulltext_urls=pdfs or [f"https://arxiv.org/pdf/{axid}"],
                                   extra={"published_doi": doi(entry.findtext("ax:doi", "", ns))}))
        return items

    def lookup(self, seed: dict, sources: list[str] | None = None) -> dict:
        wanted = normalized_seed(seed)
        if sources is not None and (not isinstance(sources, list) or not sources or set(sources) - self.SUPPORTED_LOOKUP):
            raise ResearchError("INVALID_ARGUMENT", "Seed lookup supports openalex, crossref and arxiv")
        selected = set(sources or self.SUPPORTED_LOOKUP)
        attempts, candidates, routes = [], [], []
        if wanted["openalex"] and "openalex" in selected:
            routes.append(("openalex", "openalex_id", lambda: self._oa_one(wanted["openalex"])))
        if wanted["doi"]:
            if "openalex" in selected:
                routes.append(("openalex", "doi", lambda: self._oa_one("https://doi.org/" + wanted["doi"])))
            if "crossref" in selected:
                routes.append(("crossref", "doi", lambda: self._cr_one(wanted["doi"])))
        if wanted["pmid"] and "openalex" in selected:
            routes.append(("openalex", "pmid", lambda: self._oa_one("pmid:" + wanted["pmid"])))
        if wanted["arxiv"] and "openalex" in selected:
            identifier = "https://doi.org/10.48550/arxiv." + re.sub(r"v\d+$", "", wanted["arxiv"])
            if identifier != "https://doi.org/" + (wanted["doi"] or ""):
                routes.append(("openalex", "arxiv_doi", lambda: self._oa_one(identifier)))
        if wanted["arxiv"] and "arxiv" in selected:
            routes.append(("arxiv", "arxiv_id", lambda: self._ax(identifier=wanted["arxiv"])))
        if wanted["title"]:
            if "openalex" in selected:
                routes.append(("openalex", "exact_title", lambda: [candidate_from_openalex(x) for x in self._oa_list({"search": wanted["title"], "per-page": 5})["results"]]))
            if "crossref" in selected:
                routes.append(("crossref", "exact_title", lambda: self._cr_title(wanted["title"])))
            if "arxiv" in selected:
                routes.append(("arxiv", "exact_title", lambda: self._ax(title=wanted["title"])))
        for provider, route, request in routes:
            before = self.requests_count
            try:
                returned = request()
                matched = [(item, seed_match(item, wanted)) for item in returned]
                candidates = [item for item, basis in matched if basis]
                attempts.append({"provider": provider, "route": route, "status": "matched" if candidates else "mismatch" if returned else "not_found",
                                 "returned_count": len(returned), "matched_count": len(candidates), "match_methods": list(dict.fromkeys(basis for _, basis in matched if basis)),
                                 "requests": self.requests_count - before})
                if candidates:
                    return {"candidates": candidates, "attempts": attempts, "matched": True}
            except ResearchError as error:
                attempts.append({"provider": provider, "route": route, "status": "error", "code": error.code, "retryable": error.retryable,
                                 "details": error.details, "requests": self.requests_count - before})
            except (ValueError, TypeError, AttributeError, KeyError):
                attempts.append({"provider": provider, "route": route, "status": "error", "code": "SOURCE_INVALID_RESPONSE", "requests": self.requests_count - before})
        return {"candidates": [], "attempts": attempts, "matched": False}

    def _graph_work(self, seed):
        wanted = normalized_seed(seed)
        routes = []
        if wanted["openalex"]:
            routes.append(wanted["openalex"])
        if wanted["doi"]:
            routes.append("https://doi.org/" + wanted["doi"])
        if wanted["pmid"]:
            routes.append("pmid:" + wanted["pmid"])
        if wanted["arxiv"]:
            # OpenAlex citations are work-level; revision-specific matching is not promised.
            routes.append("https://doi.org/10.48550/arxiv." + re.sub(r"v\d+$", "", wanted["arxiv"]))
        attempts = []
        work_seed = {**wanted, "arxiv": re.sub(r"v\d+$", "", wanted["arxiv"]) if wanted["arxiv"] else None}
        for identifier in routes:
            before = self.requests_count
            try:
                data = self._json("openalex", "https://api.openalex.org/works/" + quote(identifier, safe="/:"))
                if data:
                    candidate = candidate_from_openalex(data)
                    if seed_match(candidate, work_seed):
                        attempts.append({"provider": "openalex", "route": "graph_seed", "status": "matched", "requests": self.requests_count - before})
                        return data, attempts
                attempts.append({"provider": "openalex", "route": "graph_seed", "status": "not_found" if not data else "mismatch", "requests": self.requests_count - before})
            except ResearchError as error:
                attempts.append({"provider": "openalex", "route": "graph_seed", "status": "error", "code": error.code, "retryable": error.retryable,
                                 "details": error.details, "requests": self.requests_count - before})
        if not wanted["title"] and wanted["arxiv"]:
            arxiv_result = self.lookup(wanted, sources=["arxiv"])
            attempts.extend(arxiv_result["attempts"])
            if arxiv_result["matched"]:
                work_seed["title"] = arxiv_result["candidates"][0].title
        if work_seed["title"]:
            before = self.requests_count
            try:
                data = self._oa_list({"search": work_seed["title"], "per-page": 5})
                for item in data["results"]:
                    if seed_match(candidate_from_openalex(item), work_seed):
                        attempts.append({"provider": "openalex", "route": "graph_title", "status": "matched", "requests": self.requests_count - before})
                        return item, attempts
                attempts.append({"provider": "openalex", "route": "graph_title", "status": "not_found", "requests": self.requests_count - before})
            except ResearchError as error:
                attempts.append({"provider": "openalex", "route": "graph_title", "status": "error", "code": error.code, "retryable": error.retryable,
                                 "details": error.details, "requests": self.requests_count - before})
        failures = [attempt for attempt in attempts if attempt.get("status") == "error"]
        if failures:
            failure = next((attempt for attempt in failures if attempt.get("retryable")), failures[0])
            raise ResearchError(failure["code"], "Could not verify the citation graph seed because a source request failed",
                                retryable=bool(failure.get("retryable")), details={**failure.get("details", {}), "attempts": attempts})
        raise ResearchError("SEED_NOT_FOUND", "Could not verify the seed in the OpenAlex citation graph", details={"attempts": attempts})

    @staticmethod
    def _cursor(payload):
        return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(value):
        if not isinstance(value, str) or len(value) > 20000:
            raise ResearchError("INVALID_ARGUMENT", "Invalid citation cursor")
        try:
            data = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        except (ValueError, TypeError, UnicodeError):
            raise ResearchError("INVALID_ARGUMENT", "Invalid citation cursor") from None
        if not isinstance(data, dict) or data.get("v") != 1 or data.get("provider") != "openalex":
            raise ResearchError("INVALID_ARGUMENT", "Unsupported citation cursor")
        return data

    def neighbors(self, seed: dict, direction="citations", cursor=None, page_size=25, date_from=None, date_to=None) -> dict:
        wanted = normalized_seed(seed)
        if direction not in ("citations", "references") or not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 100:
            raise ResearchError("INVALID_ARGUMENT", "Use citations/references and a page size in 1..100")
        try:
            for value in (date_from, date_to):
                if value is not None:
                    date.fromisoformat(value)
            if date_from and date_to and date_from > date_to:
                raise ValueError()
        except (ValueError, TypeError):
            raise ResearchError("INVALID_ARGUMENT", "Invalid citation date range") from None
        binding = hashlib.sha256(json.dumps({"seed": wanted, "direction": direction, "date_from": date_from, "date_to": date_to, "page_size": page_size}, sort_keys=True).encode()).hexdigest()
        state = self._decode_cursor(cursor) if cursor else None
        attempts = []
        if state:
            if state.get("binding") != binding or state.get("direction") != direction:
                raise ResearchError("INVALID_ARGUMENT", "Citation cursor belongs to a different seed or query")
            wid = _openalex(state.get("work_id"))
            if not wid:
                raise ResearchError("INVALID_ARGUMENT", "Citation cursor has no seed work ID")
            work = None
        else:
            work, attempts = self._graph_work(wanted)
            wid = _openalex(work["id"])
        before = self.requests_count
        filters = []
        if date_from:
            filters.append("from_publication_date:" + date_from)
        if date_to:
            filters.append("to_publication_date:" + date_to)
        warnings = ["Citation edges are indexed work-level links, not proof of relevance or revision-specific citations.",
                    "Non-publication records are excluded; total refers to the provider's indexed link count."]
        if direction == "citations":
            filters.insert(0, "cites:" + wid)
            authenticated = bool(os.getenv("OPENALEX_API_KEY"))
            mode = state.get("mode", "cursor" if state.get("remote") else None) if state else "cursor" if authenticated else "basic"
            if mode not in ("basic", "cursor"):
                raise ResearchError("INVALID_ARGUMENT", "Unsupported citation pagination mode")
            params = {"filter": ",".join(filters), "per-page": page_size, "sort": "publication_date:desc"}
            if mode == "cursor":
                if not authenticated:
                    raise ResearchError("SOURCE_AUTH_REQUIRED", "Saved OpenAlex cursor requires an API key; restart expansion for public basic pagination")
                remote = state.get("remote") if state else "*"
                if not isinstance(remote, str) or not remote:
                    raise ResearchError("INVALID_ARGUMENT", "Citation cursor has no provider cursor")
                params["cursor"] = remote
            else:
                basic_page = state.get("page", 1) if state else 1
                if not isinstance(basic_page, int) or isinstance(basic_page, bool) or basic_page < 1:
                    raise ResearchError("INVALID_ARGUMENT", "Citation cursor has no valid basic page")
                if basic_page * page_size > 10000:
                    raise ResearchError("SOURCE_RESULT_LIMIT", "Requested citation page exceeds OpenAlex's public basic-pagination window; use an API key or narrow the date range", details={"limit": 10000, "requested_page": basic_page, "page_size": page_size})
                params["page"] = basic_page
            try:
                data = self._oa_list(params)
            except ResearchError as error:
                if state and error.details.get("http_status") in (400, 404):
                    raise ResearchError("STALE_CURSOR", "OpenAlex rejected the saved citation cursor; restart this expansion") from None
                raise
            items = [candidate_from_openalex(item) for item in data["results"] if item.get("type") not in NON_PUBLICATIONS]
            total = data["meta"]["count"]
            if mode == "cursor":
                next_remote = data["meta"].get("next_cursor")
                if next_remote is not None and not isinstance(next_remote, str):
                    raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex returned an invalid citation cursor")
                next_cursor = self._cursor({"v": 1, "provider": "openalex", "direction": direction, "binding": binding, "work_id": wid, "mode": mode, "remote": next_remote}) if next_remote else None
            else:
                if not data["results"] and total > (basic_page - 1) * page_size:
                    raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex returned an empty basic page before its reported total")
                has_more = basic_page * page_size < total
                next_cursor = self._cursor({"v": 1, "provider": "openalex", "direction": direction, "binding": binding, "work_id": wid, "mode": mode, "page": basic_page + 1}) if has_more else None
                warnings.append("Without an API key, forward citations use OpenAlex's supported basic pagination up to its public result window; a limit is not completion.")
        else:
            if work is None:
                work = self._json("openalex", "https://api.openalex.org/works/" + wid)
                if work is None:
                    raise ResearchError("STALE_CURSOR", "Seed work is no longer available")
            checked = candidate_from_openalex(work)
            if checked.source_id != wid:
                raise ResearchError("SOURCE_INVALID_RESPONSE", "Reference seed identity changed")
            if not isinstance(work.get("referenced_works"), list):
                raise ResearchError("SOURCE_INVALID_RESPONSE", "Seed response has no indexed reference list")
            try:
                references = list(dict.fromkeys(_openalex(value) for value in work["referenced_works"]))
                if any(value is None for value in references):
                    raise ValueError()
            except (ResearchError, ValueError):
                raise ResearchError("SOURCE_INVALID_RESPONSE", "OpenAlex contains an invalid reference identifier") from None
            references_hash = hashlib.sha256(json.dumps(references).encode()).hexdigest()
            if state and state.get("references_hash") != references_hash:
                raise ResearchError("STALE_CURSOR", "Seed references changed; restart this expansion")
            offset = state.get("offset", 0) if state else 0
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > len(references):
                raise ResearchError("INVALID_ARGUMENT", "Invalid reference cursor offset")
            batch = references[offset:offset + page_size]
            items = []
            if batch:
                filters.insert(0, "openalex:" + "|".join(batch))
                data = self._oa_list({"filter": ",".join(filters), "per-page": len(batch)})
                for item in data["results"]:
                    if _openalex(item.get("id")) not in batch:
                        raise ResearchError("SOURCE_INVALID_RESPONSE", "Reference batch returned an unrelated work")
                    if item.get("type") not in NON_PUBLICATIONS:
                        items.append(candidate_from_openalex(item))
            offset += len(batch)
            next_cursor = self._cursor({"v": 1, "provider": "openalex", "direction": direction, "binding": binding, "work_id": wid, "offset": offset, "references_hash": references_hash}) if offset < len(references) else None
            total = None if date_from or date_to else len(references)
            warnings.append("Indexed references may omit works not matched by OpenAlex; missing or date-filtered batch records are not fabricated.")
        attempts.append({"provider": "openalex", "route": direction, "status": "ok", "returned_count": len(items), "requests": self.requests_count - before})
        return {"page": SourcePage(items=items, next_cursor=next_cursor, total=total, warnings=warnings), "attempts": attempts, "provider": "openalex"}
