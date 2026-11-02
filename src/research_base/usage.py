"""Bounded, host-observed use of original literature during a research task.

Sessions live in a tool connection, not in a permanent evidence-card database. A
successful check says that original text was returned for this task's scope. It
does not certify comprehension, scientific correctness, or complete coverage.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import re
from threading import RLock
from typing import Any

from .models import ResearchError
from .storage import uid


ORIGINAL_ROLES = frozenset(("main_text", "supplement", "code", "data", "model"))


@dataclass
class ResearchSession:
    session_id: str
    collection_id: str
    preparation: dict
    relevant_paper_ids: tuple[str, ...]
    focus_paper_ids: tuple[str, ...]
    declared_used_paper_ids: tuple[str, ...] = ()
    reads: list[dict] = field(default_factory=list)
    pending: dict[tuple, dict] = field(default_factory=dict)
    streams: dict[tuple, dict] = field(default_factory=dict)


class ResearchUsage:
    """Track observed originals; metadata and original-path lookups never count.

    A registry may route its usual ``read_material`` through ``read`` so existing
    agents need no second reading tool. ``observe_read`` is an internal trusted
    host hook for a read already performed by that host. Do not expose it as an
    agent-callable declaration of reading. Direct file access remains available;
    an external host must instrument its file reader or use ``read`` if it wants
    enforceable usage checks. Session IDs do not survive a server restart.
    """

    def __init__(self, core: Any, *, max_sessions: int = 32, max_reads: int = 256):
        if (not isinstance(max_sessions, int) or isinstance(max_sessions, bool)
                or not 1 <= max_sessions <= 256 or not isinstance(max_reads, int)
                or isinstance(max_reads, bool) or not 1 <= max_reads <= 4096):
            raise ResearchError("INVALID_ARGUMENT", "Invalid research session bounds")
        self.core = core
        self.max_sessions, self.max_reads = max_sessions, max_reads
        self._sessions: dict[str, ResearchSession] = {}
        self._lock = RLock()

    def begin(self, task: str, collection_id: str, *, stage: str = "design",
              profile: str | None = None, focus_paper_ids: list[str] | None = None,
              max_papers: int = 8, max_chars: int = 10000,
              project_root: str | Path | None = None) -> dict:
        project_options = {"project_root": project_root} if project_root is not None else {}
        preparation = self.core.prepare_research(task, collection_id=collection_id,
            stage=stage, profile=profile, focus_paper_ids=focus_paper_ids,
            max_papers=max_papers, max_chars=max_chars, **project_options)
        frontier = preparation.get("frontier", {})
        context = preparation.get("context", {})
        focus = preparation.get("focus_paper_ids", focus_paper_ids or [])
        relevant = list(dict.fromkeys([
            *(item["paper_id"] for item in context.get("papers", []) if item.get("paper_id")),
            *frontier.get("paper_ids", []),
            *(item["paper_id"] for item in frontier.get("papers", []) if item.get("paper_id")),
            *focus,
        ]))
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                # Never silently evict a live task and accidentally attribute its
                # later reads to another task. The host starts a fresh registry.
                raise ResearchError("RESEARCH_SESSION_LIMIT", "Research session limit reached; start a new tool connection")
            session = ResearchSession(uid("research"), collection_id, deepcopy(preparation),
                                      tuple(relevant), tuple(focus))
            self._sessions[session.session_id] = session
            return {"session_id": session.session_id, "preparation": deepcopy(preparation),
                    "usage": self.check(session.session_id)}

    def _get(self, session_id: str) -> ResearchSession:
        try:
            return self._sessions[session_id]
        except (KeyError, TypeError):
            raise ResearchError("RESEARCH_SESSION_NOT_FOUND", "Start a research session in this tool connection first") from None

    def extend(self, session_id: str, paper_ids: list[str]) -> dict:
        """Add task-relevant originals without erasing existing focus or reads."""
        if (not isinstance(paper_ids, list) or not 1 <= len(paper_ids) <= 20
                or any(not isinstance(pid, str) or not pid for pid in paper_ids)):
            raise ResearchError("INVALID_ARGUMENT", "Choose 1..20 included paper identifiers")
        with self._lock:
            session = self._get(session_id)
            requested = list(dict.fromkeys(paper_ids))
            additions = [pid for pid in requested if pid not in session.relevant_paper_ids]
            if len(session.relevant_paper_ids) + len(additions) > 200:
                raise ResearchError("RESEARCH_SCOPE_LIMIT", "Research task scope is limited to 200 papers")
            papers = []
            for paper_id in requested:
                paper = self.core.get_paper(paper_id)
                if not any(item.get("collection_id") == session.collection_id and item.get("status") == "included"
                           for item in paper.get("memberships", [])):
                    raise ResearchError("PAPER_OUTSIDE_RESEARCH_SCOPE", "Only included papers from this research collection may extend the task")
                papers.append({key: paper[key] for key in ("paper_id", "title", "materials", "related_paper_ids")})
            session.relevant_paper_ids = tuple([*session.relevant_paper_ids, *additions])
            return {"session_id": session_id, "added_paper_ids": additions, "papers": papers,
                    "focus_paper_ids": list(session.focus_paper_ids)}

    def end(self, session_id: str) -> dict:
        with self._lock:
            self._get(session_id)
            del self._sessions[session_id]
            return {"session_id": session_id, "status": "ended"}

    def _scope_materials(self, session: ResearchSession) -> dict[str, tuple[dict, list[str]]]:
        """Recheck live membership; verified edition links may share originals."""
        catalog = {}
        for paper_id in session.relevant_paper_ids:
            try:
                paper = self.core.get_paper(paper_id)
            except ResearchError as exc:
                if exc.code == "NOT_FOUND":
                    continue
                raise
            included = any(item.get("collection_id") == session.collection_id and item.get("status") == "included"
                           for item in paper.get("memberships", []))
            if not included:
                continue
            for item in paper.get("materials", []):
                material_id = item.get("material_id")
                if material_id in catalog:
                    catalog[material_id][1].append(paper_id)
                elif material_id:
                    catalog[material_id] = (item, [paper_id])
        return catalog

    def _scope_material(self, session: ResearchSession, material_id: str) -> tuple[dict, list[str]]:
        match = self._scope_materials(session).get(material_id)
        if match is None:
            raise ResearchError("MATERIAL_OUTSIDE_RESEARCH_SCOPE",
                "Choose an included task-relevant paper; begin a new session with focus_paper_ids to change scope")
        return match

    def read(self, session_id: str, material_id: str, *, view: str = "text",
             pages: list[int] | None = None, max_chars: int = 12000, char_offset: int = 0) -> dict:
        with self._lock:
            session = self._get(session_id)
            self._scope_material(session, material_id)
            if len(session.reads) >= self.max_reads:
                raise ResearchError("RESEARCH_READ_LIMIT", "Research reading budget reached; start a new task session")
            result = self.core.read_material(material_id, view=view, pages=pages,
                                             max_chars=max_chars, char_offset=char_offset)
            self.observe_read(session_id, result, view=view, pages=pages, char_offset=char_offset,
                              max_chars=max_chars)
            result = deepcopy(result)
            if isinstance(result.get("continuation"), dict):
                result["continuation"]["session_id"] = session_id
            return {**result, "research_session_id": session_id}

    def observe_read(self, session_id: str, result: dict, *, view: str = "text",
                     pages: list[int] | None = None, char_offset: int = 0,
                     max_chars: int = 12000) -> bool:
        """Internal hook: record a host's actual read response, never an LLM claim.

        A lookup returning a path, metadata, an empty page, a staged file, or an
        unverified external URL is deliberately insufficient. No original text
        is retained in these lightweight in-memory receipts.
        """
        with self._lock:
            session = self._get(session_id)
            returned_material = result.get("material", {})
            material_id = returned_material.get("material_id")
            if not material_id:
                return False
            material, paper_ids = self._scope_material(session, material_id)
            if (view != "text" or material.get("status") != "available"
                    or not material.get("exists") or material.get("role") not in ORIGINAL_ROLES
                    or not material.get("details", {}).get("identity_confirmed")):
                return False
            fragments = [item for item in result.get("pages", []) if isinstance(item.get("text"), str)]
            if not any(item["text"].strip() for item in fragments):
                return False
            if len(session.reads) >= self.max_reads:
                raise ResearchError("RESEARCH_READ_LIMIT", "Research reading budget reached; start a new task session")
            ranges = [{"page": item.get("page"), "char_offset": item.get("page_char_offset", 0),
                       "chars": len(item["text"])} for item in fragments]
            scope = tuple(sorted(set(pages))) if pages is not None else None
            key = (material_id, scope)
            stream = session.streams.setdefault(key, {"intervals": [], "total": None})
            end = char_offset + sum(item["chars"] for item in ranges)
            intervals = sorted([*stream["intervals"], (char_offset, end)])
            merged = []
            for start, stop in intervals:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
                else:
                    merged.append((start, stop))
            stream["intervals"] = merged
            if not result.get("truncated"):
                stream["total"] = end
            contiguous = merged[0][1] if merged and merged[0][0] == 0 else 0
            if stream["total"] is None or contiguous < stream["total"]:
                continuation = {"session_id": session_id, "material_id": material_id,
                    "view": "text", "char_offset": contiguous, "max_chars": max_chars}
                if pages is not None:
                    continuation["pages"] = list(pages)
                session.pending[key] = continuation
            else:
                session.pending.pop(key, None)
            session.reads.append({"material_id": material_id, "paper_id": material.get("paper_id"),
                "paper_ids": paper_ids, "role": material.get("role"), "version_id": material.get("version_id"),
                "selected_pages": list(pages) if pages is not None else None,
                "char_offset": char_offset, "ranges": ranges, "returned_chars": sum(item["chars"] for item in ranges),
                "truncated": bool(result.get("truncated")), "source": "host_observed_text"})
            return True

    def observe_visual_read(self, session_id: str, material_id: str, *, pages: list[int],
                            image_sha256: list[str]) -> bool:
        """Trusted host callback after original page images reach the model.

        The host must render these physical pages and deliver the corresponding
        images through its vision interface before calling this method. Hashes
        identify those delivered images; they are not proof of delivery or
        comprehension. Never expose this callback as an LLM self-report tool.
        Looking up an original path, taking an unseen screenshot, or rendering
        a page without delivering it to the model does not justify a callback.
        """
        if (not isinstance(pages, list) or not 1 <= len(pages) <= 100
                or any(not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 100000
                       for page in pages) or len(set(pages)) != len(pages)):
            raise ResearchError("INVALID_ARGUMENT", "Visual reading requires 1..100 distinct physical page numbers")
        if (not isinstance(image_sha256, list) or len(image_sha256) != len(pages)
                or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                       for digest in image_sha256)):
            raise ResearchError("INVALID_ARGUMENT", "Supply one SHA256 image digest per delivered physical page")
        with self._lock:
            session = self._get(session_id)
            material, paper_ids = self._scope_material(session, material_id)
            if (material.get("status") != "available" or not material.get("exists")
                    or material.get("role") not in ORIGINAL_ROLES
                    or not material.get("details", {}).get("identity_confirmed")):
                raise ResearchError("MATERIAL_NOT_VERIFIED", "Visual reading requires a confirmed available original material")
            page_count = material.get("details", {}).get("parsed_pages")
            # Extraction is capped at 1000 pages; that count is then a parsing
            # window, not a reliable upper bound on the original PDF's pages.
            if isinstance(page_count, int) and 0 < page_count < 1000 and max(pages) > page_count:
                raise ResearchError("INVALID_ARGUMENT", "A requested visual page is outside the catalogued physical page range")
            if len(session.reads) >= self.max_reads:
                raise ResearchError("RESEARCH_READ_LIMIT", "Research reading budget reached; start a new task session")
            session.reads.append({"material_id": material_id, "paper_id": material.get("paper_id"),
                "paper_ids": paper_ids, "role": material.get("role"), "version_id": material.get("version_id"),
                "selected_pages": list(pages), "physical_pages": list(pages),
                "image_sha256": [digest.lower() for digest in image_sha256],
                "ranges": [], "returned_chars": 0, "truncated": False,
                "source": "host_observed_visual"})
            return True

    def check(self, session_id: str, used_paper_ids: list[str] | None = None) -> dict:
        with self._lock:
            session = self._get(session_id)
            if (used_paper_ids is not None and (not isinstance(used_paper_ids, list)
                    or len(used_paper_ids) > 50 or any(not isinstance(pid, str) or not pid for pid in used_paper_ids))):
                raise ResearchError("INVALID_ARGUMENT", "used_paper_ids must contain up to 50 paper identifiers")
            declared = tuple(dict.fromkeys([*session.declared_used_paper_ids, *(used_paper_ids or [])]))
            if len(declared) > 50:
                raise ResearchError("RESEARCH_SCOPE_LIMIT", "A task may declare up to 50 supporting papers")
            session.declared_used_paper_ids = declared
            # Recheck live membership/materials so a later exclusion or rejected
            # original cannot satisfy a check using an old reading receipt.
            valid_reads = []
            catalog = self._scope_materials(session)
            for receipt in session.reads:
                match = catalog.get(receipt["material_id"])
                if match is None:
                    continue
                material, matching_ids = match
                if (material.get("status") == "available" and material.get("exists")
                        and material.get("role") in ORIGINAL_ROLES
                        and material.get("details", {}).get("identity_confirmed")):
                    valid_reads.append({**receipt, "paper_ids": matching_ids})
            # A verified linked edition may own the returned original while only
            # its preprint/publication counterpart has collection membership.
            # Credit the actual owner too: citing the version that was read must
            # not fail merely because its stable ID differs from the entry ID.
            read_ids = sorted({pid for receipt in valid_reads
                               for pid in [*receipt["paper_ids"], receipt.get("paper_id")]
                               if pid})
            required = list(dict.fromkeys([*session.focus_paper_ids, *session.declared_used_paper_ids]))
            missing = [pid for pid in required if pid not in read_ids]
            pending = list(session.pending.values())
            ready = bool(valid_reads) and not missing and not pending
            preparation = session.preparation
            # Re-evaluate the actual search checkpoint. A search completed after
            # begin() can resolve staleness without discarding the reading scope.
            project_options = ({"project_root": preparation["project_root"]}
                               if preparation.get("project_root") is not None else {})
            freshness = self.core.prepare_context(preparation["task"], collection_id=session.collection_id,
                                                  max_papers=1, max_chars=256, **project_options)["freshness"]
            current_frontier_ready = ready and not freshness.get("needs_refresh", True)
            if preparation.get("stage") == "novelty" and not current_frontier_ready:
                ready = False
            limitations = ["Checks host-observed original text or delivered page images and reading scope only; does not certify scientific understanding, correctness, or exhaustive frontier coverage.",
                          "Reading selected passages does not mean an entire paper or all listed pages were read."]
            if freshness.get("needs_refresh"):
                limitations.append("Literature freshness is unresolved; refresh the search before making current-frontier or novelty claims.")
            if preparation.get("warnings") or preparation.get("frontier", {}).get("gaps"):
                limitations.append("Navigation still contains coverage or material gaps; retain them in the research conclusion.")
            next_actions = []
            if not valid_reads:
                next_actions.append("Read task-relevant original text using read_material/read_research_material, or use a trusted host visual-reader callback after page images are delivered; paths and metadata alone do not count.")
            if missing:
                next_actions.append("Read originals for missing_focus_paper_ids, or start a separate task with an explicitly narrower scope.")
            if pending:
                next_actions.append("Replay each pending_continuation unchanged; offsets apply to the same page selection.")
            if preparation.get("stage") == "novelty" and freshness.get("needs_refresh", True):
                next_actions.append("Refresh the collection's matching search plan before a current-frontier or novelty conclusion; reading cached papers alone cannot establish freshness.")
            return {"session_id": session_id, "collection_id": session.collection_id,
                "task": preparation.get("task"), "stage": preparation.get("stage"),
                "status": "ready_to_synthesize" if ready else "needs_materials", "ready": ready,
                "current_frontier_ready": current_frontier_ready,
                "read_paper_ids": read_ids, "missing_focus_paper_ids": missing,
                "declared_used_paper_ids": list(session.declared_used_paper_ids),
                "pending_continuations": deepcopy(pending), "reads": deepcopy(valid_reads),
                "freshness": deepcopy(freshness), "limitations": limitations, "next_actions": next_actions}
