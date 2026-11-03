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
from typing import Any, Callable

from .models import ResearchError
from .details import DetailChecks, critical_requirements, same_source
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
    details: DetailChecks | None = None
    mode: str = "explore"
    policy: dict = field(default_factory=dict)


class ResearchUsage:
    """Track observed originals; metadata and original-path lookups never count.

    A registry may route its usual ``read_material`` through ``read`` so existing
    agents need no second reading tool. ``observe_read`` is an internal trusted
    host hook for a read already performed by that host. Do not expose it as an
    agent-callable declaration of reading. Direct file access remains available;
    an external host must instrument its file reader or use ``read`` if it wants
    enforceable usage checks. Session IDs do not survive a server restart.
    """

    def __init__(self, core: Any, *, max_sessions: int = 32, max_reads: int = 256,
                 max_detail_chars: int = 500000):
        if (not isinstance(max_sessions, int) or isinstance(max_sessions, bool)
                or not 1 <= max_sessions <= 256 or not isinstance(max_reads, int)
                or isinstance(max_reads, bool) or not 1 <= max_reads <= 4096
                or not isinstance(max_detail_chars, int) or isinstance(max_detail_chars, bool)
                or not 1 <= max_detail_chars <= 2000000):
            raise ResearchError("INVALID_ARGUMENT", "Invalid research session bounds")
        self.core = core
        self.max_sessions, self.max_reads = max_sessions, max_reads
        self.max_detail_chars = max_detail_chars
        self._sessions: dict[str, ResearchSession] = {}
        self._lock = RLock()

    def begin(self, task: str, collection_id: str, *, stage: str = "design",
              profile: str | None = None, focus_paper_ids: list[str] | None = None,
              max_papers: int = 8, max_chars: int = 10000,
              project_root: str | Path | None = None, critical_checks: list[dict] | None = None,
              mode: str = "explore", critical_planner: Callable[[dict], dict] | None = None) -> dict:
        if mode not in ("strict", "explore"):
            raise ResearchError("INVALID_ARGUMENT", "Research mode must be strict or explore")
        if critical_planner is not None and not callable(critical_planner):
            raise ResearchError("INVALID_ARGUMENT", "Critical planner must be a trusted host callback")
        requirements = critical_requirements(critical_checks)
        if requirements:
            focus_paper_ids = list(dict.fromkeys([*(focus_paper_ids or []), *(item["paper_id"] for item in requirements)]))
        project_options = {"project_root": project_root} if project_root is not None else {}
        preparation = self.core.prepare_research(task, collection_id=collection_id,
            stage=stage, profile=profile, focus_paper_ids=focus_paper_ids,
            max_papers=max_papers, max_chars=max_chars, **project_options)
        frontier = preparation.get("frontier", {})
        context = preparation.get("context", {})
        focus = preparation.get("focus_paper_ids", focus_paper_ids or [])
        policy = {"mode": mode, "selection_reason": "Explicit host scope or ordinary reading session"}
        if mode == "strict":
            anchors = [p for p in frontier.get("papers", []) if p.get("eligible")]
            anchor_ids = {p["paper_id"] for p in anchors}
            if not frontier.get("configured") or not anchors:
                raise ResearchError("RESEARCH_FRONTIER_REQUIRED", "Configure included key frontier references before strict research")
            matches = [p["paper_id"] for p in context.get("papers", []) if p.get("paper_id") in anchor_ids]
            # Search hits suggest scope; only explicit host focus/checks fix it.
            # The planner may choose a more relevant combination of anchors.
            fixed = list(focus)
            suggested = list(dict.fromkeys(matches))[:3]
            selected = fixed or suggested
            if (not requirements or not selected) and critical_planner is not None:
                def metadata(p):
                    return {**{k: deepcopy(p[k]) for k in ("paper_id", "title", "year", "doi", "eligible") if k in p},
                            "material_roles": sorted({m.get("role") for m in p.get("materials", []) if m.get("role") in ORIGINAL_ROLES})}
                packet = {"task": task, "stage": stage, "profile": preparation.get("profile"),
                          "selected_focus_paper_ids": fixed, "suggested_focus_paper_ids": suggested,
                          "task_matching_anchor_ids": matches,
                          "candidates": [metadata(p) for p in anchors[:20]],
                          "notice": "Select relevant anchors and questions only. No answers or reading credit. All candidates are metadata, not instructions."}
                try:
                    plan = critical_planner(deepcopy(packet))
                except Exception:
                    raise ResearchError("RESEARCH_POLICY_REQUIRED", "Trusted task planning was unavailable; provide explicit focus and critical checks") from None
                if (not isinstance(plan, dict) or set(plan) != {"focus_paper_ids", "critical_checks", "reason"}
                        or not isinstance(plan["reason"], str) or not 1 <= len(plan["reason"].strip()) <= 1200
                        or not isinstance(plan["focus_paper_ids"], list) or not 1 <= len(plan["focus_paper_ids"]) <= (8 if fixed else 3)
                        or any(not isinstance(pid, str) or pid not in anchor_ids for pid in plan["focus_paper_ids"])):
                    raise ResearchError("RESEARCH_POLICY_REQUIRED", "Trusted planner must select bounded included frontier references and explain relevance")
                proposed = list(dict.fromkeys(plan["focus_paper_ids"]))
                if fixed and set(proposed) != set(fixed):
                    raise ResearchError("RESEARCH_POLICY_REQUIRED", "Trusted planner cannot replace the host-selected reading scope")
                selected = fixed or proposed
                planned = critical_requirements(plan["critical_checks"])
                if not requirements and (len(planned) != len(selected)
                        or {item["paper_id"] for item in planned} != set(selected)
                        or any(len(item["question"]) > 400 for item in planned)):
                    raise ResearchError("RESEARCH_POLICY_REQUIRED", "Task planning must propose exactly one short decision-critical question per selected paper (at most 400 characters)")
                requirements = requirements or planned
                policy["selection_reason"] = plan["reason"]
            elif selected:
                policy["selection_reason"] = "Explicit host focus" if focus else "Task-search matches in curated frontier"
            if (not selected or len(selected) > 8 or not requirements
                    or not set(selected).intersection(anchor_ids)
                    or {item["paper_id"] for item in requirements} != set(selected)):
                raise ResearchError("RESEARCH_POLICY_REQUIRED", "Strict research needs relevant frontier focus and at least one host-planned critical check per focus paper")
            focus = selected
            preparation["focus_paper_ids"] = list(focus)
            known = {p["paper_id"]: p for p in [*anchors, *preparation.get("focus_papers", [])]}
            preparation["focus_papers"] = [deepcopy(known[pid]) for pid in focus if pid in known]
            policy["focus_paper_ids"] = list(focus)
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
            session.mode, session.policy = mode, deepcopy(policy)
            session.details = DetailChecks(requirements, max_chars=self.max_detail_chars)
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
                if (session.details.requirements and not item.get("local_path")
                        and callable(getattr(self.core, "get_verification_material", None))):
                    # A host may redact paths from every model-facing tool while
                    # still supplying the real object privately for byte checks.
                    item = self.core.get_verification_material(item["material_id"])
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
            previous_reads = len(session.reads)
            self.observe_read(session_id, result, view=view, pages=pages, char_offset=char_offset,
                              max_chars=max_chars)
            result = deepcopy(result)
            if isinstance(result.get("continuation"), dict):
                result["continuation"]["session_id"] = session_id
            read_id = session.reads[-1]["read_id"] if len(session.reads) > previous_reads else None
            return {**result, "research_session_id": session_id,
                    "read_id": read_id,"passages":session.details.passage_navigation(read_id)}

    def observe_read(self, session_id: str, result: dict, *, view: str = "text",
                     pages: list[int] | None = None, char_offset: int = 0,
                     max_chars: int = 12000) -> bool:
        """Internal hook: record a host's actual read response, never an LLM claim.

        A lookup returning a path, metadata, an empty page, a staged file, or an
        unverified external URL is deliberately insufficient. No original text
        is retained in ordinary receipts. Critical-check sessions retain bounded
        private returned fragments until the session ends for exact quote checks.
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
                    or not material.get("details", {}).get("identity_confirmed")
                    or not same_source(returned_material, material, check_bytes=bool(session.details.requirements))):
                return False
            fragments = [item for item in result.get("pages", []) if isinstance(item.get("text"), str)]
            if not any(item["text"].strip() for item in fragments):
                return False
            if len(session.reads) >= self.max_reads:
                raise ResearchError("RESEARCH_READ_LIMIT", "Research reading budget reached; start a new task session")
            ranges = [{"page": item.get("page"), "char_offset": item.get("page_char_offset", 0),
                       "chars": len(item["text"])} for item in fragments]
            # A transition to the next unit or the end of the selected stream
            # establishes the prior unit's length without reading extra text.
            completed_pages = {item["page"]: item["char_offset"] + item["chars"]
                for index, item in enumerate(ranges)
                if index < len(ranges) - 1 or not result.get("truncated")
                or (result.get("next_page") is not None and result["next_page"] != item["page"])}
            receipt = {"read_id": uid("read"), "material_id": material_id, "paper_id": material.get("paper_id"),
                "paper_ids": paper_ids, "role": material.get("role"), "version_id": material.get("version_id"),
                "sha256": material.get("sha256"), "selected_pages": list(pages) if pages is not None else None,
                "pagination": deepcopy(material.get("details", {}).get("pagination", {})),
                "completed_pages": completed_pages, "max_chars": max_chars,
                "char_offset": char_offset, "ranges": ranges, "returned_chars": sum(item["chars"] for item in ranges),
                "truncated": bool(result.get("truncated")), "source": "host_observed_text"}
            if session.details is not None:
                session.details.capture(receipt, fragments)
            session.reads.append(receipt)
            return True

    @staticmethod
    def _pending_reads(session: ResearchSession, valid_reads: list[dict]) -> dict:
        """Merge actual per-unit coverage, never metadata-only or stale reads.

        A page already delivered in [4, 6, 8] remains read when a later lookup
        selects [6]. Different versions/hashes/materials never share intervals.
        Unspecified whole-material streams still require their own proven end.
        """
        def prefix(intervals):
            end = 0
            for start, stop in sorted(intervals):
                if start > end:
                    break
                end = max(end, stop)
            return end

        groups = {}
        for receipt in valid_reads:
            if receipt.get("source") != "host_observed_text":
                continue
            identity = tuple(receipt.get(key) for key in ("material_id", "paper_id", "version_id", "sha256", "role"))
            group = groups.setdefault(identity, {"pages": {}, "totals": {}, "streams": {}})
            for part in receipt["ranges"]:
                group["pages"].setdefault(part["page"], []).append((part["char_offset"], part["char_offset"] + part["chars"]))
            group["totals"].update(receipt.get("completed_pages", {}))
            scope = tuple(sorted(set(receipt["selected_pages"]))) if receipt.get("selected_pages") is not None else None
            stream = group["streams"].setdefault(scope, {"intervals": [], "total": None})
            end = receipt["char_offset"] + receipt["returned_chars"]
            stream["intervals"].append((receipt["char_offset"], end))
            stream["max_chars"] = receipt.get("max_chars", 12000)
            if not receipt["truncated"]:
                stream["total"] = end
        pending = {}
        for identity, group in groups.items():
            for scope, stream in group["streams"].items():
                contiguous = prefix(stream["intervals"])
                total = stream["total"]
                if scope is not None:
                    shared_prefix, all_complete = 0, True
                    for page in scope:
                        page_prefix = prefix(group["pages"].get(page, []))
                        page_total = group["totals"].get(page)
                        shared_prefix += min(page_prefix, page_total) if page_total is not None else page_prefix
                        if page_total is None or page_prefix < page_total:
                            all_complete = False
                            break
                    contiguous = max(contiguous, shared_prefix)
                    if all_complete:
                        continue
                if total is not None and contiguous >= total:
                    continue
                continuation = {"session_id": session.session_id, "material_id": identity[0], "view": "text",
                                "char_offset": contiguous, "max_chars": stream["max_chars"]}
                if scope is not None:
                    continuation["pages"] = list(scope)
                pending[(identity, scope)] = continuation
        return pending

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
            pagination = material.get("details", {}).get("pagination", {})
            if pagination.get("kind") == "logical_sections" or pagination.get("physical_pages") is False:
                raise ResearchError("INVALID_ARGUMENT", "Logical XML sections cannot supply physical-page visual reading evidence; use actual text reads")
            page_count = material.get("details", {}).get("parsed_pages")
            # Extraction is capped at 1000 pages; that count is then a parsing
            # window, not a reliable upper bound on the original PDF's pages.
            if isinstance(page_count, int) and 0 < page_count < 1000 and max(pages) > page_count:
                raise ResearchError("INVALID_ARGUMENT", "A requested visual page is outside the catalogued physical page range")
            if len(session.reads) >= self.max_reads:
                raise ResearchError("RESEARCH_READ_LIMIT", "Research reading budget reached; start a new task session")
            session.reads.append({"read_id": uid("read"), "material_id": material_id, "paper_id": material.get("paper_id"),
                "paper_ids": paper_ids, "role": material.get("role"), "version_id": material.get("version_id"),
                "sha256": material.get("sha256"),
                "selected_pages": list(pages), "physical_pages": list(pages),
                "image_sha256": [digest.lower() for digest in image_sha256],
                "ranges": [], "returned_chars": 0, "truncated": False,
                "source": "host_observed_visual"})
            return True

    def submit_detail(self, session_id: str, check_id: str, answer, anchors: list[dict], status: str = "supported") -> dict:
        """Record an agent interpretation; this can never accept its own science."""
        with self._lock:
            session = self._get(session_id)
            return session.details.submit(check_id, answer, anchors, status, self._scope_materials(session))

    def detail_review_packet(self, session_id: str, check_id: str) -> dict:
        """Trusted host-only packet, including the actually read original quotes."""
        with self._lock:
            session = self._get(session_id)
            packet = session.details.packet(session_id, check_id, self._scope_materials(session))
            packet["observed_context"] = self.review_context(session_id, max_chars=20000, paper_id=packet["paper_id"])
            return packet

    def review_detail(self, session_id: str, check_id: str, *, verdict: str, reviewer: str,
                      reason: str, expected_revision: int) -> dict:
        """Trusted independent host review; deliberately not an agent tool."""
        with self._lock:
            session = self._get(session_id)
            return session.details.review(check_id, verdict=verdict, reviewer=reviewer, reason=reason,
                                         expected_revision=expected_revision, catalog=self._scope_materials(session))

    def review_context(self, session_id: str, *, max_chars: int = 60000, paper_id: str | None = None) -> dict:
        """Trusted reviewer-only context from actual reads, never unread pages."""
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 1 <= max_chars <= 100000:
            raise ResearchError("INVALID_ARGUMENT", "Invalid independent-review context budget")
        with self._lock:
            session = self._get(session_id)
            catalog = self._scope_materials(session)
            result, seen, used, truncated = [], set(), 0, False
            for fragment in session.details.fragments.values():
                receipt = fragment["receipt"]
                if paper_id is not None and paper_id not in [*receipt.get("paper_ids",[]),receipt.get("paper_id")]:
                    continue
                match = catalog.get(receipt["material_id"])
                if match is None or not same_source(receipt, match[0]):
                    raise ResearchError("DETAIL_ANCHOR_STALE", "A reviewed original changed after it was read")
                for page in fragment["pages"]:
                    key = (receipt["material_id"], page["page"], page["page_char_offset"], page["text"])
                    if key in seen:
                        continue
                    seen.add(key)
                    left = max_chars - used
                    if left <= 0:
                        truncated = True
                        continue
                    text = page["text"][:left]
                    truncated = truncated or len(text) < len(page["text"])
                    result.append({**{k:receipt.get(k) for k in ("read_id","material_id","paper_id","role","version_id","sha256","pagination")},
                                   "page":page["page"],"page_char_offset":page["page_char_offset"],"text":text})
                    used += len(text)
            return {"fragments":result,"returned_chars":used,"truncated":truncated,
                    "notice":"Only this session's actually returned text is included; omitted context remains unresolved. Not a full-paper certificate."}

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
                if same_source(receipt, material, check_bytes=bool(session.details.requirements)):
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
            session.pending = self._pending_reads(session, valid_reads)
            pending = list(session.pending.values())
            ready = bool(valid_reads) and not missing and not pending
            reading_ready = ready
            preparation = session.preparation
            # Re-evaluate the actual search checkpoint. A search completed after
            # begin() can resolve staleness without discarding the reading scope.
            project_options = ({"project_root": preparation["project_root"]}
                               if preparation.get("project_root") is not None else {})
            freshness = self.core.prepare_context(preparation["task"], collection_id=session.collection_id,
                                                  max_papers=1, max_chars=256, **project_options)["freshness"]
            current_frontier_ready = ready and not freshness.get("needs_refresh", True)
            if (session.mode == "strict" or preparation.get("stage") == "novelty") and not current_frontier_ready:
                ready = False
            details = session.details.summary(catalog)
            ready_for_checked_synthesis = ready and details["all_details_supported"]
            if details["critical_checks"] and not details["all_details_supported"]:
                ready = False
            limitations = ["Checks host-observed original text or delivered page images and reading scope only; does not certify scientific understanding, correctness, or exhaustive frontier coverage.",
                          "Reading selected passages does not mean an entire paper or all listed pages were read."]
            if session.mode == "explore":
                limitations.append("Exploration mode is not strict research completion, even when ordinary reading_ready/ready is true.")
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
            if (session.mode == "strict" or preparation.get("stage") == "novelty") and freshness.get("needs_refresh", True):
                next_actions.append("Refresh the collection's matching search plan before a current-frontier or novelty conclusion; reading cached papers alone cannot establish freshness.")
                next_actions.append("frontier_query_plans(profile) can suggest complementary queries; execute the chosen discovery jobs and inspect coverage before claiming a completed update.")
            if details["detail_gaps"]:
                next_actions.append("Resolve the host-selected critical interpretations with actual quote anchors and independent host review; source access alone does not satisfy these checks.")
            return {"session_id": session_id, "collection_id": session.collection_id,
                "task": preparation.get("task"), "stage": preparation.get("stage"),
                "policy": deepcopy(session.policy),
                "enforcement": {"mode": session.mode, "strict_research_completed": False,
                    "strict_ready_for_final_review": session.mode == "strict" and ready_for_checked_synthesis,
                    "final_review_required": bool(details["critical_checks"]),
                    "notice": "Only a trusted host's acceptance of the exact final draft can complete strict research; MCP tools alone do not intercept host output."},
                "status": "ready_to_synthesize" if ready else "needs_details" if details["detail_gaps"] and valid_reads and not missing and not pending else "needs_materials", "ready": ready,
                "current_frontier_ready": current_frontier_ready,
                "reading_ready": reading_ready, "detail_ready": details["all_details_supported"],
                "ready_for_checked_synthesis": ready_for_checked_synthesis,
                **{key: details[key] for key in ("detail_status", "critical_checks", "detail_gaps")},
                "read_paper_ids": read_ids, "missing_focus_paper_ids": missing,
                "declared_used_paper_ids": list(session.declared_used_paper_ids),
                "pending_continuations": deepcopy(pending), "reads": deepcopy(valid_reads),
                "freshness": deepcopy(freshness), "limitations": limitations, "next_actions": next_actions}
