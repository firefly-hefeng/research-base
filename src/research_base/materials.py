"""Original material ingestion and direct, portable file access."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from pypdf import PdfReader

from .models import ResearchError
from .identity import observed_identifiers
from .storage import atomic_write, dumps, loads, now, uid


def _posted_date(text):
    match = re.search(r"this\s+version\s+posted\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text, re.I)
    if not match:
        return None
    months = {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
    try:
        return date(int(match[3]), months[match[1][:3].lower()], int(match[2])).isoformat()
    except (KeyError, ValueError):
        return None


def safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ResearchError("INVALID_ARGUMENT", "Only HTTP(S) material URLs without credentials are accepted")
    # Query-bearing signed links are kept in memory by downloaders, not in catalogs.
    from urllib.parse import parse_qsl, urlencode
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not any(word in k.lower() for word in ("key", "token", "signature", "credential", "authorization"))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def normalized_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).lower()
    # Parentheses and punctuation may be part of a registered DOI.
    return value or None


_ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?"


def _arxiv_identity(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"^(?:arxiv:\s*|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)", "", value.strip(), flags=re.I)
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    if not re.fullmatch(_ARXIV_ID, value, flags=re.I):
        return None
    parts = re.fullmatch(r"(.+?)(?:v(\d+))?", value.lower())
    return parts[1], parts[2]


def _arxiv_stamps(text):
    # Parse complete identifiers: v1 must never match the prefix of v10.
    return {_arxiv_identity(match) for match in re.findall(
        r"(?:arxiv\s*:\s*|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)(" + _ARXIV_ID + r")(?![A-Za-z0-9/])", text, flags=re.I)} - {None}


def _arxiv_matches(expected, observed):
    return expected[0] == observed[0] and (expected[1] is None or expected[1] == observed[1])


def _xml_name(node):
    return node.tag.split("}")[-1] if isinstance(node.tag, str) else ""


def _xml_text(node):
    return " ".join(" ".join(node.itertext()).split()) if node is not None else ""


def _jats_document(raw):
    """Parse article XML without external DTD/entity resolution or HTML fallback."""
    if b"<!ENTITY" in raw.replace(b"\x00", b"").upper():
        raise ResearchError("INVALID_FILE", "XML entity declarations are not accepted")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        raise ResearchError("INVALID_FILE", "Full-text endpoint did not return valid JATS XML") from None
    if _xml_name(root) != "article":
        raise ResearchError("INVALID_FILE", "Full-text XML must be a JATS article")
    if root.get("article-type", "").lower() in {"abstract", "supplementary-material"}:
        raise ResearchError("INVALID_FILE", "JATS article is not main text")
    body = next((e for e in root if _xml_name(e) == "body"), None)
    if len(_xml_text(body)) < 100:
        raise ResearchError("INVALID_FILE", "JATS XML has no substantive article body")
    return root


def _jats_content(raw):
    root = _jats_document(raw)
    front = next((e for e in root if _xml_name(e) == "front"), None)
    meta = next((e for e in front if _xml_name(e) == "article-meta"), None) if front is not None else None
    if meta is None:
        raise ResearchError("INVALID_FILE", "JATS XML lacks article identity metadata")
    ids = {}
    for node in meta:
        if _xml_name(node) == "article-id":
            ids.setdefault(node.get("pub-id-type", ""), set()).add(_xml_text(node))
    title_group = next((e for e in meta if _xml_name(e) == "title-group"), None)
    title = next((_xml_text(e) for e in title_group if _xml_name(e) == "article-title"), "") if title_group is not None else ""
    pages, sections = [], []
    def append(node, label):
        text = _xml_text(node)
        if text:
            page = len(pages) + 1
            pages.append((page, text))
            sections.append({"page": page, "label": label})
    append(front, "Front matter and abstract")
    for node in root:
        name = _xml_name(node)
        if name == "body":
            if not len(node):
                append(node, "Article body")
            for index, part in enumerate(node, 1):
                label = next((_xml_text(e) for e in part if _xml_name(e) == "title"), None)
                append(part, label or part.get("sec-type") or "Body part " + str(index))
        elif name != "front":
            append(node, {"back": "References and back matter", "floats-group": "Figures and tables"}.get(name, name))
    return pages, ids, title, {"kind": "logical_sections", "physical_pages": False, "sections": sections}


class MaterialsMixin:
    def _material(self, row, include_paths=True):
        item = dict(row)
        item["material_id"] = item.pop("id")
        item["details"] = loads(item["details"], {})
        relative = item.pop("relative_path")
        original = (self.root / relative).resolve() if relative else None
        if original and not original.is_relative_to(self.root):
            raise ResearchError("INVALID_FILE", "Catalog material points outside this library")
        item["exists"] = bool(original and original.is_file())
        if include_paths:
            item["local_path"] = str(original) if original else None
            derived = self.root / "derived" / item["material_id"] / item["details"].get("parser", "pypdf-v1")
            item["text_path"] = str(derived / "text.txt") if (derived / "text.txt").is_file() else None
            item["pages_path"] = str(derived / "pages.jsonl") if (derived / "pages.jsonl").is_file() else None
            item["access"] = {"mode": "local_filesystem", "shared_filesystem_required": True}
        return item

    def list_materials(self, paper_id=None, collection_id=None, include_paths=True):
        where, params = [], []
        if paper_id:
            self._require_paper(paper_id)
            ids = self._related_paper_ids(paper_id)
            where.append("m.paper_id IN (" + ",".join("?" for _ in ids) + ")")
            params.extend(ids)
        if collection_id:
            self._require_collection(collection_id)
            with self.store.connect() as db:
                members = [r[0] for r in db.execute("SELECT paper_id FROM memberships WHERE collection_id=? AND status='included'", (collection_id,))]
                ids = sorted({pid for member in members for pid in self._related_paper_ids(member, db)})
            where.append("m.paper_id IN (" + (",".join("?" for _ in ids) or "NULL") + ")")
            params.extend(ids)
        sql = "SELECT m.* FROM materials m" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY m.created_at,m.id"
        with self.store.connect() as db:
            return [self._material(row, include_paths) for row in db.execute(sql, params)]

    def _require_material(self, material_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if not row:
            raise ResearchError("NOT_FOUND", "Material does not exist")
        return row

    def import_material(self, paper_id, path=None, url=None, role="main_text", version_id=None, confirmed=False, source_url=None):
        paper = self._require_paper(paper_id)
        if bool(path) == bool(url):
            raise ResearchError("INVALID_ARGUMENT", "Provide exactly one local path or URL")
        if source_url:
            if not path:
                raise ResearchError("INVALID_ARGUMENT", "Download provenance accompanies a local file import only")
            source_url = safe_url(source_url)
        if role not in ("main_text", "supplement", "code", "data", "model", "other"):
            raise ResearchError("INVALID_ARGUMENT", "Unsupported material role")
        with self.store.connect() as db:
            if version_id and not db.execute("SELECT 1 FROM versions WHERE id=? AND paper_id=?", (version_id, paper_id)).fetchone():
                raise ResearchError("INVALID_ARGUMENT", "Version does not belong to this paper")
            if url:
                url = safe_url(url)
                found = db.execute("SELECT * FROM materials WHERE paper_id=? AND source_url=? AND role=? AND status='external'", (paper_id, url, role)).fetchone()
                if found:
                    return self._material(found)
                mid = uid("mat")
                db.execute("INSERT INTO materials(id,paper_id,version_id,role,format,status,source_url,created_at) VALUES(?,?,?,?,?,'external',?,?)",
                           (mid, paper_id, version_id, role, "url", url, now()))
        if url:
            return self._material(self._require_material(mid))
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ResearchError("NOT_FOUND", "Import file does not exist")
        if source.suffix.lower() in (".part", ".crdownload", ".download", ".tmp"):
            raise ResearchError("FILE_NOT_READY", "Download is not complete")
        if source.stat().st_size > self.max_file_bytes:
            raise ResearchError("INVALID_FILE", "Material exceeds the configured size limit")
        mid = uid("mat")
        staging = self.root / ".staging" / "imports" / mid
        staging.mkdir(parents=True, exist_ok=True)
        target = staging / ("original" + source.suffix.lower())
        shutil.copyfile(source, target)
        with target.open("rb") as handle:
            fmt = "pdf" if handle.read(5) == b"%PDF-" else source.suffix.lower().lstrip(".") or "bin"
        with self.store.connect() as db:
            db.execute("INSERT INTO materials(id,paper_id,version_id,role,format,status,relative_path,original_name,source_url,created_at) VALUES(?,?,?,?,?,'staged',?,?,?,?)",
                       (mid, paper_id, version_id, role, fmt, str(target.relative_to(self.root)), source.name, source_url, now()))
        return self._finalize_material(mid, confirmed=confirmed)

    def _finalize_material(self, material_id, confirmed=False):
        row = self._require_material(material_id)
        if row["status"] == "available":
            self._satisfy_tasks(row["paper_id"])
            return self._material(row)
        path = (self.root / row["relative_path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ResearchError("INVALID_FILE", "Staged file is unavailable")
        if not path.stat().st_size:
            raise ResearchError("INVALID_FILE", "Empty material")
        pages, issues, parse_status = [], [], "not_started"
        version_observations = {}
        if row["format"] == "pdf":
            try:
                reader = PdfReader(path)
                if reader.is_encrypted:
                    raise ValueError("encrypted")
                if not len(reader.pages):
                    raise ValueError("empty PDF")
                for i, page in enumerate(reader.pages):
                    if i >= 1000:
                        break
                    pages.append((i + 1, page.extract_text() or ""))
            except Exception:
                with self.store.connect() as db:
                    db.execute("UPDATE materials SET status='rejected',details=? WHERE id=?", (dumps({"error": "PDF is damaged, encrypted or unreadable"}), material_id))
                raise ResearchError("INVALID_FILE", "PDF is damaged, encrypted or unreadable") from None
            parse_status = "ready" if any(text.strip() for _, text in pages) else "empty"
            if row["role"] == "main_text":
                paper = loads(self._require_paper(row["paper_id"])["metadata"])
                if row["version_id"]:
                    with self.store.connect() as db:
                        version_meta = db.execute("SELECT metadata FROM versions WHERE id=?", (row["version_id"],)).fetchone()
                        if version_meta:
                            paper = loads(version_meta[0])
                first_page = pages[0][1] if pages else ""
                identity_region = re.split(r"\b(?:references|bibliography)\b", first_page, flags=re.I)[0]
                header = identity_region[:3000]
                arxiv_stamps = _arxiv_stamps(header)
                # A DOI can have historical provider rows mislabeled accepted
                # or unknown. An explicit stamp in the actual PDF can narrow
                # edition candidates, but never distinguish multiple revisions.
                observed_preprint = bool(arxiv_stamps or re.search(r"\b(?:bioRxiv|medRxiv)\s+preprint\b", header, re.I))
                if observed_preprint:
                    version_observations.update(observed_version_kind="preprint", version_kind_basis="first_page_preprint_stamp")
                # Only the first-page identity region, before the bibliography, counts.
                def extracted_doi(value):
                    # PDF extraction may concatenate the footer's "doi:" label.
                    value = re.sub(r"doi:$", "", value, flags=re.I)
                    value = value.rstrip(".,;")
                    while value.endswith(")") and value.count(")") > value.count("("):
                        value = value[:-1]
                    return normalized_doi(value)
                dois = {extracted_doi(x) for x in re.findall(r"10\.\d{4,9}/[^\s<>\"]+", identity_region, re.I)}
                if not row["version_id"]:
                    with self.store.connect() as db:
                        related = self._related_paper_ids(row["paper_id"], db)
                        versions = list(db.execute("SELECT * FROM versions WHERE paper_id IN (" + ",".join("?" for _ in related) + ")", related))
                        version_matches = []
                        for version in versions:
                            if observed_preprint and version["kind"] != "preprint":
                                continue
                            meta = loads(version["metadata"])
                            known_doi = normalized_doi(meta.get("doi"))
                            arxiv_id = _arxiv_identity(meta.get("identifiers", {}).get("arxiv") or normalized_doi(meta.get("doi")))
                            arxiv_match = arxiv_id and arxiv_stamps and all(_arxiv_matches(arxiv_id, stamp) for stamp in arxiv_stamps)
                            # A DOI generally identifies the arXiv work, not its revision.
                            if arxiv_match or (not arxiv_id and known_doi and known_doi in dois):
                                version_matches.append(version["id"])
                        if len(version_matches) == 1:
                            owner = next(v["paper_id"] for v in versions if v["id"] == version_matches[0])
                            db.execute("UPDATE materials SET version_id=?,paper_id=? WHERE id=?", (version_matches[0], owner, material_id))
                            row = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
                            paper = loads(next(v["metadata"] for v in versions if v["id"] == version_matches[0]))
                words = set(re.findall(r"[a-z0-9]+", paper["title"].lower()))
                observed = set(re.findall(r"[a-z0-9]+", header.lower()))
                title_match = len(words & observed) / max(1, len(words))
                expected = normalized_doi(paper.get("doi"))
                repository_ids = observed_identifiers(paper.get("identifiers"), landing_url=paper.get("landing_url"), fulltext_urls=paper.get("fulltext_urls"))
                expected_posted = paper.get("extra", {}).get("posted_date") or paper.get("extra", {}).get("archive_posted_date")
                if expected_posted and any(repository_ids.get(key) for key in ("biorxiv_revision", "medrxiv_revision")):
                    observed_posted = _posted_date(identity_region)
                    version_observations.update(expected_posted_date=expected_posted, observed_posted_date=observed_posted)
                    if not confirmed and observed_posted != expected_posted:
                        issues.append("Preprint posted-date stamp does not match the requested repository revision")
                exact_doi = bool(expected and expected in dois)
                if not confirmed:
                    if row["version_id"]:
                        with self.store.connect() as db:
                            declared = db.execute("SELECT kind FROM versions WHERE id=?", (row["version_id"],)).fetchone()
                            related = self._related_paper_ids(row["paper_id"], db)
                            other_versions = list(db.execute("SELECT id,kind,metadata FROM versions WHERE paper_id IN (" + ",".join("?" for _ in related) + ") AND id<>?", related + [row["version_id"]]))
                        expected_arxiv = _arxiv_identity(paper.get("identifiers", {}).get("arxiv") or normalized_doi(paper.get("doi")))
                        if declared and declared[0] == "preprint" and expected_arxiv and arxiv_stamps:
                            if any(not _arxiv_matches(expected_arxiv, stamp) for stamp in arxiv_stamps):
                                issues.append("File arXiv identity or revision conflicts with requested version")
                        if declared and declared[0] == "published":
                            for other in other_versions:
                                other_meta = loads(other["metadata"])
                                other_arxiv = _arxiv_identity(other_meta.get("identifiers", {}).get("arxiv") or normalized_doi(other_meta.get("doi")))
                                if other["kind"] == "preprint" and other_arxiv and any(other_arxiv[0] == stamp[0] for stamp in arxiv_stamps):
                                    issues.append("File contains a different preprint version identity")
                                    break
                    if title_match < .75 or len(words) < 3:
                        if not (exact_doi and title_match >= .4):
                            issues.append("Paper identity needs confirmation")
                    if expected and dois and not exact_doi:
                        issues.append("First-page DOI conflicts with expected identity")
                    if re.search(r"(?:supplementary (?:information|material)|supporting information|abstract.only)", first_page[:600], re.I):
                        issues.append("Main-text role needs confirmation")
                    if parse_status == "empty":
                        issues.append("Image-only PDF needs identity confirmation")
        elif row["format"] == "xml" and row["role"] == "main_text":
            try:
                pages, ids, observed_title, pagination = _jats_content(path.read_bytes())
            except ResearchError as error:
                with self.store.connect() as db:
                    db.execute("UPDATE materials SET status='rejected',parse_status='failed',details=? WHERE id=?", (dumps({"error": error.message}), material_id))
                raise
            parse_status = "ready"
            paper = loads(self._require_paper(row["paper_id"])["metadata"])
            if row["version_id"]:
                with self.store.connect() as db:
                    paper = loads(db.execute("SELECT metadata FROM versions WHERE id=?", (row["version_id"],)).fetchone()[0])
            expected_doi = normalized_doi(paper.get("doi"))
            observed_dois = {normalized_doi(value) for value in ids.get("doi", set())}
            expected_pmc = str(paper.get("identifiers", {}).get("pmcid") or "").upper()
            if expected_pmc.isdigit():
                expected_pmc = "PMC" + expected_pmc
            observed_pmc = {value.upper() for key in ("pmcid", "pmcid-ver") for value in ids.get(key, set())}
            exact_doi = bool(expected_doi and observed_dois == {expected_doi})
            exact_pmc = bool(expected_pmc and expected_pmc in observed_pmc)
            words = set(re.findall(r"[a-z0-9]+", paper["title"].lower()))
            title_words = set(re.findall(r"[a-z0-9]+", observed_title.lower()))
            title_match = len(words & title_words) / max(1, len(words))
            version_observations = {"pagination": pagination, "observed_article_ids": {key: sorted(values) for key, values in ids.items()}}
            if not confirmed:
                if not (exact_doi or exact_pmc) or title_match < .75:
                    issues.append("JATS article identity needs confirmation")
                if expected_doi and observed_dois and not exact_doi:
                    issues.append("JATS article DOI conflicts with expected identity")
                if expected_pmc and not exact_pmc:
                    issues.append("JATS repository identity or revision does not match requested version")
                repository_ids = observed_identifiers(paper.get("identifiers"), landing_url=paper.get("landing_url"), fulltext_urls=paper.get("fulltext_urls"))
                if any(repository_ids.get(key) for key in ("arxiv", "biorxiv_revision", "medrxiv_revision")):
                    issues.append("JATS preprint revision requires explicit identity confirmation")
        elif row["format"] in ("txt", "md", "py", "r", "csv", "json", "yaml", "yml", "xml", "html", "tex"):
            try:
                pages = [(1, path.read_text(encoding="utf-8"))]
                parse_status = "ready"
            except UnicodeError:
                parse_status = "failed"
            if row["role"] == "main_text" and not confirmed:
                issues.append("Non-PDF main text requires explicit identity confirmation")
        elif row["role"] == "main_text" and not confirmed:
            issues.append("Main text format needs confirmation")
        if issues:
            with self.store.connect() as db:
                db.execute("UPDATE materials SET details=?,parse_status=? WHERE id=?", (dumps({"identity_issues": issues, **version_observations}), parse_status, material_id))
            return self._material(self._require_material(material_id))
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        object_path = self.root / "objects" / "sha256" / sha[:2] / (sha + "." + re.sub(r"[^a-z0-9]", "", row["format"]))
        object_path.parent.mkdir(parents=True, exist_ok=True)
        if not object_path.exists():
            temp = object_path.with_suffix("." + uid("tmp"))
            shutil.copyfile(path, temp)
            temp.replace(object_path)
        parser = "jats-v1" if row["format"] == "xml" and row["role"] == "main_text" else "pypdf-v1"
        details = {"identity_confirmed": True, "identity_confirmation_method": "user" if confirmed else "automatic",
                   "parsed_pages": len(pages), "parser": parser, "confirmed_at": now()}
        details.update(version_observations)
        derived = self.root / "derived" / material_id / parser
        if pages:
            atomic_write(derived / "pages.jsonl", "\n".join(dumps({"page": p, "text": text}) for p, text in pages) + "\n")
            atomic_write(derived / "text.txt", "\n\n".join(text for _, text in pages))
        with self.store.connect() as db:
            db.execute("UPDATE materials SET status='available',parse_status=?,relative_path=?,sha256=?,details=? WHERE id=?",
                       (parse_status, str(object_path.relative_to(self.root)), sha, dumps(details), material_id))
            db.execute("DELETE FROM chunks WHERE material_id=?", (material_id,))
            db.execute("DELETE FROM search_fts WHERE material_id=?", (material_id,))
            for page, text in pages:
                db.execute("INSERT INTO chunks VALUES(?,?,?)", (material_id, page, text))
                db.execute("INSERT INTO search_fts(paper_id,material_id,page,text) VALUES(?,?,?,?)", (row["paper_id"], material_id, page, text))
            db.execute("UPDATE collections SET revision=revision+1 WHERE id IN(SELECT collection_id FROM memberships WHERE paper_id=?)", (row["paper_id"],))
        path.unlink(missing_ok=True)
        self._satisfy_tasks(row["paper_id"])
        return self._material(self._require_material(material_id))

    def confirm_material_identity(self, material_id, paper_id=None, role=None, version_id=None):
        row = self._require_material(material_id)
        if paper_id and paper_id != row["paper_id"]:
            raise ResearchError("INVALID_ARGUMENT", "Re-import to associate a material with a different paper")
        if role and role not in ("main_text", "supplement", "code", "data", "model", "other"):
            raise ResearchError("INVALID_ARGUMENT", "Unsupported role")
        with self.store.connect() as db:
            if version_id and not db.execute("SELECT 1 FROM versions WHERE id=? AND paper_id=?", (version_id, row["paper_id"])).fetchone():
                raise ResearchError("INVALID_ARGUMENT", "Version does not belong to paper")
            db.execute("UPDATE materials SET role=?,version_id=? WHERE id=?", (role or row["role"], version_id or row["version_id"], material_id))
        return self._finalize_material(material_id, confirmed=True)

    def read_material(self, material_id, view="text", pages=None, max_chars=12000, char_offset=0):
        row = self._require_material(material_id)
        item = self._material(row)
        if view not in ("text", "original") or not 1 <= max_chars <= 200000 or char_offset < 0:
            raise ResearchError("INVALID_ARGUMENT", "Invalid view or max_chars")
        logical_sections = item["details"].get("pagination", {}).get("kind") == "logical_sections"
        offset_scope = {"basis": "selected_logical_sections_concatenated_in_document_order" if logical_sections else "selected_pages_concatenated_in_physical_page_order",
                        "pages": list(pages) if pages is not None else None,
                        "on_pages_change": "reset char_offset to 0",
                        "next_page": "navigation only; replay continuation unchanged to continue reading"}
        if view == "original" or row["status"] != "available":
            return {"material": item, "content": None, "content_available": row["status"] == "available",
                    "continuation": None, "offset_scope": offset_scope}
        with self.store.connect() as db:
            content = list(db.execute("SELECT page,text FROM chunks WHERE material_id=? ORDER BY page", (material_id,)))
        if pages is not None:
            if not pages or any(not isinstance(p, int) or isinstance(p, bool) or p < 1 for p in pages):
                raise ResearchError("INVALID_ARGUMENT", "Pages must be positive logical section numbers" if logical_sections else "Pages must be positive physical page numbers")
            found = {p["page"] for p in content}
            if set(pages) - found:
                raise ResearchError("INVALID_ARGUMENT", "Requested pages have no extracted text", details={"available_pages": sorted(found)})
            content = [p for p in content if p["page"] in pages]
        result, used, truncated, next_page = [], 0, False, None
        skip, total = char_offset, sum(len(p["text"]) for p in content)
        for p in content:
            if skip >= len(p["text"]):
                skip -= len(p["text"])
                continue
            left = max_chars - used
            if left <= 0:
                truncated, next_page = True, p["page"]
                break
            text = p["text"][skip:skip + left]
            result.append({"page": p["page"], "text": text, "page_char_offset": skip})
            used += len(text)
            remaining = len(p["text"]) - skip
            skip = 0
            if len(text) < remaining:
                truncated, next_page = True, p["page"]
                break
        truncated = char_offset + used < total
        continuation = None
        if truncated:
            continuation = {"material_id": material_id, "view": view, "max_chars": max_chars, "char_offset": char_offset + used}
            if pages is not None:
                continuation["pages"] = list(pages)
        return {"material": item, "pages": result, "content_available": bool(result), "truncated": truncated,
                "char_offset": char_offset, "next_char_offset": char_offset + used if truncated else None,
                "next_page": next_page, "continuation": continuation, "offset_scope": offset_scope,
                "notice": None if result else "TEXT_UNAVAILABLE: use the original local_path"}

    def export_collection(self, collection_id, output_dir, format="bundle"):
        self._require_collection(collection_id)
        if format not in ("bundle", "jsonl", "bibtex"):
            raise ResearchError("INVALID_ARGUMENT", "Expected bundle, jsonl or bibtex")
        output = Path(output_dir).expanduser().resolve()
        # Objects/catalog must never be overwritten by a material export.
        if output == self.root or output.is_relative_to(self.root / "objects") or output.is_relative_to(self.root / "derived"):
            raise ResearchError("INVALID_ARGUMENT", "Choose a separate export directory")
        if output.exists() and any(output.iterdir()):
            raise ResearchError("INVALID_ARGUMENT", "Export requires a new or empty directory to avoid mixing old collection files")
        output.mkdir(parents=True, exist_ok=True)
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT paper_id FROM memberships WHERE collection_id=? AND status='included' ORDER BY paper_id", (collection_id,))]
        records = [self.get_paper(pid) for pid in ids]
        exported = []
        for paper in records:
            if format == "bundle":
                for m in paper["materials"]:
                    if m["status"] != "available" or not m["exists"]:
                        continue
                    destination = output / "papers" / paper["paper_id"] / (m["material_id"] + "." + m["format"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(m["local_path"], destination)
                    m["export_path"] = str(destination)
                    m["bundle_path"] = destination.relative_to(output).as_posix()
                atomic_write(output / "papers" / paper["paper_id"] / "metadata.json", json.dumps(paper, ensure_ascii=False, indent=2))
            exported.append(paper)
        if format in ("bundle", "jsonl"):
            atomic_write(output / "papers.jsonl", "\n".join(dumps(p) for p in exported) + ("\n" if exported else ""))
        if format in ("bundle", "bibtex"):
            def esc(text):
                return str(text or "").replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}")
            entries = []
            for p in records:
                entries.append("@article{" + p["paper_id"] + ",\n  title={" + esc(p["title"]) + "},\n  author={" + " and ".join(esc(a) for a in p.get("authors", [])) + "},\n  year={" + str(p.get("year") or "") + "},\n  doi={" + esc(p.get("doi")) + "}\n}")
            atomic_write(output / "references.bib", "\n\n".join(entries) + "\n")
        manifest = {"schema_version": 1, "collection_id": collection_id, "library_id": self.library_id,
                    "created_at": now(), "paper_count": len(records), "papers": exported}
        if format == "bundle":
            atomic_write(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return {"output_dir": str(output), "paper_count": len(records), "format": format,
                "manifest_path": str(output / "manifest.json") if format == "bundle" else None,
                "portable": format == "bundle", "note": "Use bundle_path relative to this directory on another machine"}
