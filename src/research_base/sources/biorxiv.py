"""Official bioRxiv dated/category metadata feeds, with explicit local matching.

The provider has no free-text search endpoint. A page is one dated/category
metadata page (30 version records), even when local term filtering finds zero
papers. Latest months are visited first without changing the declared window.
"""
from __future__ import annotations

import base64
from calendar import monthrange
from datetime import date
import hashlib
import json
import re
from urllib.parse import urlsplit

from research_base.models import Candidate, MaterialLink, ResearchError, SourcePage
from .registry import Transport, clean, doi, require_count, require_list


_CATEGORY = re.compile(r"[a-z]+(?:_[a-z]+){0,5}\Z")
# These names have been checked against the official API. Extending the list
# requires checking a real category response; an unknown category can otherwise
# look exactly like an empty date interval on this API.
_CATEGORIES = frozenset(("bioinformatics", "genomics", "cell_biology"))
_DOI = re.compile(r"10\.\d{4,9}/[^\s?#]+\Z", re.I)


class BioRxiv:
    info = {"name": "biorxiv", "label": "bioRxiv official metadata", "resource_kind": "publication",
        "scope": "Explicit bioRxiv categories and posted-date window; local title/abstract matching",
        "pagination": "latest month first; category/official 30-record offset pages",
        "documentation": "https://api.biorxiv.org/", "optional_key_env": None,
        "default_enabled": False, "requires_native_query": True,
        "query_format": '{"categories":["bioinformatics","genomics","cell_biology"],"any":["perturbation","perturbations","Perturb-seq"],"all":[]}'}

    def __init__(self, transport: Transport):
        self.t = transport

    @staticmethod
    def _query(query):
        try:
            if not isinstance(query, str) or len(query) > 16000:
                raise ValueError()
            value = json.loads(query)
            if not isinstance(value, dict) or set(value) - {"categories", "any", "all"}:
                raise ValueError()
            categories = value.get("categories")
            if (not isinstance(categories, list) or not 1 <= len(categories) <= 12
                    or any(not isinstance(item, str) or not _CATEGORY.fullmatch(item) or item not in _CATEGORIES for item in categories)):
                raise ValueError()
            parsed = {"categories": list(dict.fromkeys(categories))}
            for key in ("any", "all"):
                terms = value.get(key, [])
                if (not isinstance(terms, list) or len(terms) > 50 or
                        any(not isinstance(term, str) or not term.strip() or len(term) > 200 for term in terms)):
                    raise ValueError()
                parsed[key] = list(dict.fromkeys(term.casefold().strip() for term in terms))
            if not parsed["any"] and not parsed["all"]:
                raise ValueError()
            return parsed
        except (ValueError, TypeError, RecursionError):
            raise ResearchError("SOURCE_INVALID_QUERY", "bioRxiv requires JSON categories and any/all literal title/abstract terms") from None

    @staticmethod
    def _partitions(lo, hi, categories):
        intervals = []
        current = date(hi.year, hi.month, 1)
        while current <= hi and current.year >= lo.year:
            end = date(current.year, current.month, monthrange(current.year, current.month)[1])
            if end < lo:
                break
            intervals.extend((max(lo, current).isoformat(), min(hi, end).isoformat(), category) for category in categories)
            current = date(current.year - (current.month == 1), 12 if current.month == 1 else current.month - 1, 1)
        return intervals

    @staticmethod
    def _encode(state):
        return "biorxiv:" + base64.urlsafe_b64encode(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")

    def search(self, query, *, cursor, page_size, date_from, date_to):
        parsed = self._query(query)
        try:
            if (not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 1000
                    or not date_from or not date_to):
                raise ValueError()
            lo, hi = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if lo > hi or hi.year - lo.year > 10:
                raise ValueError()
        except (ValueError, TypeError):
            raise ResearchError("INVALID_ARGUMENT", "bioRxiv needs an ordered explicit ISO date window of at most ten years") from None
        partitions = self._partitions(lo, hi, parsed["categories"])
        fingerprint = hashlib.sha256(json.dumps([parsed, date_from, date_to, page_size], sort_keys=True).encode()).hexdigest()
        state = {"v": 1, "f": fingerprint, "p": 0, "o": 0, "t": None, "n": 0}
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not cursor.startswith("biorxiv:") or len(cursor) > 4096:
                    raise ValueError()
                state = json.loads(base64.urlsafe_b64decode(cursor[8:] + "=" * (-len(cursor[8:]) % 4)))
                if (set(state) != {"v", "f", "p", "o", "t", "n"} or state["v"] != 1 or state["f"] != fingerprint
                        or any(not isinstance(state[k], int) or isinstance(state[k], bool) or state[k] < 0 for k in ("p", "o", "n"))
                        or state["p"] >= len(partitions)
                        or (state["t"] is not None and (not isinstance(state["t"], int) or isinstance(state["t"], bool) or state["t"] < 0))
                        or (state["o"] and state["t"] is None)):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, RecursionError):
                raise ResearchError("INVALID_ARGUMENT", "bioRxiv cursor is invalid or belongs to another query/date scope") from None
        start, end, category = partitions[state["p"]]
        url = f"https://api.biorxiv.org/details/biorxiv/{start}/{end}/{state['o']}/json"
        envelope = self.t.json("biorxiv", url, params={"category": category})
        messages = require_list(envelope.get("messages"), "biorxiv")
        if len(messages) != 1 or not isinstance(messages[0], dict):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "bioRxiv did not confirm a successful metadata page")
        message = messages[0]
        raw = require_list(envelope.get("collection"), "biorxiv")
        if message.get("status") == "no posts found" and not raw and state["o"] == 0:
            # Actual official empty-day shape has no cursor/count/category.
            # Only a new, explicitly valid category partition may use it.
            total = count = offset = 0
        else:
            returned_category = message.get("category")
            if (message.get("status") != "ok" or not isinstance(returned_category, str)
                    or returned_category.casefold().replace("_", " ") != category.replace("_", " ")
                    or (message.get("interval") is not None and message["interval"] != f"{start}:{end}")):
                raise ResearchError("SOURCE_INVALID_RESPONSE", "bioRxiv did not confirm the requested category/date scope")
            total = require_count(message.get("total"), "biorxiv")
            count = require_count(message.get("count"), "biorxiv")
            offset = require_count(message.get("cursor"), "biorxiv")
        if (offset != state["o"] or count != len(raw) or len(raw) > 30 or offset + count > total
                or (not raw and offset < total)):
            raise ResearchError("SOURCE_INVALID_RESPONSE", "bioRxiv metadata page contradicts its cursor/count/total")
        if state["t"] is not None and total != state["t"]:
            raise ResearchError("SOURCE_SNAPSHOT_CHANGED", "bioRxiv interval count changed during pagination; start a new query rather than claiming completion")
        items = []
        for item in raw:
            if not isinstance(item, dict):
                raise ResearchError("SOURCE_INVALID_RESPONSE", "bioRxiv returned an invalid manuscript record")
            value, title = doi(item.get("doi")), clean(item.get("title"))
            try:
                posted = date.fromisoformat(item.get("date", ""))
                version = int(item.get("version", ""))
                if not value or not _DOI.fullmatch(value) or not title or version < 1 or not start <= posted.isoformat() <= end:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ResearchError("SOURCE_INVALID_RESPONSE", "bioRxiv manuscript lacks a valid identity, revision or interval posted date") from None
            text = (title + " " + clean(item.get("abstract"))).casefold()
            matches = lambda term: bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text))
            if (parsed["any"] and not any(map(matches, parsed["any"]))) or not all(map(matches, parsed["all"])):
                continue
            landing = f"https://www.biorxiv.org/content/{value}v{version}"
            published = doi(item.get("published"))
            if not published or not _DOI.fullmatch(published):
                published = None
            links = []
            jats = item.get("jatsxml")
            if isinstance(jats, str) and urlsplit(jats).scheme == "https" and urlsplit(jats).hostname in ("biorxiv.org", "www.biorxiv.org"):
                links.append(MaterialLink(role="main_text", format="xml", url=jats))
            items.append(Candidate(source="biorxiv", source_id=value + "v" + str(version), title=title,
                authors=[clean(author) for author in str(item.get("authors") or "").split(";") if clean(author)],
                year=posted.year, doi=value, abstract=clean(item.get("abstract")),
                identifiers={"biorxiv_revision": value + "v" + str(version)}, version_kind="preprint",
                landing_url=landing, fulltext_urls=[landing + ".full.pdf"], links=links,
                extra={"archive_posted_date": posted.isoformat(), "archive_version": str(version),
                    "published_doi": published, "category": item.get("category"), "license": item.get("license"),
                    "metadata_url": url, "query_match_basis": "local_literal_title_and_abstract",
                    "provider_interval": [start, end], "provider_offset": offset}))
        seen = state["n"] + len(items)
        next_cursor = None
        if offset + count < total:
            next_cursor = self._encode({**state, "o": offset + count, "t": total, "n": seen})
        elif state["p"] + 1 < len(partitions):
            next_cursor = self._encode({**state, "p": state["p"] + 1, "o": 0, "t": None, "n": seen})
        return SourcePage(items=items, next_cursor=next_cursor, total=seen if next_cursor is None else None,
            warnings=[f"bioRxiv official metadata partition {start}..{end}, category {category}, offset {offset}; raw version records {count}/{total}; local matches {len(items)}.",
                "No server-side text search: literal matching uses returned title/abstract. Fixed provider pages contain up to 30 records regardless of requested page_size; empty matched pages still require continuation.",
                "Latest months are scanned first; completion requires every declared month/category. Posted dates include revisions, not journal publication dates; mutable provider indexes can still change between pages."])
