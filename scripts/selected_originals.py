"""Limit an external-model validation to explicitly selected original pages.

This adapter changes only the validation's read surface, never the shared
collection. Navigation contains metadata, and manuscript text is returned only
by an explicit read of an allowed material and physical-page selection.
"""
from copy import deepcopy

from research_base.models import ResearchError


class SelectedOriginals:
    def __init__(self, base, collection_id, targets):
        self.base = base
        self.collection_id = collection_id
        self.library_id = base.library_id
        self.allowed = {item["paper_id"] for item in targets}
        self.pages = {item["material_id"]: set(item["pages"]) for item in targets}

    def _collection(self, collection_id=None):
        if collection_id is not None and collection_id != self.collection_id:
            raise ResearchError("VALIDATION_SCOPE", "This validation is restricted to the selected research collection")

    def _paper(self, paper):
        keys = ("paper_id", "title", "authors", "year", "doi", "identifiers", "preferred_version_id",
                "memberships", "related_paper_ids", "fulltext_available", "text_searchable", "publication_history",
                "publication_history_truncated")
        result = {key: deepcopy(paper[key]) for key in keys if key in paper}
        result["materials"] = [self._material(item) for item in paper.get("materials", []) if item.get("material_id") in self.pages]
        result["versions"] = [{key: item.get(key) for key in ("version_id", "paper_id", "kind")}
                              for item in paper.get("versions", [])]
        return result

    @staticmethod
    def _material(item):
        keys = ("material_id", "paper_id", "version_id", "role", "format", "status", "parse_status",
                "exists", "details", "sha256", "source_url")
        return {key: deepcopy(item[key]) for key in keys if key in item}

    def _context(self, context):
        result = deepcopy(context)
        result["papers"] = [self._paper(paper) for paper in context.get("papers", []) if paper["paper_id"] in self.allowed]
        # Navigation never transmits original excerpts, even for selected papers.
        result["gaps"] = [gap for gap in context.get("gaps", []) if not gap.get("paper_id") or gap["paper_id"] in self.allowed]
        return result

    def _frontier(self, frontier):
        result = deepcopy(frontier)
        result["paper_ids"] = [pid for pid in frontier["paper_ids"] if pid in self.allowed]
        result["papers"] = [self._paper(paper) for paper in frontier["papers"] if paper["paper_id"] in self.allowed]
        result["gaps"] = [gap for gap in frontier.get("gaps", []) if not gap.get("paper_id") or gap["paper_id"] in self.allowed]
        return result

    def prepare_research(self, task, collection_id, **kwargs):
        self._collection(collection_id)
        if set(kwargs.get("focus_paper_ids") or []) - self.allowed:
            raise ResearchError("VALIDATION_SCOPE", "Only the selected papers are available for this validation")
        result = self.base.prepare_research(task, collection_id, **kwargs)
        result["context"] = self._context(result["context"])
        result["frontier"] = self._frontier(result["frontier"])
        result["focus_papers"] = [self._paper(paper) for paper in result["focus_papers"]]
        result["warnings"].append("External-model validation is restricted to the selected papers and physical pages; other collection originals are unavailable in this run.")
        return result

    def prepare_context(self, task, collection_id=None, **kwargs):
        self._collection(collection_id)
        return self._context(self.base.prepare_context(task, collection_id=self.collection_id, **kwargs))

    def get_frontier(self, collection_id):
        self._collection(collection_id)
        return self._frontier(self.base.get_frontier(collection_id))

    def get_paper(self, paper_id):
        if paper_id not in self.allowed:
            raise ResearchError("NOT_FOUND", "Paper is outside this validation's selected originals")
        return self._paper(self.base.get_paper(paper_id))

    def read_material(self, material_id, *, pages=None, **kwargs):
        if (material_id not in self.pages or not isinstance(pages, list) or not pages
                or any(not isinstance(page, int) or isinstance(page, bool) for page in pages)
                or set(pages) - self.pages[material_id]):
            raise ResearchError("VALIDATION_SCOPE", "Select explicit permitted physical pages from the two approved original materials")
        result = self.base.read_material(material_id, pages=pages, **kwargs)
        result["material"] = self._material(result["material"])
        return result

    def list_materials(self, paper_id=None, collection_id=None, include_paths=True):
        self._collection(collection_id)
        if paper_id is not None and paper_id not in self.allowed:
            raise ResearchError("NOT_FOUND", "Paper is outside this validation's selected originals")
        return [self._material(item) for item in self.base.list_materials(paper_id=paper_id,
            collection_id=None if paper_id else self.collection_id, include_paths=include_paths)
            if item["material_id"] in self.pages]

    def list_papers(self, collection_id=None, membership="included", limit=100, offset=0, tag=None):
        self._collection(collection_id)
        papers = [self.get_paper(pid) for pid in sorted(self.allowed)] if membership in ("included", "all") else []
        if tag is not None:
            papers = [paper for paper in papers if tag in self.base.get_paper(paper["paper_id"]).get("tags", [])]
        return {"papers": papers[offset:offset + limit], "total": len(papers), "next_offset": None,
                "scope": "explicitly_selected_validation_originals"}

    def search_library(self, query, collection_id=None, **kwargs):
        self._collection(collection_id)
        result = self.base.search_library(query, collection_id=self.collection_id, **kwargs)
        papers = [self._paper(paper) for paper in result["papers"] if paper["paper_id"] in self.allowed]
        return {"papers": papers, "total": len(papers), "next_offset": None,
                "scope": "selected_metadata_in_returned_search_window"}

    def list_collections(self):
        collection = self.base.get_collection(self.collection_id)
        return [{"collection_id": self.collection_id, "name": collection["name"]}]

    def list_sources(self):
        return self.base.list_sources()

    def get_verification_material(self, material_id):
        """Trusted host-only source check; never part of model tool specs."""
        if material_id not in self.pages:
            raise ResearchError("VALIDATION_SCOPE", "Only selected originals can be verified")
        return next(item for item in self.base.list_materials() if item["material_id"] == material_id)
