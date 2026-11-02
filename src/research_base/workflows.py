"""Seed-aware search and bounded citation expansion, sharing the existing catalog."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date

from .materials import normalized_doi
from .models import Candidate, ResearchError
from .storage import dumps, loads, now
from .identity import observed_identifiers


def title_key(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


def seed_matches(seed, identity):
    """Identifier-bearing seeds require their identifiers, never just a similar title."""
    from .scholarly import seed_match
    ids = observed_identifiers(identity.get("identifiers"), doi=identity.get("doi"), landing_url=identity.get("landing_url"), fulltext_urls=identity.get("fulltext_urls"))
    observed = Candidate(source="observed_metadata", source_id="snapshot", title=identity.get("title") or "",
                         doi=identity.get("doi"), identifiers=ids,
                         authors=identity.get("authors") or [], year=identity.get("year"), extra=identity.get("extra") or {})
    return seed_match(observed, seed) is not None


class DiscoveryWorkflowsMixin:
    def _scholarly(self):
        if self._scholarly_client is None:
            from .scholarly import ScholarlyClient
            self._scholarly_client = ScholarlyClient(self.client)
        return self._scholarly_client

    @staticmethod
    def _record_hit(db, job_id, paper_id, stage, candidate):
        data = candidate.model_dump() if isinstance(candidate, Candidate) else candidate
        identity = {key: data.get(key) for key in ("title", "doi", "identifiers", "authors", "year", "landing_url", "fulltext_urls")}
        identity["extra"] = {"published_doi": (data.get("extra") or {}).get("published_doi")}
        db.execute("INSERT OR REPLACE INTO discovery_hits VALUES(?,?,?,?,?,?)",
                   (job_id, paper_id, stage, data["source"], data["source_id"], dumps(identity)))

    @staticmethod
    def _validate_budget(budget, *, default=24, allow_broad=False):
        budget = dict(budget or {"max_requests": default})
        allowed = {"max_requests", "broad_requests"} if allow_broad else {"max_requests"}
        if set(budget) - allowed:
            raise ResearchError("INVALID_ARGUMENT", "Unsupported request budget field")
        number = budget.get("max_requests", default)
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 10000:
            raise ResearchError("INVALID_ARGUMENT", "max_requests must be an integer in 1..10000")
        budget["max_requests"] = number
        if "broad_requests" in budget:
            broad = budget["broad_requests"]
            if isinstance(broad, bool) or not isinstance(broad, int) or not 1 <= broad <= number:
                raise ResearchError("INVALID_ARGUMENT", "broad_requests must fit within max_requests")
        return budget

    def discover_frontier(self, collection_id, query, seeds, sources=None, source_queries=None, date_from=None, date_to=None, budget=None, request_id=None):
        self._require_collection(collection_id)
        if not isinstance(query, str) or not query.strip():
            raise ResearchError("INVALID_ARGUMENT", "Search query is required")
        if not isinstance(seeds, list) or not 1 <= len(seeds) <= 50:
            raise ResearchError("INVALID_ARGUMENT", "Provide 1..50 seed records")
        allowed = {"seed_id", "title", "doi", "arxiv", "pmid", "authors", "year"}
        for seed in seeds:
            if not isinstance(seed, dict) or set(seed) - allowed or not any(seed.get(k) for k in ("title", "doi", "arxiv", "pmid")):
                raise ResearchError("INVALID_ARGUMENT", "Each seed needs a title or a supported identifier")
            for key in ("seed_id", "title", "doi", "arxiv", "pmid"):
                if key in seed and (not isinstance(seed[key], str) or not seed[key].strip() or len(seed[key]) > 2000):
                    raise ResearchError("INVALID_ARGUMENT", "Invalid seed text field")
            from .scholarly import normalized_seed
            normalized_seed(seed)
        available = {s["name"] for s in self.list_sources()}
        chosen = sorted(set(sources or available))
        if not chosen or set(chosen) - available:
            raise ResearchError("INVALID_ARGUMENT", "Unknown literature source")
        for value in (date_from, date_to):
            if value:
                try:
                    date.fromisoformat(value)
                except (ValueError, TypeError):
                    raise ResearchError("INVALID_ARGUMENT", "Dates must use YYYY-MM-DD") from None
        if date_from and date_to and date_from > date_to:
            raise ResearchError("INVALID_ARGUMENT", "Date range is reversed")
        budget = self._validate_budget(budget, allow_broad=True)
        payload = {"query": query.strip(), "seeds": seeds, "sources": chosen, "source_queries": source_queries or {},
                   "date_from": date_from, "date_to": date_to, "budget": budget}
        jid, _ = self._new_job("frontier", collection_id, payload, request_id)
        return self.get_job(jid)

    def _run_frontier(self, row):
        payload, progress = loads(row["payload"]), loads(row["progress"], {})
        total_budget = payload["budget"]["max_requests"]
        broad_budget = payload["budget"].get("broad_requests", max(1, total_budget - min(len(payload["seeds"]) * 3, total_budget // 2)))
        used = 0
        if not progress.get("broad_job_id"):
            broad = self.discover_papers(row["collection_id"], payload["query"], sources=payload["sources"], source_queries=payload["source_queries"],
                                         date_from=payload["date_from"], date_to=payload["date_to"], budget={"max_requests": broad_budget},
                                         request_id="frontier:" + row["id"] + ":broad")
            progress["broad_job_id"] = broad["job_id"]
            self._save_progress(row["id"], progress)
        broad_row = self._job_row(progress["broad_job_id"])
        # Reserve the first run's remaining budget for actual provider seed lookup.
        prioritize_seeds = bool(progress.get("broad_state")) and any(s.get("status") in ("pending", "retry") for s in progress.get("seeds", []))
        if not prioritize_seeds and broad_row["state"] not in ("succeeded", "partial", "failed", "cancelled"):
            if broad_row["state"] == "paused":
                self.resume_job(broad_row["id"])
                broad_row = self._job_row(broad_row["id"])
            before = loads(broad_row["progress"], {}).get("requests", 0)
            self._run_discovery(broad_row)
            broad = self.get_job(broad_row["id"])
            used += broad["progress"].get("requests", 0) - before
        else:
            broad = self.get_job(broad_row["id"])
        progress["broad_state"] = broad["state"]
        progress["broad_paper_count"] = len(broad["progress"].get("paper_ids", []))
        with self.store.connect() as db:
            hits = [(r["paper_id"], loads(r["identity"])) for r in db.execute("SELECT paper_id,identity FROM discovery_hits WHERE job_id=? AND stage='broad'", (broad["job_id"],))]
        statuses = progress.setdefault("seeds", [{"seed_index": i, "seed": seed, "status": "pending"} for i, seed in enumerate(payload["seeds"])])
        client = self._scholarly()
        for status in statuses:
            if self._job_row(row["id"])["state"] == "cancelled":
                return
            matched = list(dict.fromkeys(pid for pid, identity in hits if seed_matches(status["seed"], identity)))
            status["broad_paper_ids"] = matched
            if matched:
                status.update(status="found_broad", paper_ids=matched)
                continue
            if status["status"] not in ("pending", "retry"):
                continue
            if used >= total_budget:
                break
            before = getattr(client, "requests_count", 0)
            try:
                result = client.lookup(status["seed"])
                ids, verified = [], []
                for candidate in result.get("candidates", []):
                    candidate = Candidate.model_validate(candidate)
                    # Strong ID seeds never become hits solely through same-title fallback.
                    if not seed_matches(status["seed"], candidate.model_dump()):
                        continue
                    pid = self._upsert_candidate(candidate)
                    with self.store.connect() as db:
                        db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?,'candidate')", (row["collection_id"], pid))
                        self._record_hit(db, row["id"], pid, "seed", candidate)
                    ids.append(pid)
                    verified.append(candidate.source)
                attempts = result.get("attempts", [])
                uncertain = not ids and any(a.get("status") == "error" for a in attempts)
                status.update(status="found_lookup" if ids else "error" if uncertain else "not_found", paper_ids=list(dict.fromkeys(ids)),
                              providers=list(dict.fromkeys(verified)), attempts=result.get("attempts", []))
            except ResearchError as error:
                status.update(status="error", error=error.to_dict())
            calls = max(1, getattr(client, "requests_count", before + 1) - before)
            used += calls
            progress["seed_requests"] = progress.get("seed_requests", 0) + calls
            self._save_progress(row["id"], progress)
        progress["requests_this_run"] = used
        progress["requests"] = broad["progress"].get("requests", 0) + progress.get("seed_requests", 0)
        progress["budget_overshoot"] = max(0, used - total_budget)
        progress["broad_recall"] = {"found": sum(bool(s.get("broad_paper_ids")) for s in statuses), "total": len(statuses)}
        progress["seed_coverage"] = {"found": sum(s["status"] in ("found_broad", "found_lookup") for s in statuses), "total": len(statuses)}
        progress["paper_ids"] = list(dict.fromkeys(broad["progress"].get("paper_ids", []) + [pid for s in statuses for pid in s.get("paper_ids", [])]))
        if any(s["status"] in ("pending", "retry") for s in statuses) or broad["state"] in ("queued", "running", "paused"):
            state = "paused"
            progress["stop_reason"] = "budget_reached"
        elif broad["state"] == "succeeded" and progress["seed_coverage"]["found"] == len(statuses):
            state = "succeeded"
        else:
            state = "partial" if progress["paper_ids"] else "failed"
        self._save_progress(row["id"], progress, state)

    def expand_citations(self, collection_id, paper_ids, direction="citations", date_from=None, date_to=None, budget=None, request_id=None):
        self._require_collection(collection_id)
        if not isinstance(paper_ids, list) or not 1 <= len(paper_ids) <= 50 or direction not in ("citations", "references", "both"):
            raise ResearchError("INVALID_ARGUMENT", "Provide 1..50 paper IDs and a valid citation direction")
        for pid in paper_ids:
            self._require_paper(pid)
        for value in (date_from, date_to):
            if value:
                try:
                    date.fromisoformat(value)
                except (ValueError, TypeError):
                    raise ResearchError("INVALID_ARGUMENT", "Dates must use YYYY-MM-DD") from None
        if date_from and date_to and date_from > date_to:
            raise ResearchError("INVALID_ARGUMENT", "Date range is reversed")
        payload = {"paper_ids": list(dict.fromkeys(paper_ids)), "direction": direction, "date_from": date_from, "date_to": date_to,
                   "budget": self._validate_budget(budget, default=12)}
        jid, _ = self._new_job("citations", collection_id, payload, request_id)
        return self.get_job(jid)

    def _run_citations(self, row):
        payload, progress = loads(row["payload"]), loads(row["progress"], {})
        directions = ["citations", "references"] if payload["direction"] == "both" else [payload["direction"]]
        streams = progress.setdefault("streams", [{"paper_id": pid, "direction": direction, "cursor": None, "complete": False, "pages": 0}
                                                    for pid in payload["paper_ids"] for direction in directions])
        progress.setdefault("paper_ids", [])
        progress.setdefault("warnings", [])
        progress.setdefault("edges_added", 0)
        client, used = self._scholarly(), 0
        budget = payload["budget"]["max_requests"]
        while used < budget:
            advanced = False
            start = progress.get("next_stream", 0) % len(streams)
            for idx in list(range(start, len(streams))) + list(range(start)):
                stream = streams[idx]
                if stream["complete"] or stream.get("error"):
                    continue
                if self._job_row(row["id"])["state"] == "cancelled":
                    return
                advanced = True
                paper = self.get_paper(stream["paper_id"])
                for related_id in paper.get("related_paper_ids", []):
                    related = self.get_paper(related_id)
                    doi = related.get("doi") or ""
                    if related.get("identifiers", {}).get("pmid") and not doi.startswith(("10.1101/", "10.64898/")):
                        paper = related
                        stream["graph_anchor_paper_id"] = related_id
                        stream["anchor_basis"] = "explicit_preprint_publication_relation"
                        break
                seed = {k: paper.get(k) for k in ("title", "doi", "identifiers")}
                before = getattr(client, "requests_count", 0)
                try:
                    response = client.neighbors(seed, direction=stream["direction"], cursor=stream["cursor"], page_size=25,
                                                date_from=payload["date_from"], date_to=payload["date_to"])
                    page = response["page"]
                    stream["attempts"] = response.get("attempts", [])
                    stream["provider"] = response.get("provider", "unknown")
                    for candidate in page.items:
                        try:
                            pid = self._upsert_candidate(candidate)
                            if pid in self._related_paper_ids(stream["paper_id"]):
                                continue
                            citing, cited = (pid, stream["paper_id"]) if stream["direction"] == "citations" else (stream["paper_id"], pid)
                            with self.store.connect() as db:
                                db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?,'candidate')", (row["collection_id"], pid))
                                self._record_hit(db, row["id"], pid, stream["direction"], candidate)
                                inserted = db.execute("INSERT OR IGNORE INTO citation_edges VALUES(?,?,?,?)", (citing, cited, stream["provider"], now())).rowcount
                            progress["edges_added"] += inserted
                            if pid not in progress["paper_ids"]:
                                progress["paper_ids"].append(pid)
                        except ResearchError as error:
                            progress["warnings"].append({"paper_id": stream["paper_id"], "code": error.code, "message": error.message})
                    stream.update(cursor=page.next_cursor, complete=page.next_cursor is None, pages=stream["pages"] + 1, provider_total=page.total)
                    progress["warnings"].extend(page.warnings)
                except ResearchError as error:
                    stream["error"] = error.to_dict()
                calls = max(1, getattr(client, "requests_count", before + 1) - before)
                used += calls
                progress["requests"] = progress.get("requests", 0) + calls
                progress["next_stream"] = (idx + 1) % len(streams)
                progress["requests_this_run"] = used
                progress["budget_overshoot"] = max(0, used - budget)
                self._save_progress(row["id"], progress)
                if used >= budget:
                    break
            if not advanced:
                break
        if all(s["complete"] for s in streams):
            state = "partial" if progress["warnings"] else "succeeded"
        elif any(not s["complete"] and not s.get("error") for s in streams):
            state = "paused"
            progress["stop_reason"] = "budget_reached"
        else:
            state = "partial" if progress["paper_ids"] else "failed"
        progress["scope"] = "one_hop_per_requested_seed; candidate relevance still requires screening"
        self._save_progress(row["id"], progress, state)

    def retry_acquisition(self, job_id, refresh_locations=True):
        row = self._job_row(job_id)
        if row["type"] != "acquisition" or row["state"] == "cancelled":
            raise ResearchError("INVALID_ARGUMENT", "Select an active acquisition job")
        if not isinstance(refresh_locations, bool):
            raise ResearchError("INVALID_ARGUMENT", "refresh_locations must be boolean")
        retried, skipped = [], []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            tasks = list(db.execute("SELECT t.* FROM tasks t JOIN job_tasks j ON t.id=j.task_id WHERE j.job_id=? AND j.active=1", (job_id,)))
            for task in tasks:
                if task["state"] in ("succeeded", "leased", "validating", "needs_user", "cancelled"):
                    skipped.append({"task_id": task["id"], "state": task["state"]})
                    continue
                active = db.execute("SELECT id FROM tasks WHERE target_key=? AND id<>? AND state NOT IN ('succeeded','failed','cancelled')",
                                    (task["target_key"], task["id"])).fetchone()
                if active:
                    skipped.append({"task_id": task["id"], "state": task["state"], "reason": "active_target_exists", "active_task_id": active[0]})
                    continue
                previous = loads(task["result"], {})
                previous["retry_access"] = {"requested_at": now(), "refresh_locations": refresh_locations}
                db.execute("UPDATE tasks SET state='queued',result=?,updated_at=? WHERE id=?", (dumps(previous), now(), task["id"]))
                retried.append(task["id"])
            if retried:
                db.execute("UPDATE jobs SET state='queued',updated_at=? WHERE id=?", (now(), job_id))
        return {"job_id": job_id, "retried_task_ids": retried, "skipped": skipped, "refresh_locations": refresh_locations,
                "next_action": "run_worker" if retried else "no_unblocked_tasks"}
