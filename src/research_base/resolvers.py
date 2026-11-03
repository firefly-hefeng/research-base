"""Public full-text routes with explicit identity and bounded metadata requests."""
from __future__ import annotations

from html.parser import HTMLParser
import ipaddress
import json
import os
import re
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx

from .materials import normalized_doi
from .models import ResearchError

PMC_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
METADATA_LIMIT = 2 * 1024 * 1024
PREPRINT_DOI = re.compile(r"10\.(?:1101|64898)/(?:\d{4}\.\d{2}\.\d{2}\.\d+|\d{6,})$", re.I)


def _public_https(value):
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if parts.hostname.lower() in {"localhost", "localhost.localdomain"}:
        return None
    try:
        if not ipaddress.ip_address(parts.hostname).is_global:
            return None
    except ValueError:
        pass
    return value


def _request(client, url, *, params=None):
    """Bound HTML/JSON metadata; do not echo response bodies in diagnostics."""
    current, visited, previous = url, set(), None
    for hop in range(6):
        # Validate before every request: checking only the final URL is too late
        # after a public endpoint redirects into a private/loopback address.
        if not _public_https(current) or current in visited:
            raise ValueError("Invalid public metadata redirect")
        visited.add(current)
        request = client.build_request("GET", current, params=params if hop == 0 else None, timeout=15)
        if previous is not None and urlsplit(previous).netloc != urlsplit(current).netloc:
            request.headers.pop("authorization", None)
            request.headers.pop("proxy-authorization", None)
        response = client.send(request, stream=True, follow_redirects=False,
                               auth=httpx.USE_CLIENT_DEFAULT if hop == 0 else None)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location or hop == 5:
                    raise ValueError("Public metadata redirect is missing or exceeds the limit")
                previous, current = current, urljoin(current, location)
                continue
            if "application/pdf" in response.headers.get("content-type", "").lower():
                return response.status_code, str(response.url), dict(response.headers), b""
            body = bytearray()
            for chunk in response.iter_bytes(65536):
                body.extend(chunk)
                if len(body) > METADATA_LIMIT:
                    raise ValueError("Metadata response exceeds limit")
            return response.status_code, str(response.url), dict(response.headers), bytes(body)
        finally:
            response.close()
    raise ValueError("Public metadata redirect limit exceeded")


def _json_request(client, url, *, params=None):
    status, _, _, body = _request(client, url, params=params)
    if status != 200:
        return status, None
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("error") or data.get("errors"):
        raise ValueError("Invalid metadata envelope")
    return status, data


def _identity(paper):
    identifiers = dict(paper.get("identifiers") or {})
    for version in paper.get("versions", []):
        for key, value in (version.get("metadata", {}).get("identifiers") or {}).items():
            identifiers.setdefault(key, value)
    return identifiers


class _EmbeddedMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values, self.pdfs = {}, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            if name in {"citation_doi", "dc.identifier", "dc.identifier.doi", "citation_title", "citation_pdf_url", "citation_arxiv_id"}:
                self.values.setdefault(name, []).append(attrs.get("content", "").strip())
        elif tag.lower() == "link" and attrs.get("type", "").lower() == "application/pdf":
            if "alternate" in attrs.get("rel", "").lower().split():
                self.pdfs.append(attrs.get("href", ""))


