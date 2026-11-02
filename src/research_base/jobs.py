"""Persistent discovery/acquisition jobs and host browser handoff."""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .materials import safe_url
from .models import ResearchError
from .storage import dumps, loads, now, uid


TERMINAL = ("succeeded", "failed", "cancelled")


def access_result(previous, detail):
    """Keep route diagnostics when a browser handoff replaces the current result."""
    saved = loads(previous, {}) if isinstance(previous, str) else previous or {}
    return {**{key: saved[key] for key in ("route_attempts", "resolver_diagnostics", "open_access_errors", "pending_material_ids") if key in saved}, **detail}


class JobsMixin:
    def _new_job(self, kind, collection_id, payload, request_id=None):
        self._require_collection(collection_id)
        fingerprint = hashlib.sha256(dumps(payload).encode()).hexdigest()
        scope = kind + ":" + collection_id
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_id:
                old = db.execute("SELECT * FROM requests WHERE operation=? AND request_id=?", (scope, request_id)).fetchone()
                if old:
                    if old["fingerprint"] != fingerprint:
                        raise ResearchError("IDEMPOTENCY_CONFLICT", "Request ID was used with different arguments")
                    return old["job_id"], False
            jid = uid("job")
            db.execute("INSERT INTO jobs(id,type,collection_id,state,payload,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?)", (jid, kind, collection_id, dumps(payload), now(), now()))
            if request_id:
                db.execute("INSERT INTO requests VALUES(?,?,?,?)", (scope, request_id, fingerprint, jid))
        return jid, True

    def _job_row(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ResearchError("NOT_FOUND", "Job does not exist")
        return row

    def discover_papers(self, collection_id, query, sources=None, source_queries=None, date_from=None, date_to=None, budget=None, request_id=None):
        defaults = loads(self._require_collection(collection_id)["config"], {}).get("discovery", {})
        sources = sources if sources is not None else defaults.get("sources")
        source_queries = source_queries if source_queries is not None else defaults.get("source_queries")
        date_from = date_from if date_from is not None else defaults.get("date_from")
        date_to = date_to if date_to is not None else defaults.get("date_to")
        budget = budget if budget is not None else defaults.get("budget")
        if not isinstance(query, str) or not query.strip():
            raise ResearchError("INVALID_ARGUMENT", "Search query is required")
        available = {s["name"] for s in self.list_sources()}
        if not sources:
            sources = [s["name"] for s in self.list_sources() if s.get("default_enabled", True)]
        if not sources or set(sources) - available:
            raise ResearchError("INVALID_ARGUMENT", "Unknown literature source", details={"available_sources": sorted(available)})
        from datetime import date
        try:
            for value in (date_from, date_to):
                if value:
                    date.fromisoformat(value)
            if date_from and date_to and date_from > date_to:
                raise ValueError()
        except (ValueError, TypeError):
            raise ResearchError("INVALID_ARGUMENT", "Dates must use YYYY-MM-DD and an ordered range") from None
        budget = budget or {"max_requests": 100}
        if set(budget) - {"max_requests"}:
            raise ResearchError("INVALID_ARGUMENT", "Only max_requests is supported in a discovery budget")
        if not 1 <= budget.get("max_requests", 100) <= 10000:
            raise ResearchError("INVALID_ARGUMENT", "Request budget must be 1..10000")
        payload = {"query": query.strip(), "sources": sorted(set(sources)), "source_queries": source_queries or {},
                   "date_from": date_from, "date_to": date_to, "budget": budget}
        jid, _ = self._new_job("discovery", collection_id, payload, request_id)
        return self.get_job(jid)

    def get_job(self, job_id, **kwargs):
        if set(kwargs) - {"task_state", "cursor", "limit"}:
            raise ResearchError("INVALID_ARGUMENT", "Unsupported job query option")
        row = self._job_row(job_id)
        if row["type"] == "acquisition" and row["state"] != "cancelled":
            self._refresh_job(job_id)
            row = self._job_row(job_id)
        result = dict(row)
        result["job_id"] = result.pop("id")
        result["payload"] = loads(result["payload"], {})
        result["progress"] = loads(result["progress"], {})
        if row["type"] == "acquisition":
            with self.store.connect() as db:
                tasks = [dict(r) for r in db.execute("SELECT t.* FROM tasks t JOIN job_tasks j ON t.id=j.task_id WHERE j.job_id=? ORDER BY t.id", (job_id,))]
            counts = {}
            for t in tasks:
                counts[t["state"]] = counts.get(t["state"], 0) + 1
                t["task_id"] = t.pop("id")
                t["target"] = loads(t["target"], {})
                t["result"] = loads(t["result"], {})
                t.pop("lease_token", None)
            result["task_count"] = counts
            result["paper_count"] = len({t["paper_id"] for t in tasks})
            filtered = [t for t in tasks if not kwargs.get("task_state") or t["state"] == kwargs["task_state"]]
            try:
                offset, limit = int(kwargs.get("cursor") or 0), int(kwargs.get("limit", 200))
                if offset < 0 or not 1 <= limit <= 1000:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ResearchError("INVALID_ARGUMENT", "Invalid job task pagination") from None
            result["tasks"] = filtered[offset:offset + limit]
            result["task_total"] = len(filtered)
            result["next_cursor"] = str(offset + limit) if offset + limit < len(filtered) else None
        return result

    def _save_progress(self, job_id, progress, state="running"):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET progress=?,state=?,updated_at=? WHERE id=? AND state<>'cancelled'", (dumps(progress), state, now(), job_id))

    def run_worker(self, job_id):
        with self.store.lock("worker", timeout=.2):
            row = self._job_row(job_id)
            if row["state"] in TERMINAL or row["state"] in ("partial", "paused"):
                return self.get_job(job_id)
            if row["type"] == "discovery":
                self._run_discovery(row)
            elif row["type"] == "frontier":
                self._run_frontier(row)
            elif row["type"] == "citations":
                self._run_citations(row)
            else:
                self._run_acquisition(row)
        return self.get_job(job_id)

    def _run_discovery(self, row):
        payload = loads(row["payload"])
        progress = loads(row["progress"], {})
        states = progress.setdefault("sources", {name: {"cursor": None, "complete": False, "pages": 0, "records": 0} for name in payload["sources"]})
        progress.setdefault("paper_ids", [])
        progress.setdefault("requests", 0)
        progress.setdefault("warnings", [])
        used = 0
        budget = payload["budget"].get("max_requests", 100)
        self._save_progress(row["id"], progress)
        while used < budget:
            advanced = False
            start_index = progress.get("next_source_index", 0) % len(payload["sources"])
            source_order = payload["sources"][start_index:] + payload["sources"][:start_index]
            for name in source_order:
                status = states[name]
                if status["complete"] or status.get("error"):
                    continue
                if self._job_row(row["id"])["state"] == "cancelled":
                    return
                advanced = True
                before = getattr(self.sources, "request_count", 0)
                try:
                    page = self.sources.search_page(name, payload["source_queries"].get(name, payload["query"]), cursor=status["cursor"], page_size=50,
                                                    date_from=payload["date_from"], date_to=payload["date_to"])
                    for candidate in page.items:
                        try:
                            pid = self._upsert_candidate(candidate)
                            with self.store.connect() as db:
                                db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?,'candidate')", (row["collection_id"], pid))
                                self._record_hit(db, row["id"], pid, "broad", candidate)
                            if pid not in progress["paper_ids"]:
                                progress["paper_ids"].append(pid)
                        except ResearchError as error:
                            progress["warnings"].append({"source": name, "code": error.code, "message": error.message})
                    status.update(cursor=page.next_cursor, complete=page.next_cursor is None, pages=status["pages"] + 1,
                                  records=status["records"] + len(page.items), total=page.total)
                    with self.store.connect() as db:
                        db.execute("UPDATE collections SET revision=revision+1 WHERE id=?", (row["collection_id"],))
                    progress["warnings"].extend({"source": name, "message": warning} for warning in page.warnings)
                except ResearchError as error:
                    status["error"] = error.to_dict()
                except Exception as error:
                    status["error"] = {"code": "SOURCE_UNAVAILABLE", "message": type(error).__name__, "retryable": True}
                calls = max(1, getattr(self.sources, "request_count", before + 1) - before)
                used += calls
                progress["requests"] += calls
                progress["requests_this_run"] = used
                progress["next_source_index"] = (payload["sources"].index(name) + 1) % len(payload["sources"])
                progress["budget_overshoot"] = max(0, used - budget)
                progress["budget_policy"] = "pause_between_source_pages"
                self._save_progress(row["id"], progress)
                if used >= budget:
                    break
            if not advanced:
                break
        all_complete = all(s["complete"] for s in states.values())
        pending = any(not s["complete"] and not s.get("error") for s in states.values())
        if all_complete:
            state = "succeeded"
            fp = self._plan_fingerprint(payload)
            with self.store.connect() as db:
                db.execute("INSERT INTO search_runs VALUES(?,?,?,?) ON CONFLICT(collection_id,fingerprint) DO UPDATE SET completed_at=excluded.completed_at,coverage_through=excluded.coverage_through",
                           (row["collection_id"], fp, now(), payload["date_to"] or now()[:10]))
                db.execute("UPDATE collections SET revision=revision+1 WHERE id=?", (row["collection_id"],))
        elif pending:
            state = "paused"
            progress["stop_reason"] = "budget_reached"
        else:
            state = "partial" if progress["paper_ids"] else "failed"
        self._save_progress(row["id"], progress, state)

    def acquire_papers(self, collection_id, paper_ids=None, request_id=None, **kwargs):
        if set(kwargs) - {"roles", "version_policy", "requested_versions"}:
            raise ResearchError("INVALID_ARGUMENT", "Unsupported acquisition option")
        defaults = loads(self._require_collection(collection_id)["config"], {}).get("acquisition", {})
        roles = kwargs.get("roles", defaults.get("roles", ["main_text"]))
        policy = kwargs.get("version_policy", defaults.get("version_policy", ["published", "accepted", "preprint"]))
        if roles != ["main_text"] or not policy or set(policy) - {"published", "accepted", "preprint", "unknown"}:
            raise ResearchError("INVALID_ARGUMENT", "Automatic acquisition supports main_text and known version policies")
        with self.store.connect() as db:
            included = {r[0] for r in db.execute("SELECT paper_id FROM memberships WHERE collection_id=? AND status='included'", (collection_id,))}
        ids = sorted(included if paper_ids is None else set(paper_ids))
        if set(ids) - included:
            raise ResearchError("INVALID_ARGUMENT", "Acquisition requires included collection members")
        if len(ids) > 10000:
            raise ResearchError("INVALID_ARGUMENT", "Split acquisition batches above 10000 targets")
        versions = kwargs.get("requested_versions") or {}
        for pid, vid in versions.items():
            with self.store.connect() as db:
                if pid not in ids or not db.execute("SELECT 1 FROM versions WHERE id=? AND paper_id=?", (vid, pid)).fetchone():
                    raise ResearchError("INVALID_ARGUMENT", "Requested version does not belong to target paper")
        payload = {"paper_ids": ids, "roles": roles, "version_policy": policy, "requested_versions": versions}
        jid, created = self._new_job("acquisition", collection_id, payload, request_id)
        if created:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for pid in ids:
                    target = {"role": "main_text", "requested_version_id": versions.get(pid), "version_policy": policy}
                    key = hashlib.sha256(dumps([pid, target]).encode()).hexdigest()
                    existing = db.execute("SELECT id FROM tasks WHERE target_key=? AND state NOT IN ('succeeded','failed','cancelled')", (key,)).fetchone()
                    tid = existing[0] if existing else uid("task")
                    if not existing:
                        db.execute("INSERT INTO tasks(id,target_key,paper_id,state,target,updated_at) VALUES(?,?,?,'queued',?,?)", (tid, key, pid, dumps(target), now()))
                    db.execute("INSERT OR IGNORE INTO job_tasks VALUES(?,?,1)", (jid, tid))
            for pid in ids:
                self._satisfy_tasks(pid)
        return self.get_job(jid)

    def _matching_material(self, db, paper_id, target):
        related = self._related_paper_ids(paper_id, db)
        rows = db.execute("SELECT m.*,v.kind FROM materials m LEFT JOIN versions v ON m.version_id=v.id WHERE m.paper_id IN (" + ",".join("?" for _ in related) + ") AND m.role=? AND m.status='available'", related + [target["role"]])
        for row in rows:
            if target.get("requested_version_id") and row["version_id"] != target["requested_version_id"]:
                continue
            if not target.get("requested_version_id") and (row["kind"] or "unknown") not in target["version_policy"]:
                continue
            if row["relative_path"] and (self.root / row["relative_path"]).is_file():
                return row["id"]
        return None

    def _satisfy_tasks(self, paper_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            related = self._related_paper_ids(paper_id, db)
            tasks = list(db.execute("SELECT * FROM tasks WHERE paper_id IN (" + ",".join("?" for _ in related) + ") AND state NOT IN ('succeeded','cancelled')", related))
            for task in tasks:
                if not db.execute("SELECT 1 FROM job_tasks WHERE task_id=? AND active=1", (task["id"],)).fetchone():
                    continue
                mid = self._matching_material(db, paper_id, loads(task["target"]))
                if mid:
                    db.execute("UPDATE tasks SET state='succeeded',result=?,lease_token=NULL,lease_expires=NULL,updated_at=? WHERE id=?",
                               (dumps(access_result(task["result"], {"material_id": mid})), now(), task["id"]))

    def _refresh_job(self, job_id):
        with self.store.connect() as db:
            states = [r[0] for r in db.execute("SELECT t.state FROM tasks t JOIN job_tasks j ON j.task_id=t.id WHERE j.job_id=? AND j.active=1", (job_id,))]
            current = db.execute("SELECT state,payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if current["state"] == "queued" and (all(s == "queued" for s in states)):
                if states or loads(current["payload"]).get("paper_ids"):
                    return
            if not states or all(s == "succeeded" for s in states):
                state = "succeeded"
            elif any(s in ("queued", "resolving", "downloading", "validating", "retry_wait") for s in states):
                state = "running"
            elif any(s in ("waiting_browser", "leased") for s in states):
                state = "waiting_external"
            elif any(s == "needs_user" for s in states):
                state = "needs_user"
            else:
                state = "partial" if "succeeded" in states else "failed"
            db.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=? AND state<>'cancelled'", (state, now(), job_id))

    def _run_acquisition(self, row):
        with self.store.connect() as db:
            tids = [r[0] for r in db.execute("SELECT task_id FROM job_tasks WHERE job_id=? AND active=1", (row["id"],))]
        for tid in tids:
            with self.store.connect() as db:
                task = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if self._job_row(row["id"])["state"] == "cancelled":
                break
            if task["state"] not in ("queued", "resolving", "downloading", "retry_wait", "validating"):
                continue
            if task["state"] == "validating":
                mid = loads(task["result"]).get("material_id")
                if mid:
                    self._validate_browser_material(tid, mid)
                continue
            self._satisfy_tasks(task["paper_id"])
            with self.store.connect() as db:
                if db.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "succeeded":
                    continue
                db.execute("UPDATE tasks SET state='resolving',updated_at=? WHERE id=?", (now(), tid))
            paper = self.get_paper(task["paper_id"])
            target = loads(task["target"])
            options = []
            ordered_versions = sorted(paper["versions"], key=lambda v: target["version_policy"].index(v["kind"]) if v["kind"] in target["version_policy"] else len(target["version_policy"]))
            for version in ordered_versions:
                if target["requested_version_id"] and version["version_id"] != target["requested_version_id"]:
                    continue
                if not target["requested_version_id"] and version["kind"] != "unknown" and version["kind"] not in target["version_policy"]:
                    continue
                for url in version["metadata"].get("fulltext_urls", []):
                    options.append((url, version["version_id"]))
            errors, obtained, pending_materials, tried = [], False, [], set()
            route_attempts = []
            route_phases = {url: "known_location" for url, _ in options}
            options = list(dict.fromkeys(options))
            index, resolver_stage = 0, 0
            resolver_phases = ["metadata", "fallback"]
            if loads(task["result"], {}).get("retry_access", {}).get("refresh_locations") is False:
                resolver_phases = []
            while True:
                if index >= len(options):
                    if resolver_stage >= len(resolver_phases):
                        break
                    extra_options, diagnostics = self._resolved_options(paper, target, phase=resolver_phases[resolver_stage])
                    self._satisfy_tasks(task["paper_id"])
                    with self.store.connect() as db:
                        if db.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "succeeded":
                            obtained = True
                            break
                    for candidate_url, _ in extra_options:
                        route_phases.setdefault(candidate_url, resolver_phases[resolver_stage])
                    errors.extend(diagnostics)
                    options.extend((url, vid) for url, vid in extra_options if url not in tried)
                    resolver_stage += 1
                    if index >= len(options):
                        continue
                url, vid = options[index]
                index += 1
                if url in tried:
                    continue
                tried.add(url)
                with self.store.connect() as db:
                    if db.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "cancelled":
                        break
                    db.execute("UPDATE tasks SET state='downloading',updated_at=? WHERE id=?", (now(), tid))
                directory = self.root / ".staging" / "http" / tid
                directory.mkdir(parents=True, exist_ok=True)
                file = directory / "article.pdf"
                try:
                    self._download_file(url, file)
                    with self.store.connect() as db:
                        owner = db.execute("SELECT paper_id FROM versions WHERE id=?", (vid,)).fetchone()[0]
                    if owner not in self._related_paper_ids(task["paper_id"]):
                        raise ResearchError("IDENTITY_CONFLICT", "Resolved version has no verified relationship to this target")
                    item = self.import_material(owner, path=file, version_id=vid)
                    route_attempts.append({"url": safe_url(url), "phase": route_phases.get(url), "outcome": item["status"]})
                    with self.store.connect() as db:
                        db.execute("UPDATE materials SET source_url=? WHERE id=?", (safe_url(url), item["material_id"]))
                        current = db.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0]
                        if current != "cancelled":
                            obtained = bool(self._matching_material(db, task["paper_id"], target))
                            state = "succeeded" if obtained else "resolving"
                            db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=?", (state, dumps({"material_id": item["material_id"], "identity_issues": item["details"].get("identity_issues", []), "route_attempts": route_attempts, "resolver_diagnostics": errors}), now(), tid))
                    if obtained:
                        break
                    pending_materials.append(item["material_id"])
                except ResearchError as error:
                    errors.append({"code": error.code, "message": error.message, "url": safe_url(url), "phase": route_phases.get(url)})
                    route_attempts.append({"url": safe_url(url), "phase": route_phases.get(url), "outcome": "failed", "code": error.code})
                finally:
                    file.unlink(missing_ok=True)
            if not obtained:
                with self.store.connect() as db:
                    result = {"open_access_errors": errors, "pending_material_ids": pending_materials, "route_attempts": route_attempts}
                    if pending_materials:
                        result["material_id"] = pending_materials[0]
                    state = "needs_user" if pending_materials else "waiting_browser"
                    db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=? AND state NOT IN ('succeeded','cancelled')", (state, dumps(result), now(), tid))

    def _resolved_options(self, paper, target, phase="metadata"):
        from .resolvers import resolve_locations
        from .materials import normalized_doi
        resolution_paper = paper
        if target.get("requested_version_id"):
            requested = next(v for v in paper["versions"] if v["version_id"] == target["requested_version_id"])
            resolution_paper = {**paper, **requested["metadata"], "versions": [requested]}
        locations, diagnostics = resolve_locations(self.client, resolution_paper, phase=phase)
        diagnostics = [{**item, "phase": phase} for item in diagnostics]
        for diagnostic in list(diagnostics):
            if diagnostic.get("outcome") == "explicit_version_relation":
                from .materials import normalized_doi
                pub_doi = normalized_doi(diagnostic.get("published_doi"))
                if pub_doi != normalized_doi(paper.get("doi")):
                    diagnostics.append({"resolver": "version_relation", "outcome": "publication_identity_mismatch"})
                    continue
                pre_doi = normalized_doi(diagnostic.get("preprint_doi"))
                if not pre_doi:
                    continue
                from .models import Candidate
                candidate = Candidate(source="biorxiv_relation", source_id=pre_doi, title=diagnostic.get("title") or paper["title"],
                                      doi=pre_doi, version_kind="preprint", landing_url=diagnostic.get("landing_url"),
                                      authors=paper.get("authors", []), extra={"published_doi": pub_doi})
                try:
                    self._upsert_candidate(candidate)
                except ResearchError as error:
                    diagnostics.append({"resolver": "version_relation", "outcome": error.code})
        options = []
        for location in locations:
            kind = location["version_kind"]
            doi = location.get("doi")
            ids = location.get("identifiers") or {}
            if target.get("requested_version_id"):
                requested = next(v for v in paper["versions"] if v["version_id"] == target["requested_version_id"])
                expected = requested["metadata"]
                exact_doi = doi and normalized_doi(doi) == normalized_doi(expected.get("doi"))
                exact_arxiv = ids.get("arxiv") and ids.get("arxiv") == expected.get("identifiers", {}).get("arxiv")
                if kind == requested["kind"] and (exact_doi or exact_arxiv):
                    options.append((location["url"], requested["version_id"]))
                    continue
            source_id = hashlib.sha256(dumps([paper["paper_id"], location["url"], kind, doi, ids]).encode()).hexdigest()
            with self.store.connect() as db:
                existing = db.execute("SELECT version_id FROM source_records WHERE source='fulltext_resolver' AND source_id=?", (source_id,)).fetchone()
                vid = existing[0] if existing else uid("ver")
                if not existing:
                    meta = {k: paper.get(k) for k in ("title", "authors", "year", "abstract", "landing_url")}
                    meta.update(source="fulltext_resolver", source_id=source_id, identifiers=ids, doi=doi or (resolution_paper.get("doi") if kind == "published" else None),
                                version_kind=kind, fulltext_urls=[location["url"]], links=[])
                    db.execute("INSERT INTO versions VALUES(?,?,?,?)", (vid, paper["paper_id"], kind, dumps(meta)))
                    db.execute("INSERT INTO source_records VALUES('fulltext_resolver',?,?,?,?,?)", (source_id, paper["paper_id"], vid, dumps(meta), now()))
            options.append((location["url"], vid))
        return options, diagnostics

    def _download_file(self, url, path):
        safe_url(url)
        params = {}
        if urlsplit(url).hostname == "content.openalex.org" and os.getenv("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        for attempt in range(3):
            try:
                with self.client.stream("GET", url, params=params) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 2:
                            raw = response.headers.get("Retry-After", "1")
                            try:
                                delay = min(5.0, max(0, float(raw)))
                            except ValueError:
                                delay = 1
                            time.sleep(delay)
                            continue
                    if response.status_code != 200:
                        raise ResearchError("FULLTEXT_UNAVAILABLE", f"Full-text server returned HTTP {response.status_code}")
                    length = response.headers.get("Content-Length")
                    if length and int(length) > self.max_file_bytes:
                        raise ResearchError("INVALID_FILE", "Download exceeds size limit")
                    size = 0
                    with path.open("wb") as handle:
                        for chunk in response.iter_bytes(65536):
                            size += len(chunk)
                            if size > self.max_file_bytes:
                                raise ResearchError("INVALID_FILE", "Download exceeds size limit")
                            handle.write(chunk)
                with path.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        raise ResearchError("INVALID_FILE", "Full-text endpoint did not return a PDF")
                return
            except (httpx.HTTPError, ValueError, OSError):
                if attempt == 2:
                    raise ResearchError("FULLTEXT_UNAVAILABLE", "Full-text download failed", retryable=True) from None
        raise ResearchError("FULLTEXT_UNAVAILABLE", "Full-text download exhausted retries")

    def cancel_job(self, job_id):
        row = self._job_row(job_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET state='cancelled',updated_at=? WHERE id=?", (now(), job_id))
            tids = [r[0] for r in db.execute("SELECT task_id FROM job_tasks WHERE job_id=?", (job_id,))]
            db.execute("UPDATE job_tasks SET active=0 WHERE job_id=?", (job_id,))
            for tid in tids:
                if not db.execute("SELECT 1 FROM job_tasks WHERE task_id=? AND active=1", (tid,)).fetchone():
                    db.execute("UPDATE tasks SET state='cancelled',lease_token=NULL,lease_expires=NULL WHERE id=? AND state NOT IN ('succeeded','failed')", (tid,))
        if row["type"] == "frontier":
            child = loads(row["progress"], {}).get("broad_job_id")
            if child:
                self.cancel_job(child)
        return self.get_job(job_id)

    def resume_job(self, job_id):
        row = self._job_row(job_id)
        if row["state"] == "succeeded":
            raise ResearchError("INVALID_ARGUMENT", "Completed jobs cannot refresh literature coverage; create a new discovery request")
        if row["state"] == "cancelled":
            raise ResearchError("INVALID_ARGUMENT", "Create a new request after cancellation")
        progress = loads(row["progress"], {})
        for name, status in progress.get("sources", {}).items():
            error = status.pop("error", None)
            if name == "crossref" and error and error.get("details", {}).get("http_status") == 400 and status.get("cursor"):
                status["cursor"] = None
                status["complete"] = False
                status["cursor_restarts"] = status.get("cursor_restarts", 0) + 1
                status["restart_reason"] = "provider_rejected_saved_cursor"
        progress.pop("stop_reason", None)
        if row["type"] == "acquisition":
            with self.store.connect() as db:
                db.execute("UPDATE tasks SET state='queued' WHERE id IN(SELECT task_id FROM job_tasks WHERE job_id=? AND active=1) AND state IN ('failed','needs_user')", (job_id,))
        if row["type"] == "frontier":
            for seed in progress.get("seeds", []):
                if seed.get("status") == "error":
                    seed["status"] = "retry"
            child_id = progress.get("broad_job_id")
            if child_id and self._job_row(child_id)["state"] in ("partial", "failed"):
                self.resume_job(child_id)
        if row["type"] == "citations":
            for stream in progress.get("streams", []):
                stream.pop("error", None)
        self._save_progress(job_id, progress, "queued")
        return self.get_job(job_id)

    def _expire_leases(self, db):
        db.execute("UPDATE tasks SET state='waiting_browser',lease_token=NULL,lease_expires=NULL,session_id=NULL WHERE state='leased' AND lease_expires<?", (time.time(),))

    def claim_browser_task(self, worker_id, session_id="default", capabilities=None, job_id=None):
        if not worker_id or "local_download" not in (capabilities or ["local_download"]):
            raise ResearchError("INVALID_ARGUMENT", "A worker ID and local_download capability are required")
        if job_id:
            self._job_row(job_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire_leases(db)
            if db.execute("SELECT 1 FROM tasks WHERE state='leased' AND session_id=?", (session_id,)).fetchone():
                raise ResearchError("BROWSER_SESSION_BUSY", "This browser session already has a task")
            where, params = (" AND j.job_id=?", [job_id]) if job_id else ("", [])
            task = db.execute("SELECT t.*,j.job_id FROM tasks t JOIN job_tasks j ON j.task_id=t.id WHERE t.state='waiting_browser' AND j.active=1" + where + " ORDER BY t.updated_at LIMIT 1", params).fetchone()
            if not task:
                return {"task": None}
            lease_id, token, expires = uid("lease"), secrets.token_urlsafe(32), time.time() + 600
            directory = self.root / ".staging" / "browser" / task["id"] / lease_id
            directory.mkdir(parents=True, exist_ok=True)
            db.execute("UPDATE tasks SET state='leased',lease_id=?,lease_token=?,lease_expires=?,session_id=?,updated_at=? WHERE id=?", (lease_id, token, expires, session_id, now(), task["id"]))
        paper = self.get_paper(task["paper_id"])
        from datetime import datetime, timezone
        return {"task_id": task["id"], "job_id": task["job_id"], "paper_id": task["paper_id"],
                "execution_policy": {"mode": "background", "foreground_allowed": False, "os_input_allowed": False,
                                     "user_tabs_allowed": False, "enforced_by": "trusted_browser_worker_adapter"},
                "target": {**loads(task["target"]), "accepted_formats": ["pdf"]},
                "expected_identity": {k: paper.get(k) for k in ("doi", "title", "authors", "year")},
                "landing_urls": list(dict.fromkeys(v["metadata"]["landing_url"] for v in paper["versions"] if v["metadata"].get("landing_url"))),
                "lease": {"lease_id": lease_id, "token": token, "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z")},
                "handoff": {"mode": "shared_local_filesystem", "download_dir": str(directory)}}

    def renew_browser_lease(self, task_id, lease_token):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._check_lease(task, lease_token)
            expires = time.time() + 600
            db.execute("UPDATE tasks SET lease_expires=? WHERE id=?", (expires, task_id))
        return {"task_id": task_id, "expires_at_epoch": expires}

    @staticmethod
    def _check_lease(task, token):
        if not task or task["state"] != "leased" or not task["lease_token"] or not secrets.compare_digest(task["lease_token"], token) or task["lease_expires"] < time.time():
            raise ResearchError("STALE_LEASE", "Browser lease is absent, expired or replaced")

    def submit_browser_result(self, task_id, lease_token, submission_id, result):
        fingerprint = hashlib.sha256(dumps(result).encode()).hexdigest()
        if not submission_id or result.get("outcome") not in ("downloaded", "retryable_error", "needs_user", "not_found"):
            raise ResearchError("INVALID_ARGUMENT", "Invalid browser submission")
        with self.store.connect() as db:
            old = db.execute("SELECT * FROM submissions WHERE task_id=? AND submission_id=?", (task_id, submission_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ResearchError("IDEMPOTENCY_CONFLICT", "Submission ID payload changed")
                return loads(old["response"])
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._check_lease(task, lease_token)
        outcome = result["outcome"]
        mid = None
        if outcome == "downloaded":
            import shutil
            if not isinstance(result.get("file"), dict) or result["file"].get("kind") != "local_path" or not isinstance(result["file"].get("path"), str):
                raise ResearchError("INVALID_ARGUMENT", "Only local_path handoff is implemented")
            path = Path(result["file"]["path"]).resolve()
            allowed = (self.root / ".staging" / "browser" / task_id / task["lease_id"]).resolve()
            if not path.is_relative_to(allowed):
                raise ResearchError("PATH_OUTSIDE_STAGING", "Copy the file into this lease's download directory")
            if not path.is_file() or path.suffix.lower() in (".part", ".crdownload", ".tmp"):
                raise ResearchError("FILE_NOT_READY", "Browser download is incomplete")
            if path.stat().st_size > self.max_file_bytes:
                raise ResearchError("INVALID_FILE", "Downloaded file exceeds size limit")
            mid = uid("mat")
            own = self.root / ".staging" / "accepted" / mid / "article.pdf"
            own.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, own)
        response = {"task_id": task_id, "submission_id": submission_id, "accepted": True,
                    "material_id": mid, "state": "validating" if mid else "needs_user" if outcome == "needs_user" else "failed" if outcome == "not_found" else "retry_wait"}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._check_lease(current, lease_token)
            old = db.execute("SELECT * FROM submissions WHERE task_id=? AND submission_id=?", (task_id, submission_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ResearchError("IDEMPOTENCY_CONFLICT", "Submission ID payload changed")
                return loads(old["response"])
            attempts = current["attempts"] + 1
            if outcome == "retryable_error" and attempts >= 3:
                response["state"] = "failed"
            if mid:
                target = loads(task["target"])
                # reported_version does not establish actual version identity.
                vid = target.get("requested_version_id")
                db.execute("INSERT INTO materials(id,paper_id,version_id,role,format,status,relative_path,source_url,created_at) VALUES(?,?,?,?,?,'staged',?,?,?)",
                           (mid, task["paper_id"], vid, target["role"], "pdf", str(own.relative_to(self.root)), safe_url(result.get("source_url")), now()))
            detail = access_result(current["result"], {"material_id": mid} if mid else {"outcome": outcome, "reason": str(result.get("reason", ""))[:1000]})
            db.execute("UPDATE tasks SET state=?,attempts=?,result=?,lease_token=NULL,lease_expires=NULL,updated_at=? WHERE id=?",
                       (response["state"], attempts, dumps(detail), now(), task_id))
            db.execute("INSERT INTO submissions VALUES(?,?,?,?)", (task_id, submission_id, fingerprint, dumps(response)))
        return response

    def _validate_browser_material(self, task_id, material_id):
        try:
            item = self._finalize_material(material_id)
            with self.store.connect() as db:
                task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if task["state"] in ("cancelled", "succeeded"):
                    return
                target = loads(task["target"])
                matched = self._matching_material(db, task["paper_id"], target)
                state = "succeeded" if matched else "needs_user"
                db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=?", (state, dumps(access_result(task["result"], {"material_id": material_id, "notice": "Confirm identity/version if required" if not matched else None})), now(), task_id))
        except ResearchError as error:
            with self.store.connect() as db:
                current = db.execute("SELECT attempts,result FROM tasks WHERE id=?", (task_id,)).fetchone()
                state = ("failed" if current[0] >= 3 else "waiting_browser") if error.code == "INVALID_FILE" else "needs_user"
                db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=? AND state<>'cancelled'", (state, dumps(access_result(current["result"], {"material_id": material_id, "code": error.code})), now(), task_id))

    def resume_browser_task(self, task_id, **kwargs):
        with self.store.connect() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ResearchError("NOT_FOUND", "Browser task does not exist")
            if task["state"] in ("cancelled", "succeeded", "leased", "validating"):
                raise ResearchError("INVALID_ARGUMENT", "Task cannot be resumed in its current state")
        self._satisfy_tasks(task["paper_id"])
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state='waiting_browser',updated_at=? WHERE id=? AND state<>'succeeded'", (now(), task_id))
        return {"task_id": task_id, "resumed": True}
