"""Search bounded, verifiable ACL Anthology XML snapshots.

The native query names official collection files and literal title/abstract
terms. It never accepts an arbitrary URL or equates a failed file with no hits.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from xml.etree import ElementTree as ET

from research_base.models import Candidate, ResearchError, SourcePage

from .registry import Transport, doi


_COLLECTION = re.compile(r"(?:19|20)\d{2}\.[a-z][a-z0-9-]{0,49}\Z")
_LOCAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}\Z")
_MONTHS = {name: number for number, name in enumerate((
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
_MONTHS.update({name[:3]: number for name, number in list(_MONTHS.items())})


def _text(node: ET.Element | None) -> str:
    # Inline fixed-case/tex-math elements must not split titles into fake words.
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip() if node is not None else ""


def _normalized(value: str) -> str:
    return re.sub(r"[\u2010-\u2015]", "-", value.casefold())


@dataclass
class _Snapshot:
    collection: str
    sha256: str
    content_bytes: int
    root: ET.Element


class AclAnthology:
    info = {"name": "acl_anthology", "label": "ACL Anthology", "scope": "Declared ACL Anthology proceedings collections; title and abstract matching",
            "resource_kind": "publication", "pagination": "collection snapshots with SHA256-bound cursors",
            "optional_key_env": None, "documentation": "https://aclanthology.org/info/development/",
            "default_enabled": False, "requires_native_query": True,
            "query_format": '{"collections":["2026.emnlp"],"any":["RAG","retrieval augmented"],"all":[]}'}

    def __init__(self, transport: Transport, *, max_file_bytes: int = 25 * 1024 * 1024,
                 max_cache_bytes: int = 128 * 1024 * 1024):
        self.t = transport
        self.max_file_bytes, self.max_cache_bytes = max_file_bytes, max_cache_bytes
        self._snapshots: OrderedDict[tuple[str, str], _Snapshot] = OrderedDict()
        self._cache_bytes = 0

    @staticmethod
    def _query(query: str) -> dict:
        try:
            if not isinstance(query, str) or len(query) > 64000:
                raise ValueError()
            value = json.loads(query)
            if not isinstance(value, dict) or set(value) - {"collections", "any", "all"}:
                raise ValueError()
            collections = value.get("collections")
            if (not isinstance(collections, list) or not 1 <= len(collections) <= 40
                    or any(not isinstance(item, str) or not _COLLECTION.fullmatch(item) for item in collections)):
                raise ValueError()
            result = {"collections": list(dict.fromkeys(collections))}
            for key in ("any", "all"):
                terms = value.get(key, [])
                if (not isinstance(terms, list) or len(terms) > 100
                        or any(not isinstance(term, str) or not term.strip() or len(term) > 500 for term in terms)):
                    raise ValueError()
                result[key] = list(dict.fromkeys(_normalized(term.strip()) for term in terms))
            if not result["any"] and not result["all"]:
                raise ValueError()
            return result
        except (ValueError, TypeError, RecursionError):
            raise ResearchError("SOURCE_INVALID_QUERY", "ACL query must be JSON with official year.venue collections and nonempty any/all literal terms") from None

    def _fetch(self, collection: str, expected_sha: str | None) -> _Snapshot:
        if expected_sha and (collection, expected_sha) in self._snapshots:
            key = (collection, expected_sha)
            self._snapshots.move_to_end(key)
            return self._snapshots[key]
        url = f"https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{collection}.xml"
        response = self.t.get("acl_anthology", url)
        raw = response.content
        declared_length = response.headers.get("Content-Length")
        if len(raw) > self.max_file_bytes or (declared_length and declared_length.isdigit() and int(declared_length) > self.max_file_bytes):
            raise ResearchError("SOURCE_RESULT_LIMIT", "ACL collection exceeds the configured XML file-size limit")
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha and digest != expected_sha:
            raise ResearchError("SOURCE_SNAPSHOT_CHANGED", "ACL collection changed since this cursor; start a new discovery job instead of treating it as completed",
                                retryable=False, details={"collection": collection, "expected_sha256": expected_sha, "actual_sha256": digest})
        if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "ACL XML contains unsupported document declarations")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise ResearchError("SOURCE_INVALID_RESPONSE", "ACL collection returned malformed XML") from None
        if root.tag != "collection" or root.get("id") != collection or not root.findall("volume"):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "ACL XML has the wrong collection identity or no volume metadata")
        snapshot = _Snapshot(collection, digest, len(raw), root)
        key = (collection, digest)
        if key not in self._snapshots and len(raw) <= self.max_cache_bytes:
            while self._snapshots and self._cache_bytes + len(raw) > self.max_cache_bytes:
                _, old = self._snapshots.popitem(last=False)
                self._cache_bytes -= old.content_bytes
            self._snapshots[key] = snapshot
            self._cache_bytes += len(raw)
        return snapshot

    @staticmethod
    def _period(year_text: str, month_text: str) -> tuple[int, date, date, str]:
        if not re.fullmatch(r"(?:19|20)\d{2}", year_text):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "ACL volume has no valid publication year")
        year_value = int(year_text)
        # A textual "November 3-5" still means November; day numbers must
        # never be mistaken for March and May in the conservative interval.
        months = [_MONTHS[token] for token in re.findall(r"[A-Za-z]+", month_text.casefold()) if token in _MONTHS]
        if not months and re.fullmatch(r"\s*\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s*", month_text):
            months = [int(token) for token in re.findall(r"\d+", month_text) if 1 <= int(token) <= 12]
        # Metadata supplies a month or range, not an invented exact publication day.
        first, last = (min(months), max(months)) if months else (1, 12)
        import calendar
        lo = date(year_value, first, 1)
        hi = date(year_value, last, calendar.monthrange(year_value, last)[1])
        return year_value, lo, hi, "month" if first == last and months else "month_range" if months else "year"

    @staticmethod
    def _term(term: str, haystack: str) -> bool:
        pattern = r"(?<!\w)" + re.escape(term).replace(r"\ ", r"\s+") + r"(?!\w)"
        return re.search(pattern, haystack, re.I) is not None

    def _matches(self, snapshot: _Snapshot, query: dict, lo: date | None, hi: date | None) -> list[Candidate]:
        result = []
        for volume in snapshot.root.findall("volume"):
            volume_id = volume.get("id", "")
            if not _LOCAL_ID.fullmatch(volume_id):
                raise ResearchError("SOURCE_INVALID_RESPONSE", "ACL volume has no valid identifier")
            meta = volume.find("meta")
            publication_year, start, end, precision = self._period(_text(meta.find("year")) if meta is not None else "",
                _text(meta.find("month")) if meta is not None else "")
            if (lo and end < lo) or (hi and start > hi):
                continue
            for paper in volume.findall("paper"):
                paper_id, title = paper.get("id", ""), _text(paper.find("title"))
                if not _LOCAL_ID.fullmatch(paper_id) or not title:
                    raise ResearchError("SOURCE_INVALID_RESPONSE", "ACL paper has no valid identity or title")
                abstract = _text(paper.find("abstract"))
                haystack = _normalized(title + "\n" + abstract)
                if query["any"] and not any(self._term(term, haystack) for term in query["any"]):
                    continue
                if not all(self._term(term, haystack) for term in query["all"]):
                    continue
                source_id = f"{snapshot.collection}-{volume_id}.{paper_id}"
                authors = []
                for author in paper.findall("author"):
                    name = " ".join(filter(None, (_text(author.find("first")), _text(author.find("last")))))
                    authors.append(name or _text(author.find("name")) or _text(author))
                pdf = paper.find("pdf")
                pdf_url = f"https://aclanthology.org/{source_id}.pdf" if pdf is not None else None
                result.append(Candidate(source="acl_anthology", source_id=source_id, title=title,
                    authors=authors, year=publication_year, doi=doi(_text(paper.find("doi"))), abstract=abstract,
                    identifiers={"acl_anthology": source_id}, version_kind="published",
                    landing_url=f"https://aclanthology.org/{source_id}/", fulltext_urls=[pdf_url] if pdf_url else [],
                    extra={"collection": snapshot.collection, "volume": volume_id,
                        "publication_month": _text(meta.find("month")), "publication_year": publication_year,
                        "publication_date_precision": precision, "publication_interval_start": start.isoformat(),
                        "publication_interval_end": end.isoformat(), "snapshot_sha256": snapshot.sha256,
                        "metadata_url": f"https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{snapshot.collection}.xml"}))
        return result

    @staticmethod
    def _encode(value: dict) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "acl:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def search(self, query: str, *, cursor: str | None, page_size: int,
               date_from: str | None, date_to: str | None) -> SourcePage:
        parsed = self._query(query)
        try:
            if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 1000:
                raise ValueError()
            lo = date.fromisoformat(date_from) if date_from else None
            hi = date.fromisoformat(date_to) if date_to else None
            if lo and hi and lo > hi:
                raise ValueError()
        except (ValueError, TypeError):
            raise ResearchError("INVALID_ARGUMENT", "ACL page size and ordered ISO date range are required") from None
        fingerprint = hashlib.sha256(json.dumps([parsed, page_size, date_from, date_to], sort_keys=True).encode()).hexdigest()
        state = {"v": 1, "f": fingerprint, "i": 0, "o": 0, "s": None, "n": 0}
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not cursor.startswith("acl:") or len(cursor) > 4096:
                    raise ValueError()
                state = json.loads(base64.urlsafe_b64decode(cursor[4:] + "=" * (-len(cursor[4:]) % 4)))
                if (set(state) != {"v", "f", "i", "o", "s", "n"} or state["v"] != 1 or state["f"] != fingerprint
                        or any(not isinstance(state[key], int) or isinstance(state[key], bool) or state[key] < 0 for key in ("i", "o", "n"))
                        or state["i"] >= len(parsed["collections"])
                        or (state["s"] is not None and (not isinstance(state["s"], str) or not re.fullmatch(r"[0-9a-f]{64}", state["s"])))
                        or (state["o"] > 0 and state["s"] is None)):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, RecursionError):
                raise ResearchError("INVALID_ARGUMENT", "ACL cursor is invalid or belongs to another query/date/page-size scope") from None
        collection = parsed["collections"][state["i"]]
        snapshot = self._fetch(collection, state["s"])
        matches = self._matches(snapshot, parsed, lo, hi)
        if state["o"] > len(matches):
            raise ResearchError("INVALID_ARGUMENT", "ACL cursor offset is outside its verified snapshot")
        items = matches[state["o"]:state["o"] + page_size]
        seen = state["n"] + len(items)
        next_cursor = None
        if state["o"] + len(items) < len(matches):
            next_cursor = self._encode({**state, "o": state["o"] + len(items), "s": snapshot.sha256, "n": seen})
        elif state["i"] + 1 < len(parsed["collections"]):
            next_cursor = self._encode({**state, "i": state["i"] + 1, "o": 0, "s": None, "n": seen})
        warnings = [f"ACL collection {collection}; XML SHA256 {snapshot.sha256}; publication dates are month/year intervals, not exact days; date filtering uses interval overlap.",
                    "Coverage is limited to the declared collection files and literal title/abstract query; papers without abstracts and later upstream changes may affect recall."]
        return SourcePage(items=items, next_cursor=next_cursor, total=seen if next_cursor is None else None, warnings=warnings)