def _landing_locations(client, paper, landing_url, expected_doi, *, related_published_doi=None, explicit_kind=None):
    status, final_url, headers, body = _request(client, landing_url)
    if status != 200:
        return [], {"resolver": "landing_metadata", "http_status": status}
    if "application/pdf" in headers.get("content-type", "").lower():
        # Redirects establish a route, not an edition classification.
        return [{"url": final_url, "version_kind": explicit_kind or "unknown", "resolver": "landing_redirect", "landing_url": landing_url}], None
    parser = _EmbeddedMetadata()
    parser.feed(body.decode("utf-8", errors="replace"))
    observed = {normalized_doi(value) for key in ("citation_doi", "dc.identifier.doi", "dc.identifier") for value in parser.values.get(key, []) if "10." in value}
    expected_doi = normalized_doi(expected_doi)
    if not expected_doi or expected_doi not in observed:
        return [], {"resolver": "landing_metadata", "outcome": "identity_not_confirmed"}
    title = next(iter(parser.values.get("citation_title", [])), None)
    pdfs = parser.values.get("citation_pdf_url", []) + parser.pdfs
    result = []
    for value in pdfs:
        url = _public_https(urljoin(final_url, value))
        if not url:
            continue
        kind = explicit_kind or ("preprint" if PREPRINT_DOI.fullmatch(expected_doi) else "unknown")
        item = {"url": url, "version_kind": kind, "doi": expected_doi, "identifiers": {"doi": expected_doi},
                "resolver": "landing_metadata", "landing_url": final_url}
        if title:
            item["title"] = title
        if related_published_doi:
            item["related_published_doi"] = related_published_doi
        result.append(item)
    return result, None if result else {"resolver": "landing_metadata", "outcome": "no_pdf_metadata"}


def _pmc_locations(client, paper, identifiers):
    locations, diagnostics = [], []
    pmcid = str(identifiers.get("pmcid") or "").upper()
    if pmcid.isdigit():
        pmcid = "PMC" + pmcid
    expected_doi = normalized_doi(paper.get("doi"))
    if not pmcid and identifiers.get("pmid"):
        params = {"ids": str(identifiers["pmid"]), "idtype": "pmid", "format": "json", "tool": "research-base"}
        if os.getenv("NCBI_EMAIL"):
            params["email"] = os.environ["NCBI_EMAIL"]
        status, record = _json_request(client, "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/", params=params)
        if status != 200:
            return [], [{"resolver": "pmc_id_converter", "http_status": status}]
        if record.get("status") != "ok" or not isinstance(record.get("records"), list):
            raise ValueError("Invalid PMC conversion response")
        for match in record["records"]:
            if str(match.get("pmid")) != str(identifiers["pmid"]):
                continue
            if expected_doi and normalized_doi(match.get("doi")) != expected_doi:
                continue
            if match.get("live") is False or match.get("live") == "false":
                diagnostics.append({"resolver": "pmc_id_converter", "outcome": "embargoed"})
                continue
            pmcid = str(match.get("pmcid") or "").upper()
            break
    if not re.fullmatch(r"PMC\d+(?:\.\d+)?", pmcid):
        return [], diagnostics
    base_id = pmcid.split(".")[0]
    status, _, _, body = _request(client, PMC_BUCKET + "/", params={"list-type": "2", "prefix": base_id + ".", "delimiter": "/", "max-keys": "50"})
    if status != 200:
        return [], diagnostics + [{"resolver": "pmc_cloud", "http_status": status}]
    root = ET.fromstring(body)
    if root.tag.split("}")[-1] != "ListBucketResult":
        raise ValueError("Invalid PMC cloud listing")
    prefixes = [node.text for node in root.iter() if node.tag.split("}")[-1] == "Prefix" and node.text and re.fullmatch(re.escape(base_id) + r"\.\d+/", node.text)]
    prefixes = sorted(set(prefixes), key=lambda value: int(value.rstrip("/").rsplit(".", 1)[1]), reverse=True)
    if "." in pmcid:
        prefixes = [prefix for prefix in prefixes if prefix == pmcid + "/"]
    if len(prefixes) > 3:
        diagnostics.append({"resolver": "pmc_cloud", "outcome": "version_window_limited", "version_count": len(prefixes)})
    for prefix in prefixes[:3]:
        version_name = prefix.rstrip("/")
        status, record = _json_request(client, PMC_BUCKET + "/metadata/" + version_name + ".json")
        if status != 200:
            diagnostics.append({"resolver": "pmc_cloud", "http_status": status})
            continue
        if str(record.get("pmcid", "")).split(".")[0].upper() != base_id:
            diagnostics.append({"resolver": "pmc_cloud", "outcome": "identity_mismatch"})
            continue
        actual_doi = normalized_doi(record.get("doi"))
        if expected_doi and actual_doi != expected_doi:
            diagnostics.append({"resolver": "pmc_cloud", "outcome": "doi_mismatch"})
            continue
        manuscript = str(record.get("is_manuscript", "")).lower()
        kind = "accepted" if manuscript in ("yes", "true", "1") else "published" if manuscript in ("no", "false", "0") else "unknown"
        if actual_doi and PREPRINT_DOI.fullmatch(actual_doi):
            kind = "preprint"
        # Author manuscripts often have official JATS XML but no PDF. Preserve
        # that original format; never infer a nonexistent web PDF route.
        for field, file_format in (("pdf_url", "pdf"), ("xml_url", "xml")):
            url = record.get(field)
            if isinstance(url, str) and url.startswith("s3://pmc-oa-opendata/"):
                url = PMC_BUCKET + "/" + url.split("/", 3)[3]
            if not _public_https(url):
                continue
            item = {"url": url, "version_kind": kind, "doi": actual_doi,
                    "identifiers": {"pmcid": version_name}, "title": record.get("title"), "resolver": "pmc_cloud",
                    "repository_version": version_name, "license": record.get("license_code")}
            if file_format == "xml":
                item["format"] = "xml"
            locations.append(item)
    if not locations:
        diagnostics.append({"resolver": "pmc_cloud", "outcome": "no_available_fulltext"})
    return locations, diagnostics


