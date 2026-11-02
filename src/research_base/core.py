"""Single local library API shared by CLI, MCP and model agents."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .materials import MaterialsMixin, normalized_doi, safe_url
from .models import Candidate, ResearchError
from .storage import Store, dumps, loads, now, uid
from .jobs import JobsMixin
from .workflows import DiscoveryWorkflowsMixin
from .identity import observed_identifiers
from .research import ResearchSpaceMixin


class ResearchBase(ResearchSpaceMixin, MaterialsMixin, DiscoveryWorkflowsMixin, JobsMixin):
    def __init__(self, library_path, *, source_registry=None, client=None, max_file_bytes=100 * 1024 * 1024, scholarly_client=None):
        self.store = Store(library_path)
        self.root, self.library_id = self.store.root, self.store.library_id
        self.max_file_bytes = max_file_bytes
        self.client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "ResearchBase/0.1 (research literature collector)"})
        self._owns_client = client is None
        if source_registry is None:
            from .sources import SourceRegistry
            source_registry = SourceRegistry()
        self.sources = source_registry
        self._scholarly_client = scholarly_client

    def close(self):
        if self._owns_client:
            self.client.close()
        close = getattr(self.sources, "close", None)
        if close:
            close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def list_sources(self):
        return self.sources.list_sources()

    def _require_collection(self, collection_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()
        if not row:
            raise ResearchError("NOT_FOUND", "Collection does not exist")
        return row

    def _require_paper(self, paper_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            raise ResearchError("NOT_FOUND", "Paper does not exist")
        return row

    def create_collection(self, name, description="", config=None):
        if not isinstance(name, str) or not name.strip():
            raise ResearchError("INVALID_ARGUMENT", "Collection name is required")
        cid = uid("col")
        with self.store.connect() as db:
            db.execute("INSERT INTO collections(id,name,description,config,created_at) VALUES(?,?,?,?,?)", (cid, name.strip(), description, dumps(config or {}), now()))
        return self.get_collection(cid)

    def get_collection(self, collection_id):
        row = dict(self._require_collection(collection_id))
        row["collection_id"] = row.pop("id")
        row["config"] = loads(row["config"], {})
        with self.store.connect() as db:
            row["counts"] = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM memberships WHERE collection_id=? GROUP BY status", (collection_id,))}
        return row

    def list_collections(self):
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM collections ORDER BY created_at,id")]
        return [self.get_collection(cid) for cid in ids]

    def update_collection(self, collection_id, config=None, name=None, description=None):
        row = self._require_collection(collection_id)
        with self.store.connect() as db:
            db.execute("UPDATE collections SET name=?,description=?,config=?,revision=revision+1 WHERE id=?",
                       (name or row["name"], description if description is not None else row["description"], dumps(config if config is not None else loads(row["config"])), collection_id))
        return self.get_collection(collection_id)

    def set_membership(self, collection_id, paper_ids, status="included"):
        self._require_collection(collection_id)
        if status not in ("candidate", "included", "excluded") or len(paper_ids) > 10000:
            raise ResearchError("INVALID_ARGUMENT", "Invalid membership state or batch size")
        for pid in paper_ids:
            self._require_paper(pid)
        with self.store.connect() as db:
            for pid in set(paper_ids):
                db.execute("INSERT INTO memberships VALUES(?,?,?) ON CONFLICT(collection_id,paper_id) DO UPDATE SET status=excluded.status", (collection_id, pid, status))
            db.execute("UPDATE collections SET revision=revision+1 WHERE id=?", (collection_id,))
        return {"collection_id": collection_id, "paper_ids": list(dict.fromkeys(paper_ids)), "status": status, "count": len(set(paper_ids))}

    def add_paper(self, candidate, collection_id=None, status="included"):
        """Import known metadata without an external search, for researchers and tests."""
        candidate = Candidate.model_validate(candidate)
        if collection_id:
            self._require_collection(collection_id)
        paper_id = self._upsert_candidate(candidate)
        if collection_id:
            self.set_membership(collection_id, [paper_id], status)
        return self.get_paper(paper_id)

    def _upsert_candidate(self, candidate):
        data = candidate.model_dump()
        if not candidate.title.strip() or not candidate.source_id:
            raise ResearchError("INVALID_ARGUMENT", "Candidate title and source identifier are required")
        data["doi"] = normalized_doi(candidate.doi)
        ids = observed_identifiers(candidate.identifiers, doi=data["doi"], landing_url=candidate.landing_url, fulltext_urls=candidate.fulltext_urls)
        ids[candidate.source.lower()] = candidate.source_id
        if data["doi"]:
            ids["doi"] = data["doi"]
        if "doi" in ids:
            ids["doi"] = normalized_doi(ids["doi"])
            data["doi"] = data["doi"] or ids["doi"]
        # arXiv's publication DOI links two versions; it is not a preprint DOI.
        published_doi = normalized_doi(data.get("extra", {}).get("published_doi"))
        arxiv = ids.get("arxiv")
        if arxiv:
            base_arxiv = re.sub(r"v\d+$", "", arxiv)
            ids["arxiv_work"] = base_arxiv
        data["identifiers"] = ids
        data["landing_url"] = safe_url(data.get("landing_url"))
        data["fulltext_urls"] = list(dict.fromkeys(safe_url(u) for u in data["fulltext_urls"]))
        for link in data["links"]:
            link["url"] = safe_url(link["url"])
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            matches = []
            for ns, value in ids.items():
                match = db.execute("SELECT * FROM identifiers WHERE namespace=? AND value=?", (ns, value)).fetchone()
                if match:
                    matches.append(match)
            if published_doi:
                match = db.execute("SELECT * FROM identifiers WHERE namespace='doi' AND value=?", (published_doi,)).fetchone()
                if match:
                    matches.append(match)
            if data["doi"]:
                match = db.execute("SELECT * FROM identifiers WHERE namespace='related_doi' AND value=?", (data["doi"],)).fetchone()
                if match:
                    matches.append(match)
            papers = {m["paper_id"] for m in matches}
            if len(papers) > 1:
                publication = db.execute("SELECT paper_id FROM identifiers WHERE namespace='doi' AND value=?", (published_doi,)).fetchone() if published_doi else None
                other = papers - ({publication[0]} if publication else set())
                if publication and len(other) == 1:
                    paper_id = next(iter(other))
                    db.execute("INSERT OR IGNORE INTO paper_relations VALUES(?,?,?,?)", (publication[0], paper_id, candidate.source, now()))
                else:
                    raise ResearchError("IDENTITY_CONFLICT", "Source identifiers point to different existing papers")
            else:
                paper_id = next(iter(papers), uid("paper"))
            existing = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not existing:
                db.execute("INSERT INTO papers(id,metadata,created_at,updated_at) VALUES(?,?,?,?)", (paper_id, dumps(data), now(), now()))
            # A DOI or repository ID can point to a different edition. Choose an
            # existing version only when its known edition identity is compatible.
            exact_arxiv = next((m for m in matches if m["namespace"] == "arxiv" and m["value"] == arxiv), None) if arxiv else None
            exact_doi = next((m for m in matches if m["namespace"] == "doi" and m["value"] == data["doi"]), None)
            source_record = db.execute("SELECT version_id FROM source_records WHERE source=? AND source_id=?", (candidate.source, candidate.source_id)).fetchone()
            version_id, version = uid("ver"), None
            possible = [match["version_id"] for match in (exact_arxiv, exact_doi) if match and match["version_id"]]
            if source_record:
                possible.append(source_record[0])
            for possible_id in dict.fromkeys(possible):
                prior_version = db.execute("SELECT metadata,paper_id FROM versions WHERE id=?", (possible_id,)).fetchone()
                if not prior_version or prior_version["paper_id"] != paper_id:
                    continue
                old_identity = loads(prior_version[0])
                old_kind = old_identity.get("version_kind", "unknown")
                kind_changed = old_kind != "unknown" and data["version_kind"] != "unknown" and old_kind != data["version_kind"]
                doi_changed = data["doi"] and old_identity.get("doi") not in (None, data["doi"])
                old_arxiv = old_identity.get("identifiers", {}).get("arxiv")
                arxiv_changed = bool(old_arxiv and arxiv and old_arxiv != arxiv
                                     and (re.search(r"v\d+$", old_arxiv) or re.search(r"v\d+$", arxiv)))
                if not (kind_changed or doi_changed or arxiv_changed):
                    version_id, version = possible_id, prior_version
                    break
            if version:
                old = loads(version[0])
                for field in ("fulltext_urls", "references"):
                    data[field] = list(dict.fromkeys(old.get(field, []) + data.get(field, [])))
                if not data["abstract"]:
                    data["abstract"] = old.get("abstract", "")
                for field in ("authors", "year", "doi", "landing_url"):
                    if not data.get(field):
                        data[field] = old.get(field)
                data["identifiers"] = {**old.get("identifiers", {}), **ids}
                if data["version_kind"] == "unknown":
                    data["version_kind"] = old.get("version_kind", "unknown")
            db.execute("INSERT INTO versions(id,paper_id,kind,metadata) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,metadata=excluded.metadata",
                       (version_id, paper_id, data["version_kind"], dumps(data)))
            for ns, value in ids.items():
                db.execute("INSERT OR IGNORE INTO identifiers VALUES(?,?,?,?)", (ns, value, paper_id, version_id))
            if published_doi:
                db.execute("INSERT OR IGNORE INTO identifiers VALUES('related_doi',?,?,NULL)", (published_doi, paper_id))
            db.execute("INSERT INTO source_records VALUES(?,?,?,?,?,?) ON CONFLICT(source,source_id) DO UPDATE SET metadata=excluded.metadata,fetched_at=excluded.fetched_at,version_id=excluded.version_id,paper_id=excluded.paper_id",
                       (candidate.source, candidate.source_id, paper_id, version_id, dumps(data), now()))
            preferred = existing["preferred_version_id"] if existing else None
            prior = loads(existing["metadata"], {}) if existing else {}
            rank = {"unknown": 0, "preprint": 1, "accepted": 2, "published": 3}
            new_rank, old_rank = rank.get(data["version_kind"], 0), rank.get(prior.get("version_kind"), 0)
            newer = new_rank > old_rank
            if new_rank == old_rank:
                old_arxiv = prior.get("identifiers", {}).get("arxiv", "")
                new_arxiv = data.get("identifiers", {}).get("arxiv", "")
                old_revision, new_revision = re.search(r"v(\d+)$", old_arxiv), re.search(r"v(\d+)$", new_arxiv)
                if old_revision and new_revision and re.sub(r"v\d+$", "", old_arxiv) == re.sub(r"v\d+$", "", new_arxiv):
                    newer = int(new_revision[1]) >= int(old_revision[1])
                else:
                    newer = preferred == version_id
            if not preferred or newer:
                preferred, prior = version_id, data
            db.execute("UPDATE papers SET preferred_version_id=?,metadata=?,updated_at=? WHERE id=?", (preferred, dumps(prior), now(), paper_id))
            db.execute("DELETE FROM search_fts WHERE paper_id=? AND material_id=''", (paper_id,))
            db.execute("INSERT INTO search_fts(paper_id,material_id,page,text) VALUES(?,'',0,?)", (paper_id, prior["title"] + "\n" + prior.get("abstract", "")))
            # Register external artifacts without downloading or executing their contents.
            for link in data.get("links", []):
                if not db.execute("SELECT 1 FROM materials WHERE paper_id=? AND source_url=? AND role=?", (paper_id, link["url"], link["role"])).fetchone():
                    db.execute("INSERT INTO materials(id,paper_id,version_id,role,format,status,source_url,created_at) VALUES(?,?,?,?,?,'external',?,?)",
                               (uid("mat"), paper_id, version_id, link["role"], link["format"], link["url"], now()))
        return paper_id

    def _related_paper_ids(self, paper_id, db=None):
        sql = """WITH RECURSIVE linked(id) AS (
                 SELECT ? UNION SELECT CASE WHEN r.published_paper_id=l.id THEN r.preprint_paper_id ELSE r.published_paper_id END
                 FROM paper_relations r JOIN linked l ON r.published_paper_id=l.id OR r.preprint_paper_id=l.id)
                 SELECT id FROM linked ORDER BY id"""
        if db is not None:
            return [r[0] for r in db.execute(sql, (paper_id,))]
        with self.store.connect() as connection:
            return [r[0] for r in connection.execute(sql, (paper_id,))]

    def get_paper(self, paper_id, include_raw=False):
        row = self._require_paper(paper_id)
        data = loads(row["metadata"])
        with self.store.connect() as db:
            related = self._related_paper_ids(paper_id, db)
            placeholders = ",".join("?" for _ in related)
            versions = [{"version_id": v["id"], "paper_id": v["paper_id"], "kind": v["kind"], "metadata": loads(v["metadata"])} for v in db.execute("SELECT * FROM versions WHERE paper_id IN (" + placeholders + ") ORDER BY id", related)]
            records = [{"source": r[0], "source_id": r[1]} for r in db.execute("SELECT source,source_id FROM source_records WHERE paper_id=?", (paper_id,))]
            date_observations = []
            date_keys = ("publication_date", "first_publication_date", "first_submitted_at", "version_updated_at",
                         "publication_month", "publication_year", "publication_date_precision", "publication_interval_start",
                         "publication_interval_end", "archive_posted_date", "archive_version", "index_updated_at", "first_index_date")
            for record in db.execute("SELECT source,source_id,version_id,metadata,fetched_at FROM source_records WHERE paper_id IN (" + placeholders + ") ORDER BY fetched_at DESC", related):
                extra = loads(record[3], {}).get("extra", {})
                observed = {key: extra[key] for key in date_keys if extra.get(key) not in (None, "")}
                if observed:
                    date_observations.append({"source": record[0], "source_id": record[1], "version_id": record[2],
                                              "observed_at": record[4], "dates": observed})
            memberships = [{"collection_id": r[0], "status": r[1]} for r in db.execute("SELECT collection_id,status FROM memberships WHERE paper_id=?", (paper_id,))]
            tags = [r[0] for r in db.execute("SELECT tag FROM paper_tags WHERE paper_id=? ORDER BY tag", (paper_id,))]
        materials = self.list_materials(paper_id=paper_id)
        if not include_raw:
            data["reference_count"] = len(data.pop("references", []))
            data.pop("extra", None)
            for version in versions:
                version["metadata"].pop("references", None)
                version["metadata"].pop("extra", None)
                text = version["metadata"].get("abstract", "")
                if len(text) > 2000:
                    version["metadata"]["abstract"] = text[:2000]
                    version["metadata"]["abstract_truncated"] = True
        return {**data, "paper_id": paper_id, "preferred_version_id": row["preferred_version_id"], "versions": versions,
                "sources": records, "memberships": memberships, "materials": materials, "tags": tags, "related_paper_ids": [p for p in related if p != paper_id],
                "publication_history": date_observations[:32], "publication_history_truncated": len(date_observations) > 32,
                "fulltext_available": any(m["role"] == "main_text" and m["status"] == "available" and m["exists"] for m in materials),
                "text_searchable": any(m["role"] == "main_text" and m["status"] == "available" and m["parse_status"] == "ready" for m in materials)}

    def list_papers(self, collection_id=None, membership="included", limit=100, offset=0, tag=None):
        if not 1 <= limit <= 200 or offset < 0 or membership not in ("included", "candidate", "excluded", "all"):
            raise ResearchError("INVALID_ARGUMENT", "Invalid paper pagination or membership")
        params, where = [], ""
        if collection_id:
            self._require_collection(collection_id)
            where = " WHERE EXISTS(SELECT 1 FROM memberships m WHERE m.paper_id=p.id AND m.collection_id=?"
            params.append(collection_id)
            if membership != "all":
                where += " AND m.status=?"
                params.append(membership)
            where += ")"
        if tag:
            where += (" AND " if where else " WHERE ") + "EXISTS(SELECT 1 FROM paper_tags pt WHERE pt.paper_id=p.id AND pt.tag=?)"
            params.append(tag)
        with self.store.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM papers p" + where, params).fetchone()[0]
            rows = list(db.execute("SELECT p.id,p.metadata FROM papers p" + where + " ORDER BY p.created_at,p.id LIMIT ? OFFSET ?", params + [limit, offset]))
        papers = []
        for row in rows:
            meta = loads(row["metadata"])
            papers.append({"paper_id": row["id"], **{k: meta.get(k) for k in ("title", "authors", "year", "doi", "version_kind")}})
        return {"papers": papers, "total": total, "next_offset": offset + limit if offset + limit < total else None}

    def set_tags(self, paper_id, tags, replace=False):
        self._require_paper(paper_id)
        if not isinstance(tags, list) or len(tags) > 100 or any(not isinstance(t, str) or not t.strip() or len(t) > 128 for t in tags):
            raise ResearchError("INVALID_ARGUMENT", "Tags must be a list of at most 100 nonempty strings (128 characters each)")
        with self.store.connect() as db:
            if replace:
                db.execute("DELETE FROM paper_tags WHERE paper_id=?", (paper_id,))
            for tag in tags:
                db.execute("INSERT OR IGNORE INTO paper_tags VALUES(?,?)", (paper_id, tag.strip()))
            current = [r[0] for r in db.execute("SELECT tag FROM paper_tags WHERE paper_id=? ORDER BY tag", (paper_id,))]
        return {"paper_id": paper_id, "tags": current}

    def import_bibliography(self, path, collection_id, format=None, membership="candidate"):
        self._require_collection(collection_id)
        if membership not in ("candidate", "included", "excluded"):
            raise ResearchError("INVALID_ARGUMENT", "Invalid import membership")
        from .bibliography import parse_bibliography
        entries = parse_bibliography(path, format=format)
        ids, warnings = [], []
        for index, entry in enumerate(entries):
            try:
                tags = entry.get("tags", [])
                if not isinstance(tags, list) or len(tags) > 100 or any(not isinstance(t, str) or not t.strip() or len(t) > 128 for t in tags):
                    raise ResearchError("INVALID_ARGUMENT", "Import tags exceed the supported length or count")
                pid = self._upsert_candidate(entry["candidate"])
                with self.store.connect() as db:
                    # Reimporting a bibliography must not re-include an excluded paper.
                    db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?,?)", (collection_id, pid, membership))
                self.set_tags(pid, tags)
                ids.append(pid)
                warnings.extend(entry.get("warnings", []))
            except ResearchError as error:
                warnings.append(f"Entry {index + 1}: {error.code}")
        if ids:
            with self.store.connect() as db:
                db.execute("UPDATE collections SET revision=revision+1 WHERE id=?", (collection_id,))
        return {"collection_id": collection_id, "record_count": len(entries), "paper_count": len(set(ids)),
                "paper_ids": list(dict.fromkeys(ids)), "warnings": warnings, "attachments_auto_imported": False}

    def search_library(self, query, collection_id=None, membership="included", limit=20, offset=0, **kwargs):
        if set(kwargs) - {"content_scope", "sort"}:
            raise ResearchError("INVALID_ARGUMENT", "Unsupported library search option")
        sort = kwargs.get("sort", "relevance")
        if sort not in ("relevance", "newest"):
            raise ResearchError("INVALID_ARGUMENT", "Sort must be relevance or newest")
        if not isinstance(query, str) or not query.strip() or not 1 <= limit <= 200 or offset < 0:
            raise ResearchError("INVALID_ARGUMENT", "A nonempty query, limit 1..200 and nonnegative offset are required")
        if membership not in ("included", "candidate", "excluded", "all"):
            raise ResearchError("INVALID_ARGUMENT", "Unsupported membership filter")
        if collection_id:
            self._require_collection(collection_id)
        terms = re.findall(r"[\w]+", query, re.UNICODE)[:40]
        # Natural language research tasks need broad candidate recall; rank exact overlaps.
        expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in terms)
        if not expression:
            return {"papers": [], "total": 0, "next_offset": None, "search_mode": "fts"}
        where, params = [], []
        if collection_id:
            where.append("EXISTS(SELECT 1 FROM memberships x WHERE x.paper_id=search_fts.paper_id AND x.collection_id=?" + (" AND x.status=?" if membership != "all" else "") + ")")
            params.append(collection_id)
            if membership != "all":
                params.append(membership)
        scope = kwargs.get("content_scope", "all")
        if scope not in ("all", "fulltext", "metadata"):
            raise ResearchError("INVALID_ARGUMENT", "Invalid content_scope")
        if scope == "fulltext":
            where.append("material_id<>''")
        elif scope == "metadata":
            where.append("material_id=''")
        suffix = " AND " + " AND ".join(where) if where else ""
        with self.store.connect() as db:
            rows = list(db.execute("SELECT paper_id,material_id,page,text,bm25(search_fts) AS rank FROM search_fts WHERE search_fts MATCH ?" + suffix + " ORDER BY rank LIMIT 5000", [expression] + params))
            mode = "fts"
            if re.search(r"[\u3400-\u9fff]", query):
                cjk = re.findall(r"[\u3400-\u9fff]+", query)[:10]
                for term in cjk:
                    more = list(db.execute("SELECT paper_id,material_id,page,text,0 AS rank FROM search_fts WHERE instr(text,?)>0" + suffix + " LIMIT 5000", [term] + params))
                    rows.extend(more)
                mode = "fts+cjk_substring"
        grouped = {}
        query_lower = query.lower()
        for r in rows:
            item = grouped.setdefault(r["paper_id"], {"score": 0, "matches": [], "seen": set()})
            key = (r["material_id"], r["page"])
            if key in item["seen"]:
                continue
            item["seen"].add(key)
            text = r["text"]
            overlaps = sum(term.lower() in text.lower() for term in terms)
            item["score"] += overlaps + (3 if query_lower in text.lower() else 0)
            positions = [text.lower().find(term.lower()) for term in terms if term.lower() in text.lower()]
            start = max(0, min(positions or [0]) - 120)
            if len(item["matches"]) < 3:
                item["matches"].append({"material_id": r["material_id"] or None, "page": int(r["page"]) or None,
                                        "text": text[start:start + 900], "content_scope": "fulltext" if r["material_id"] else "metadata"})
        ordered = sorted(grouped, key=lambda pid: grouped[pid]["score"], reverse=True)
        if sort == "newest":
            ordered.sort(key=lambda pid: loads(self._require_paper(pid)["metadata"]).get("year") or 0, reverse=True)
        papers = []
        for pid in ordered[offset:offset + limit]:
            full = self.get_paper(pid)
            paper = {key: full[key] for key in ("paper_id", "title", "authors", "year", "doi", "abstract", "identifiers", "version_kind", "preferred_version_id", "fulltext_available", "text_searchable", "tags", "sources", "memberships", "materials")}
            if len(paper["abstract"]) > 2000:
                paper["abstract"] = paper["abstract"][:2000]
                paper["abstract_truncated"] = True
            paper["matches"] = grouped[pid]["matches"]
            papers.append(paper)
        return {"papers": papers, "total": len(ordered), "next_offset": offset + limit if offset + limit < len(ordered) else None,
                "search_mode": mode, "candidate_window_limited": len(rows) >= 5000}

    def prepare_context(self, task, collection_id=None, project_root=None, max_papers=None, max_chars=None, freshness_days=None):
        config = {}
        if project_root:
            import yaml
            path = Path(project_root).resolve() / ".research" / "config.yaml"
            if not path.is_file():
                raise ResearchError("NOT_FOUND", "Project has no .research/config.yaml")
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if config.get("library_id") and config["library_id"] != self.library_id:
                raise ResearchError("LIBRARY_ID_MISMATCH", "Project is bound to a different library")
            if collection_id and config.get("collection_id") != collection_id:
                raise ResearchError("INVALID_ARGUMENT", "Collection conflicts with project binding")
            collection_id = config.get("collection_id")
        if not collection_id:
            raise ResearchError("INVALID_ARGUMENT", "collection_id or project_root is required")
        collection = self.get_collection(collection_id)
        context_defaults = {**collection["config"].get("context", {}), **config.get("context", {})}
        max_papers = max_papers if max_papers is not None else context_defaults.get("max_papers", 8)
        max_chars = max_chars if max_chars is not None else context_defaults.get("max_chars", 12000)
        freshness_defaults = {**collection["config"].get("freshness", {}), **config.get("freshness", {})}
        freshness_days = freshness_days if freshness_days is not None else freshness_defaults.get("max_age_days", 30)
        if (not isinstance(freshness_days, int) or isinstance(freshness_days, bool)
                or not 0 <= freshness_days <= 36500):
            raise ResearchError("INVALID_ARGUMENT", "freshness_days must be an integer in 0..36500")
        if not isinstance(max_papers, int) or not isinstance(max_chars, int) or not 1 <= max_papers <= 50 or not 256 <= max_chars <= 200000:
            raise ResearchError("INVALID_ARGUMENT", "Invalid context budget")
        discovery = config.get("discovery") or collection["config"].get("discovery") or {}
        scope_basis = "configured_plan"
        if not discovery.get("query"):
            with self.store.connect() as db:
                plans = [loads(r[0]) for r in db.execute("SELECT payload FROM jobs WHERE collection_id=? AND type='discovery' ORDER BY created_at DESC", (collection_id,))]
            unique_plans = {self._plan_fingerprint(p): p for p in plans}
            if len(unique_plans) == 1:
                discovery = next(iter(unique_plans.values()))
                scope_basis = "only_observed_plan"
            else:
                scope_basis = "ambiguous_or_missing_plan"
        fingerprint = self._plan_fingerprint(discovery) if discovery.get("query") else None
        with self.store.connect() as db:
            run = db.execute("SELECT * FROM search_runs WHERE collection_id=? AND fingerprint=?", (collection_id, fingerprint)).fetchone() if fingerprint else None
            other_run = db.execute("SELECT 1 FROM search_runs WHERE collection_id=? LIMIT 1", (collection_id,)).fetchone()
        reason = None if run else "scope_changed" if other_run else "never_completed"
        age, coverage_age = None, None
        if run:
            checked_now = datetime.now(timezone.utc)
            age = (checked_now - datetime.fromisoformat(run["completed_at"].replace("Z", "+00:00"))).total_seconds() / 86400
            if age > freshness_days:
                reason = "stale"
            try:
                coverage_date = datetime.fromisoformat(run["coverage_through"][:10]).date()
                coverage_age = (checked_now.date() - coverage_date).days
                if coverage_age > freshness_days and reason is None:
                    reason = "coverage_stale"
            except (ValueError, TypeError):
                reason = "coverage_unknown"
        result = self.search_library(task, collection_id=collection_id, limit=max_papers)
        papers, used, truncated, gaps = [], 0, bool(result["next_offset"]), []
        for full in result["papers"]:
            item = {key: full[key] for key in ("paper_id", "title", "authors", "year", "doi", "identifiers", "preferred_version_id", "fulltext_available", "text_searchable")}
            item["materials"] = full["materials"]
            item["matches"] = []
            # Bound the generated text, while keeping original paths as first-class entries.
            for match in full["matches"]:
                available = max_chars - used
                if available <= 0:
                    truncated = True
                    break
                text = match["text"][:available]
                if len(text) < len(match["text"]):
                    truncated = True
                item["matches"].append({**match, "text": text})
                used += len(text)
            papers.append(item)
            if not full["fulltext_available"]:
                gaps.append({"paper_id": full["paper_id"], "reason": "fulltext_unavailable"})
        if not papers:
            gaps.append({"reason": "no_local_matches"})
        return {"collection_id": collection_id, "collection_revision": collection["revision"], "task": task, "generated_at": now(),
                "freshness": {"plan_fingerprint": fingerprint, "scope_basis": scope_basis, "last_complete_search_at": run["completed_at"] if run else None,
                              "coverage_through": run["coverage_through"] if run else None,
                              "search_scope": {key: discovery.get(key) for key in ("query", "sources", "source_queries", "date_from", "date_to")},
                              "checked_age_days": age, "coverage_age_days": coverage_age,
                              "max_age_days": freshness_days, "needs_refresh": reason is not None, "reason": reason,
                              "notice": "Freshness describes the completed provider queries and date window, not exhaustive field coverage or scientific quality."},
                "papers": papers, "gaps": gaps, "truncated": truncated, "limits": {"max_papers": max_papers, "max_chars": max_chars},
                "access": {"mode": "local_filesystem", "note": "local_path/text_path may be read directly by agents on this machine; otherwise use read_material or export a bundle"}}

    def _plan_fingerprint(self, plan):
        normalized = {k: plan.get(k) for k in ("query", "sources", "source_queries", "date_from", "date_to")}
        normalized["sources"] = sorted(normalized.get("sources") or [s["name"] for s in self.list_sources() if s.get("default_enabled", True)])
        normalized["source_queries"] = normalized.get("source_queries") or {}
        return "sha256:" + hashlib.sha256(dumps(normalized).encode()).hexdigest()
