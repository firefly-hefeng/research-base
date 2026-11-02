"""A small frontier reference space and discipline-aware research navigation.

The stored knowledge remains the papers and their original materials. This module
stores only a curated set of entry IDs and a profile, never generated paper cards.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .models import ResearchError
from .storage import now


PROFILES = ("general", "machine_learning", "single_cell")
STAGES = ("explore", "design", "implement", "interpret", "novelty")

_STAGE_QUESTIONS = {
    "explore": [
        "What question, study objects, conditions and scope do the closest studies actually address?",
        "Which established findings, competing approaches and unresolved limitations matter for this task?",
    ],
    "design": [
        "Which assumptions and experimental units make the reference methods applicable here?",
        "Which baselines, controls, data separation, metrics and uncertainty analyses are needed for a fair comparison?",
        "Which original methods, experimental settings and supplements could change the proposed design?",
    ],
    "implement": [
        "Which exact paper, code and data versions, preprocessing, parameters and evaluation settings are being implemented?",
        "Which details require inspecting code or supplements, and which remain unspecified or inaccessible?",
        "How will a minimal reproduction or sanity check establish that the reference procedure was implemented correctly?",
    ],
    "interpret": [
        "Are effect sizes, variability, controls and the unit of evaluation comparable with the reference studies?",
        "Could leakage, confounding, measurement choices or a changed setting explain the result?",
        "Which claims are supported by the actual observed results, and which remain hypotheses or extrapolations?",
        "Is the guarantee about an expectation, an average, a risk rate or every individual trial? Do not silently strengthen its level.",
    ],
    "novelty": [
        "How does the proposed contribution differ from the closest methods in assumptions, setting, mechanism and evidence?",
        "Were relevant recent work, foundational work and contradictory findings considered within the stated search scope?",
        "What remains unsearched or unread, preventing an exhaustive priority or state-of-the-art claim?",
    ],
}

_PROFILE_QUESTIONS = {
    "general": [
        "Choose the discipline's appropriate primary evidence and methods; publication date or venue alone does not establish quality.",
        "Resolve definitions continuing onto adjacent pages; completed selected ranges do not establish a complete concept. Library version labels are not independent evidence of peer review.",
    ],
    "machine_learning": [
        "Check dataset provenance, preprocessing and train/validation/test separation at the appropriate independent unit; keep tuning separate from final evaluation.",
        "Inspect strong simple baselines, metric definitions, hyperparameter/search budgets, code versions and variability before transferring published performance claims.",
        "For retrieval or generation, distinguish retrieval relevance, answer correctness and claim support; inspect judge/calibration protocols and synthetic-data overlap where applicable.",
        "For each decision-critical metric identify numerator, denominator, positive class, evaluation unit, conditions and aggregation; a correct name or quote alone is insufficient.",
        "A per-instance conjunction of binary indicators equals their product; its mean generally differs from multiplying marginal accuracies. Keep instance and aggregate definitions separate.",
    ],
    "single_cell": [
        "Distinguish cells from independent biological replicates, donors and perturbations; inspect batch, cell-type and control construction.",
        "For perturbation prediction, inspect held-out perturbation/context/donor splits, preprocessing leakage, matched simple baselines and the exact aggregation and differential-expression criteria.",
        "Inspect control-noise sensitivity, uncertainty across biological units and whether validation supports the intended biological generalization rather than only an in-distribution score.",
        "Distinguish feasible prediction baselines from evaluation-only positive controls that access held-out responses; keep such controls out of model fitting, tuning and deployment claims.",
        "Check the exact metric's noise assumptions before transferring a correction; a nonzero null estimate or a change in rankings requires uncertainty and alternative explanations, not an automatic leakage diagnosis.",
        "Configuration declarations are not an audit of executed splits; separate manuscript claims, pinned code behavior and observed execution. Two control arrays do not prove independent biological sampling.",
        "Declare the calibration-response budget before calling a setup zero-shot or few-shot. A positive mean gain does not imply protection in every released trial; report harmful-release risk and uncertainty separately.",
    ],
}


class ResearchSpaceMixin:
    """Task navigation layered on the existing collection and original-material API."""

    @staticmethod
    def _research_ids(paper_ids, *, maximum=20):
        if (not isinstance(paper_ids, list) or len(paper_ids) > maximum
                or any(not isinstance(pid, str) or not pid.strip() or len(pid) > 300 for pid in paper_ids)):
            raise ResearchError("INVALID_ARGUMENT", f"paper_ids must contain at most {maximum} nonempty identifiers")
        return list(dict.fromkeys(paper_ids))

    @staticmethod
    def _frontier_config(collection):
        config = collection.get("config", {})
        if not isinstance(config, dict):
            raise ResearchError("INVALID_ARGUMENT", "Collection configuration must be an object")
        frontier = config.get("frontier", {})
        if not isinstance(frontier, dict):
            raise ResearchError("INVALID_ARGUMENT", "frontier configuration must be an object")
        profile = frontier.get("profile", "general")
        if profile not in PROFILES:
            raise ResearchError("INVALID_ARGUMENT", "Unknown research discipline profile")
        ids = ResearchSpaceMixin._research_ids(frontier.get("paper_ids", []))
        return frontier, profile, ids

    def _included_research_ids(self, collection_id, paper_ids):
        with self.store.connect() as db:
            for pid in paper_ids:
                row = db.execute("SELECT status FROM memberships WHERE collection_id=? AND paper_id=?", (collection_id, pid)).fetchone()
                if row is None or row[0] != "included":
                    raise ResearchError("INVALID_ARGUMENT", "Research focus and frontier entries must be included papers in this collection",
                                        details={"paper_id": pid, "collection_id": collection_id})

    @staticmethod
    def _research_paper(paper, membership):
        # Keep the entry light; callers can list all materials or fetch full
        # metadata. Original paths remain first-class and are not summarized away.
        material_keys = ("material_id", "paper_id", "version_id", "role", "format", "status", "parse_status", "exists",
                         "local_path", "text_path", "pages_path", "source_url", "sha256")
        materials = paper["materials"]
        return {"paper_id": paper["paper_id"], "title": paper.get("title", "")[:2000],
                "authors": paper.get("authors", [])[:20], "year": paper.get("year"), "doi": paper.get("doi"),
                "preferred_version_id": paper.get("preferred_version_id"),
                "publication_history": paper.get("publication_history", []),
                "publication_history_truncated": paper.get("publication_history_truncated", False),
                "versions": [{key: version.get(key) for key in ("version_id", "paper_id", "kind")} for version in paper.get("versions", [])[:32]],
                "versions_truncated": len(paper.get("versions", [])) > 32,
                "related_paper_ids": paper.get("related_paper_ids", []), "membership": membership,
                "eligible": membership == "included", "fulltext_available": paper["fulltext_available"],
                "text_searchable": paper["text_searchable"],
                "materials": [{key: material.get(key) for key in material_keys} for material in materials[:32]],
                "materials_truncated": len(materials) > 32,
                "more_materials": {"tool": "list_materials", "arguments": {"paper_id": paper["paper_id"]}} if len(materials) > 32 else None}

    def _frontier_space(self, collection, freshness):
        config, profile, ids = self._frontier_config(collection)
        papers, gaps, warnings = [], [], []
        with self.store.connect() as db:
            memberships = {row[0]: row[1] for row in db.execute("SELECT paper_id,status FROM memberships WHERE collection_id=?", (collection["collection_id"],))}
        if not ids:
            warnings.append("No curated frontier entries are configured; task search is available but no key-paper set has been established.")
        for pid in ids:
            membership = memberships.get(pid, "absent")
            try:
                paper = self.get_paper(pid)
            except ResearchError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                papers.append({"paper_id": pid, "membership": membership, "eligible": False, "missing": True, "materials": []})
                gaps.append({"paper_id": pid, "reason": "paper_missing"})
                continue
            papers.append(self._research_paper(paper, membership))
            if membership != "included":
                gaps.append({"paper_id": pid, "reason": "frontier_not_included", "membership": membership})
            if not paper["fulltext_available"]:
                gaps.append({"paper_id": pid, "reason": "fulltext_unavailable"})
            elif not paper["text_searchable"]:
                gaps.append({"paper_id": pid, "reason": "text_unavailable_use_original"})
        if freshness["needs_refresh"]:
            warnings.append("The completed search is missing, stale or outside the current scope; curated references do not certify current or exhaustive coverage.")
        return {"collection_id": collection["collection_id"], "collection_revision": collection["revision"],
                "configured": bool(ids), "profile": profile, "configured_at": config.get("configured_at"),
                "paper_ids": ids, "papers": papers, "freshness": deepcopy(freshness), "gaps": gaps, "warnings": warnings}

    def configure_frontier(self, collection_id, paper_ids, profile="general"):
        """Explicitly curate entry points without replacing materials or membership."""
        collection = self.get_collection(collection_id)
        self._frontier_config(collection)
        ids = self._research_ids(paper_ids)
        if profile not in PROFILES:
            raise ResearchError("INVALID_ARGUMENT", "Unknown research discipline profile")
        self._included_research_ids(collection_id, ids)
        config = deepcopy(collection["config"])
        existing = config.get("frontier", {})
        if existing.get("paper_ids") != ids or existing.get("profile", "general") != profile:
            config["frontier"] = {"paper_ids": ids, "profile": profile, "configured_at": now()}
            self.update_collection(collection_id, config=config)
        return self.get_frontier(collection_id)

    def get_frontier(self, collection_id):
        """Get curated original-material navigation, with gaps still visible."""
        collection = self.get_collection(collection_id)
        self._frontier_config(collection)
        freshness = self.prepare_context("frontier", collection_id=collection_id, max_papers=1, max_chars=256)["freshness"]
        return self._frontier_space(collection, freshness)

    def prepare_research(self, task, collection_id, stage="design", profile=None,
                         focus_paper_ids=None, max_papers=8, max_chars=10000, project_root=None):
        """Prepare task/stage guidance; never starts discovery or fabricates reads."""
        if not isinstance(task, str) or not task.strip() or len(task) > 12000:
            raise ResearchError("INVALID_ARGUMENT", "A nonempty research task up to 12000 characters is required")
        if stage not in STAGES:
            raise ResearchError("INVALID_ARGUMENT", "Unknown research stage")
        if (not isinstance(max_papers, int) or isinstance(max_papers, bool) or not 1 <= max_papers <= 50
                or not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 256 <= max_chars <= 100000):
            raise ResearchError("INVALID_ARGUMENT", "Invalid research navigation budget")
        collection = self.get_collection(collection_id)
        _, default_profile, _ = self._frontier_config(collection)
        profile = default_profile if profile is None else profile
        if profile not in PROFILES:
            raise ResearchError("INVALID_ARGUMENT", "Unknown research discipline profile")
        focus = self._research_ids([] if focus_paper_ids is None else focus_paper_ids)
        self._included_research_ids(collection_id, focus)
        if project_root is not None:
            if not isinstance(project_root, (str, Path)) or not str(project_root).strip():
                raise ResearchError("INVALID_ARGUMENT", "project_root must identify the bound research project")
            project_root = str(Path(project_root).expanduser().resolve())
        # Project-local discovery/freshness defaults must survive the higher-level
        # research interface. prepare_context also checks collection/library binding.
        context = self.prepare_context(task, collection_id=collection_id, project_root=project_root,
                                       max_papers=max_papers, max_chars=max_chars)
        frontier = self._frontier_space(collection, context["freshness"])
        focus_papers = [self._research_paper(self.get_paper(pid), "included") for pid in focus]
        return {"task": task, "stage": stage, "profile": profile, "collection_id": collection_id,
                **({"project_root": project_root} if project_root is not None else {}),
                "collection_revision": collection["revision"], "generated_at": now(), "frontier": frontier,
                "context": context, "focus_paper_ids": focus, "focus_papers": focus_papers,
                "reading_questions": list(_STAGE_QUESTIONS[stage]) + list(_PROFILE_QUESTIONS[profile]),
                "warnings": list(frontier["warnings"]),
                "access": {"paths": "Same-host agents may freely open the returned original/text/pages paths. Remote agents should use read_material; export a bundle when changing hosts.",
                           "usage": "Navigation and excerpts do not prove full-paper reading. Inspect the sections, code and supplements needed for the present decision; report missing or truncated material.",
                           "scope": "Curated entries are task entry points, not a mandatory quota or a declaration of scientific correctness. Excluded or missing entries are gaps, not eligible supporting evidence."}}