def _related_preprints(client, paper, identifiers):
    doi = normalized_doi(paper.get("doi"))
    if not doi or PREPRINT_DOI.fullmatch(doi) or not (identifiers.get("pmid") or identifiers.get("pmcid")):
        return [], []
    status, data = _json_request(client, "https://api.biorxiv.org/pubs/biorxiv/" + quote(doi, safe="/"))
    if status != 200:
        return [], [{"resolver": "biorxiv_relation", "http_status": status}]
    if not isinstance(data.get("collection"), list) or not isinstance(data.get("messages"), list):
        raise ValueError("Invalid bioRxiv relation response")
    locations, diagnostics = [], []
    for record in data["collection"][:3]:
        if normalized_doi(record.get("published_doi")) != doi:
            continue
        preprint_doi = normalized_doi(record.get("preprint_doi") or record.get("biorxiv_doi"))
        if not preprint_doi or not PREPRINT_DOI.fullmatch(preprint_doi):
            continue
        diagnostics.append({"resolver": "biorxiv_relation", "outcome": "explicit_version_relation",
                            "preprint_doi": preprint_doi, "published_doi": doi,
                            "title": record.get("preprint_title"), "landing_url": "https://doi.org/" + preprint_doi})
        found, diagnostic = _landing_locations(client, paper, "https://doi.org/" + preprint_doi, preprint_doi,
                                               related_published_doi=doi, explicit_kind="preprint")
        locations.extend(found)
        if diagnostic:
            diagnostics.append(diagnostic)
    if not locations:
        diagnostics.append({"resolver": "biorxiv_relation", "outcome": "no_available_preprint_pdf"})
    return locations, diagnostics


def resolve_locations(client: httpx.Client, paper: dict, *, phase="metadata") -> tuple[list[dict], list[dict]]:
    """Return actual routes; fallback is explicit so the caller can be lazy."""
    if phase not in {"metadata", "fallback", "all"}:
        raise ResearchError("INVALID_ARGUMENT", "Resolver phase must be metadata, fallback or all")
    locations, diagnostics, seen = [], [], set()

    def add(url, kind="unknown", **identity):
        url = _public_https(url)
        if not url:
            return
        if url in seen:
            existing = next(item for item in locations if item["url"] == url)
            if existing["version_kind"] == "unknown" and kind != "unknown":
                existing["version_kind"] = kind
            # A second provider may verify identity for an already classified URL.
            for key, value in identity.items():
                if key == "identifiers":
                    existing.setdefault(key, {}).update({name: item for name, item in value.items() if name not in existing.get(key, {})})
                else:
                    existing.setdefault(key, value)
            return
        seen.add(url)
        locations.append({"url": url, "version_kind": kind, **identity})

    identifiers = _identity(paper)
    doi = normalized_doi(paper.get("doi"))
    if phase in {"metadata", "all"}:
        arxiv = identifiers.get("arxiv") or identifiers.get("arxiv_work")
        if arxiv and re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?", arxiv):
            add("https://arxiv.org/pdf/" + arxiv, "preprint", identifiers={"arxiv": arxiv})
        if doi:
            try:
                params = {"api_key": os.environ["OPENALEX_API_KEY"]} if os.getenv("OPENALEX_API_KEY") else {}
                status, work = _json_request(client, "https://api.openalex.org/works/https://doi.org/" + quote(doi, safe=""), params=params)
                if status == 200:
                    if not any(key in work for key in ("content_urls", "best_oa_location", "locations")):
                        raise ValueError("Invalid work response")
                    actual_doi = normalized_doi(work.get("doi") or (work.get("ids") or {}).get("doi"))
                    if actual_doi and actual_doi != doi:
                        raise ValueError("OpenAlex work identity mismatch")
                    identity = {"doi": actual_doi, "identifiers": {"doi": actual_doi}} if actual_doi == doi else {}
                    add((work.get("content_urls") or {}).get("pdf"), **identity)
                    for location in [work.get("best_oa_location") or {}] + (work.get("locations") or []):
                        if location.get("is_oa") or location == work.get("best_oa_location"):
                            kind = {"publishedVersion": "published", "acceptedVersion": "accepted", "submittedVersion": "preprint"}.get(location.get("version"), "unknown")
                            add(location.get("pdf_url"), kind, **identity)
                elif status != 404:
                    diagnostics.append({"resolver": "openalex", "http_status": status})
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                diagnostics.append({"resolver": "openalex", "error": "unavailable_or_invalid_response"})
            email = os.getenv("UNPAYWALL_EMAIL")
            if email:
                try:
                    status, data = _json_request(client, "https://api.unpaywall.org/v2/" + quote(doi, safe=""), params={"email": email})
                    if status == 200:
                        if not isinstance(data.get("oa_locations"), list):
                            raise ValueError("Invalid OA response")
                        actual_doi = normalized_doi(data.get("doi"))
                        if actual_doi and actual_doi != doi:
                            raise ValueError("Unpaywall work identity mismatch")
                        identity = {"doi": actual_doi, "identifiers": {"doi": actual_doi}} if actual_doi == doi else {}
                        for location in data["oa_locations"]:
                            kind = {"publishedVersion": "published", "acceptedVersion": "accepted", "submittedVersion": "preprint"}.get(location.get("version"), "unknown")
                            add(location.get("url_for_pdf"), kind, **identity)
                    elif status != 404:
                        diagnostics.append({"resolver": "unpaywall", "http_status": status})
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    diagnostics.append({"resolver": "unpaywall", "error": "unavailable_or_invalid_response"})
    if phase in {"fallback", "all"}:
        for name, resolver in (("pmc_cloud", _pmc_locations), ("biorxiv_relation", _related_preprints)):
            try:
                found, notes = resolver(client, paper, identifiers)
                for location in found:
                    add(location["url"], location["version_kind"], **{k: v for k, v in location.items() if k not in {"url", "version_kind"}})
                diagnostics.extend(notes)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError, ET.ParseError):
                diagnostics.append({"resolver": name, "error": "unavailable_or_invalid_response"})
        landings = [paper.get("landing_url")] + [v.get("metadata", {}).get("landing_url") for v in paper.get("versions", [])]
        if doi:
            landings.append("https://doi.org/" + doi)
        for landing in list(dict.fromkeys(value for value in landings if _public_https(value)))[:3]:
            try:
                found, note = _landing_locations(client, paper, landing, doi)
                for location in found:
                    add(location["url"], location["version_kind"], **{k: v for k, v in location.items() if k not in {"url", "version_kind"}})
                if note:
                    diagnostics.append(note)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                diagnostics.append({"resolver": "landing_metadata", "error": "unavailable_or_invalid_response"})
    return locations, diagnostics
