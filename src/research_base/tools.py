"""One validated tool surface for MCP, external agents and DeepSeek."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any

from .models import ResearchError, success
from .metric_reference import metric_reference


def string(description: str = "", **kwargs: Any) -> dict:
    return {"type": "string", "description": description, **kwargs}


def integer(minimum: int = 0, maximum: int = 1000) -> dict:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def array(items: dict, maximum: int = 500) -> dict:
    return {"type": "array", "items": items, "maxItems": maximum}


def object_schema(properties: dict | None = None, required: list[str] | None = None, **extra: Any) -> dict:
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False, **extra}


def _definition(name: str, description: str, properties: dict, required: list[str] = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": object_schema(properties, list(required))}}


ID = string("Stable library identifier", minLength=1, maxLength=300)
QUERY = string("Research question or search query", minLength=1, maxLength=12000)
IDS = array(ID)
SEED = object_schema({
    "seed_id": ID,
    "title": string("Known paper title, used only to verify provider results", minLength=1, maxLength=2000),
    "doi": string(minLength=1, maxLength=300),
    "arxiv": string(minLength=1, maxLength=100),
    "pmid": string(minLength=1, maxLength=40),
    "authors": array(string(minLength=1, maxLength=300), 100),
    "year": integer(1800, 2100),
}, anyOf=[{"type": "object", "required": [field]} for field in ("title", "doi", "arxiv", "pmid")])
SEEDS = {**array(SEED, 50), "minItems": 1}
RESEARCH_STAGE = string(enum=["explore", "design", "implement", "interpret", "novelty"])
RESEARCH_PROFILE = string(enum=["general", "machine_learning", "single_cell"])
RESEARCH_ARGUMENTS = {"task": QUERY, "collection_id": ID, "stage": RESEARCH_STAGE,
    "profile": RESEARCH_PROFILE, "focus_paper_ids": array(ID, 20),
    "project_root": string(maxLength=4096),
    "max_papers": integer(1, 50), "max_chars": integer(256, 100000)}
CRITICAL_CHECK = object_schema({"check_id": ID, "paper_id": ID,
    "kind": string(minLength=1, maxLength=100), "question": string(minLength=1, maxLength=2000),
    "required_role": string(enum=["main_text", "supplement", "code", "data", "model"])},
    ["check_id", "paper_id", "kind", "question"])
DETAIL_ANSWER = {"anyOf": [string(minLength=1, maxLength=6000),
    object_schema({field: string(minLength=1, maxLength=2000)
                   for field in ("summary", "numerator", "denominator", "positive_class", "evaluation_unit", "conditions", "aggregation")},
                  ["summary"])]}
DETAIL_ANCHOR = {"anyOf":[object_schema({"passage_id":ID},["passage_id"]),
    object_schema({"read_id":ID,"quote":string(minLength=1,maxLength=2000)},["read_id","quote"])]}
READ_TOOLS = [
    _definition("metric_reference", "Check mathematical classification or joint-metric definitions using explicitly supplied labels or boolean trials. Does not establish which labels or conventions a paper uses and gives no original-reading credit.",
        {"kind":string(enum=["binary_classification","joint_binary"]),
         "positive_label":string(minLength=1,maxLength=200),"negative_label":string(minLength=1,maxLength=200),
         "predicted_positive_action":string(minLength=1,maxLength=200),"predicted_negative_action":string(minLength=1,maxLength=200),
         "trials":array(object_schema({"answer_correct":{"type":"boolean"},"evidence_correct":{"type":"boolean"}},["answer_correct","evidence_correct"]),1000)}),
    _definition("get_frontier", "Open a collection's curated key frontier reference space, original paths, versions, freshness and gaps. Curated selection is not a certification of exhaustive coverage.",
        {"collection_id": ID}, ["collection_id"]),
    _definition("prepare_research", "Prepare task/stage-specific navigation, curated anchors and discipline reading questions. Metadata only; no reading credit and no implicit network search.",
        RESEARCH_ARGUMENTS, ["task", "collection_id"]),
    _definition("begin_research", "Start a task-scoped research session before research decisions. Returns navigation and a session_id for original reading and the pre-synthesis check. Select focus_paper_ids only when those originals are necessary for this task.",
        {**RESEARCH_ARGUMENTS, "critical_checks": array(CRITICAL_CHECK, 8)}, ["task", "collection_id"]),
    _definition("read_research_material", "Read original text for a research session. Only returned nonempty original text counts as observed access; paths alone do not. Replay continuation arguments unchanged, including session_id, until the selected range is complete. Does not certify comprehension.",
        {"session_id": ID, "material_id": ID, "view": string(enum=["text", "original"]),
         "pages": array(integer(1, 100000), 100), "max_chars": integer(1, 20000),
         "char_offset": integer(0, 100000000)}, ["session_id", "material_id"]),
    _definition("read_research_context", "Read a physical page with up to two neighboring pages on either side to resolve definitions crossing page boundaries. Uses the normal observed-read budget and continuation. Complete selected pages do not prove conceptual completeness.",
        {"session_id": ID, "material_id": ID, "page": integer(1, 100000),
         "adjacent_pages": integer(0, 2), "max_chars": integer(1, 20000)}, ["session_id", "material_id", "page"]),
    _definition("submit_research_detail", "Submit an interpretation for a host-required critical question. Prefer passage_id anchors from the actual read response; the host resolves their original text without retyping. Alternatively supply read_id plus an exact quote. Metric answers should state numerator, denominator, positive class, unit, conditions and aggregation. Creates pending independent review, never self-approval. Use unresolved when context is insufficient.",
        {"session_id": ID, "check_id": ID, "answer": DETAIL_ANSWER,
         "anchors": array(DETAIL_ANCHOR, 8),
         "status": string(enum=["supported", "unresolved"])}, ["session_id", "check_id", "answer", "anchors"]),
    _definition("extend_research", "Add task-relevant included papers found after initial navigation to this session. Keeps existing focus requirements and read receipts; does not count as reading or reset checks.",
        {"session_id": ID, "paper_ids": {**array(ID, 20), "minItems": 1}}, ["session_id", "paper_ids"]),
    _definition("check_research", "Check observed original reads before research synthesis. Reports unread focus papers, pending selected-range continuations and freshness limits. This is a procedural check, not scientific validation. Recheck after further reads.",
        {"session_id": ID, "used_paper_ids": array(ID, 50)}, ["session_id"]),
    _definition("end_research", "Release a finished research session in this connection. Its receipts are then unavailable; a host-bound active task cannot be ended by the model.",
        {"session_id": ID}, ["session_id"]),
    _definition("list_sources", "List literature sources and their fields, coverage and availability.", {}),
    _definition("list_collections", "List available literature collections.", {}),
    _definition("list_papers", "Browse paper metadata in a collection or library without a search query, then choose original materials to inspect.",
        {"collection_id": ID, "membership": string(enum=["included", "candidate", "excluded", "all"]),
         "limit": integer(1, 200), "offset": integer(0, 1000000), "tag": string(minLength=1, maxLength=500)}),
    _definition("search_library", "Search stored metadata and original-material text. Returns material IDs and available local paths.",
        {"query": QUERY, "collection_id": ID, "membership": string(enum=["included", "candidate", "excluded", "all"]),
         "limit": integer(1, 100), "offset": integer(0, 1000000)}, ["query"]),
    _definition("get_paper", "Get a paper, its versions, identifiers and original-material entries.", {"paper_id": ID}, ["paper_id"]),
    _definition("list_materials", "List original PDFs, supplements, code/data links, extracted text and absolute local paths. Same-host agents may open these paths directly.",
        {"paper_id": ID, "collection_id": ID, "include_paths": {"type": "boolean"}}),
    _definition("read_material", "Read selected PDF physical pages (1-based), or get an original-material path. To continue truncated text, replay the returned continuation arguments unchanged. char_offset is scoped to the original pages selection; reset it to 0 when changing pages. next_page is navigation only, not evidence of reading that page. Does not execute code.",
        {"material_id": ID, "view": string(enum=["text", "original"]), "pages": array(integer(1, 100000), 100),
         "max_chars": integer(1, 100000), "char_offset": integer(0, 100000000)}, ["material_id"]),
    _definition("prepare_context", "Find task-relevant papers and material locations, freshness and gaps. Does not start an implicit network search.",
        {"task": QUERY, "collection_id": ID, "project_root": string(maxLength=4096),
         "max_papers": integer(1, 50), "max_chars": integer(256, 100000), "freshness_days": integer(0, 36500)}, ["task"]),
    _definition("get_job", "Read the persisted discovery/acquisition progress and unresolved tasks.", {"job_id": ID}, ["job_id"]),
]

WRITE_TOOLS = [
    _definition("configure_frontier", "Explicitly curate key included papers and the discipline profile for a collection. Stores identifiers only; does not infer scientific authority from venue, age or citation counts.",
        {"collection_id": ID, "paper_ids": array(ID, 20), "profile": RESEARCH_PROFILE}, ["collection_id", "paper_ids"]),
    _definition("create_collection", "Create a collection for a research topic.",
        {"name": string(minLength=1, maxLength=500), "description": string(maxLength=12000)}, ["name"]),
    _definition("update_collection", "Explicitly update shared collection defaults, including its discovery plan. Project configuration is not implicitly written back.",
        {"collection_id": ID, "config": object_schema(additionalProperties=True),
         "name": string(minLength=1, maxLength=500), "description": string(maxLength=12000)}, ["collection_id"]),
    _definition("set_tags", "Add or replace lightweight paper tags for browsing and organization.",
        {"paper_id": ID, "tags": array(string(minLength=1, maxLength=500), 200), "replace": {"type": "boolean"}}, ["paper_id", "tags"]),
    _definition("discover_papers", "Create a multi-source literature-discovery job. Candidates are not automatically included. Run the worker to execute it.",
        {"collection_id": ID, "query": QUERY, "sources": array(string(minLength=1), 30),
         "source_queries": object_schema(additionalProperties=string()), "date_from": string(), "date_to": string(),
         "budget": object_schema({"max_requests": integer(1, 10000)}), "request_id": ID}, ["collection_id", "query"]),
    _definition("discover_frontier", "Create bounded broad discovery followed by provider-verified recovery of known seeds. Reports broad recall and seed recovery separately; never treats seed input or cached metadata as a new source hit. Run the worker to execute.",
        {"collection_id": ID, "query": QUERY, "seeds": SEEDS,
         "sources": array(string(minLength=1), 30), "source_queries": object_schema(additionalProperties=string()),
         "date_from": string(), "date_to": string(), "budget": object_schema({"max_requests": integer(1, 10000), "broad_requests": integer(1, 10000)}),
         "request_id": ID}, ["collection_id", "query", "seeds"]),
    _definition("expand_citations", "Create a bounded one-hop reference or citation expansion from existing papers. Returns candidates with their origin and direction; does not automatically include them or certify broad topic coverage.",
        {"collection_id": ID, "paper_ids": {**array(ID, 50), "minItems": 1},
         "direction": string(enum=["citations", "references", "both"]),
         "date_from": string(), "date_to": string(), "budget": object_schema({"max_requests": integer(1, 10000)}),
         "request_id": ID}, ["collection_id", "paper_ids"]),
    _definition("set_membership", "Explicitly include/exclude candidate papers in a research collection.",
        {"collection_id": ID, "paper_ids": IDS, "status": string(enum=["candidate", "included", "excluded"])}, ["collection_id", "paper_ids"]),
    _definition("acquire_papers", "Create a full-text acquisition job for included papers; unresolved items can be handed off to a host browser.",
        {"collection_id": ID, "paper_ids": IDS, "request_id": ID,
         "version_policy": array(string(enum=["published", "accepted", "preprint", "unknown"]), 4)}, ["collection_id"]),
    _definition("run_worker", "Execute the persisted discovery or download job. Network access and local writes may occur.", {"job_id": ID}, ["job_id"]),
    _definition("resume_job", "Resume a paused or failed discovery, frontier or citation job from its checkpoints, then call run_worker. Acquisition jobs must use retry_acquisition or the authorized host browser recovery workflow.",
        {"job_id": ID}, ["job_id"]),
    _definition("cancel_job", "Cancel unfinished job work without deleting stored papers or original materials. Cancelling a frontier job also cancels its broad-discovery child.",
        {"job_id": ID}, ["job_id"]),
    _definition("retry_acquisition", "Retry public full-text acquisition routes for unresolved tasks without an active browser lease. Does not bypass tasks waiting for human action or silently override version policies.",
        {"job_id": ID, "refresh_locations": {"type": "boolean"}}, ["job_id"]),
]

LOCAL_TOOLS = [
    _definition("import_bibliography", "Import a trusted same-host BibTeX/RIS/CSL JSON/Zotero API JSON file. Never automatically reads referenced local attachments.",
        {"path": string(minLength=1, maxLength=4096), "collection_id": ID,
         "format": string(enum=["bibtex", "ris", "csl_json", "zotero_json", "json"]),
         "membership": string(enum=["candidate", "included", "excluded"])}, ["path", "collection_id"]),
    _definition("import_material", "Import a trusted same-host file or register a material URL. Supply exactly one path or URL; does not execute imported code.",
        {"paper_id": ID, "path": string(minLength=1, maxLength=4096), "url": string(minLength=1, maxLength=4096),
         "source_url": string(minLength=1, maxLength=4096),
         "role": string(enum=["main_text", "supplement", "code", "data", "model", "other"]),
         "version_id": ID, "confirmed": {"type": "boolean"}}, ["paper_id"]),
    _definition("confirm_material_identity", "Explicitly confirm a staged material's paper, role and version after inspection. Does not bypass file-format validation.",
        {"material_id": ID, "paper_id": ID, "role": string(enum=["main_text", "supplement", "code", "data", "model", "other"]),
         "version_id": ID}, ["material_id"]),
    _definition("export_collection", "Export materials and a manifest to a trusted same-host output directory. Only local host integrations should use this tool.",
        {"collection_id": ID, "output_dir": string(minLength=1, maxLength=4096), "format": string(enum=["bundle", "jsonl", "bibtex"])}, ["collection_id", "output_dir"]),
    _definition("claim_browser_task", "Claim one browser-download task for a same-host computer-use worker.",
        {"worker_id": ID, "session_id": ID, "capabilities": array(string(), 10), "job_id": ID}, ["worker_id"]),
    _definition("renew_browser_lease", "Renew a browser task lease while the browser is still downloading.",
        {"task_id": ID, "lease_token": ID}, ["task_id", "lease_token"]),
    _definition("submit_browser_result", "Submit a browser result from the task's local staging directory for validation.",
        {"task_id": ID, "lease_token": ID, "submission_id": ID,
         "result": object_schema({"outcome": string(enum=["downloaded", "retryable_error", "needs_user", "not_found"]),
                    "file": object_schema({"kind": string(enum=["local_path"]), "path": string(minLength=1, maxLength=4096)}, ["kind", "path"]),
                    "source_url": string(maxLength=4096), "reported_version": string(), "reason": string(maxLength=12000)}, ["outcome"])},
        ["task_id", "lease_token", "submission_id", "result"]),
    _definition("resume_browser_task", "Resume a browser task after an external obstacle is resolved.", {"task_id": ID}, ["task_id"]),
]


def _validate(value: Any, schema: dict, path: str = "arguments") -> None:
    """Validate the deliberately small JSON Schema subset emitted above."""
    if "anyOf" in schema:
        for alternative in schema["anyOf"]:
            try:
                _validate(value, alternative, path)
                break
            except ResearchError:
                continue
        else:
            raise ResearchError("INVALID_ARGUMENT", f"{path} must identify a paper by title, DOI, arXiv ID or PMID")
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
             "null": value is None}.get(kind, True)
    if not valid:
        raise ResearchError("INVALID_ARGUMENT", f"{path} must have type {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ResearchError("INVALID_ARGUMENT", f"{path} is not an allowed value")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise ResearchError("INVALID_ARGUMENT", f"{path}.{key} is required")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{path}.{key}")
            elif extra is False:
                raise ResearchError("INVALID_ARGUMENT", f"{path} has an unsupported field")
            elif isinstance(extra, dict):
                _validate(item, extra, f"{path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ResearchError("INVALID_ARGUMENT", f"{path} has too few items")
        if len(value) > schema.get("maxItems", float("inf")):
            raise ResearchError("INVALID_ARGUMENT", f"{path} has too many items")
        for index, item in enumerate(value):
            _validate(item, schema.get("items", {}), f"{path}[{index}]")
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
            raise ResearchError("INVALID_ARGUMENT", f"{path} has an invalid length")
    elif kind in ("number", "integer"):
        if not schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf")):
            raise ResearchError("INVALID_ARGUMENT", f"{path} is outside the allowed range")


class ToolRegistry:
    """Expose only explicitly registered methods, never arbitrary object attributes."""

    def __init__(self, core: Any, *, allow_writes: bool = False, local_admin: bool = False,
                 collection_id: str | None = None):
        self.core = core
        self.collection_id = collection_id
        from .usage import ResearchUsage
        self.research = ResearchUsage(core)
        self.required_session_id: str | None = None
        definitions = READ_TOOLS + (WRITE_TOOLS if allow_writes else []) + (LOCAL_TOOLS if local_admin and allow_writes else [])
        self._definitions = {item["function"]["name"]: deepcopy(item) for item in definitions}

    def specs(self) -> list[dict]:
        return deepcopy(list(self._definitions.values()))

    def require_research_ready(self, session_id: str, *, used_paper_ids: list[str] | None = None,
                               require_current_frontier: bool = False,
                               require_checked_details: bool = False) -> dict:
        """Trusted host hook immediately before accepting a research result."""
        check = self.research.check(session_id, used_paper_ids=used_paper_ids)
        if (not check["ready"] or (require_current_frontier and not check["current_frontier_ready"])
                or (require_checked_details and not check.get("ready_for_checked_synthesis", False))):
            raise ResearchError("RESEARCH_USAGE_REQUIRED", "Research output requires original-material usage and the selected freshness policy",
                                details={"research_usage": check})
        return check

    def review_research_detail(self, session_id: str, check_id: str, **review) -> dict:
        """Trusted host API; intentionally absent from model/MCP tool specs."""
        return self.research.review_detail(session_id, check_id, **review)

    def require_checked_synthesis(self, session_id: str, answer: str, reviewer,
                                  *, require_current_frontier: bool = False) -> dict:
        """Review the actual final draft, not only its earlier interpretations.

        A trusted independent reviewer must inspect the complete draft and its
        source-bound critical details. This callback is never a model tool and
        never chooses or contacts an external model implicitly.
        """
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 100000:
            raise ResearchError("INVALID_ARGUMENT", "Supply the actual final draft up to 100000 characters")
        check = self.require_research_ready(session_id, require_current_frontier=require_current_frontier,
                                            require_checked_details=True)
        packets = [self.research.detail_review_packet(session_id, item["check_id"])
                   for item in check["critical_checks"]]
        context = self.research.review_context(session_id)
        fingerprint = hashlib.sha256(json.dumps({"details":packets,"context":context},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if not callable(reviewer):
            raise ResearchError("RESEARCH_SYNTHESIS_REVIEW_REQUIRED", "An independent trusted host reviewer must inspect the final draft")
        try:
            review = reviewer(deepcopy({"answer": answer, "verified_details": packets,"observed_context":context,
                                        "mathematical_reference":{"classification":metric_reference(),
                                            "joint_illustration":metric_reference("joint_binary",trials=[{"answer_correct":True,"evidence_correct":True},{"answer_correct":False,"evidence_correct":False}])},
                                        "research_usage": check, "session_id": session_id}))
        except Exception:
            raise ResearchError("RESEARCH_SYNTHESIS_REVIEW_REQUIRED", "The final-draft reviewer was unavailable") from None
        if (not isinstance(review, dict) or set(review) != {"verdict", "reviewer", "reason"}
                or review.get("verdict") not in ("supported", "contradicted", "needs_context")
                or any(not isinstance(review.get(key), str) or not 1 <= len(review[key].strip()) <= 2000
                       for key in ("reviewer", "reason"))):
            raise ResearchError("RESEARCH_SYNTHESIS_REVIEW_REQUIRED", "The final-draft reviewer must return a bounded independent verdict")
        # Recheck after an asynchronous reviewer: submissions and source bytes
        # may have changed while it inspected the draft.
        check = self.require_research_ready(session_id, require_current_frontier=require_current_frontier,
                                            require_checked_details=True)
        current = [self.research.detail_review_packet(session_id, item["check_id"])
                   for item in check["critical_checks"]]
        current_context = self.research.review_context(session_id)
        if hashlib.sha256(json.dumps({"details":current,"context":current_context},sort_keys=True,ensure_ascii=False).encode()).hexdigest() != fingerprint:
            raise ResearchError("RESEARCH_SYNTHESIS_REVIEW_REQUIRED", "Critical details changed during final-draft review")
        if review["verdict"] != "supported":
            raise ResearchError("RESEARCH_SYNTHESIS_REVIEW_REQUIRED", "The final draft requires correction or additional context",
                                details={"review": deepcopy(review)})
        return {"research_usage": check, "synthesis_review": {**deepcopy(review),
            "draft_sha256": hashlib.sha256(answer.encode()).hexdigest(),
            "detail_revisions": {p["check_id"]: p["revision"] for p in current},
            "scope": "Independent review of this exact draft against selected critical details, not a guarantee of all scientific claims."}}

    def _read_context(self, session_id: str, material_id: str, page: int,
                      adjacent_pages: int = 1, max_chars: int = 12000) -> dict:
        # Scope validation precedes even page-count navigation. A path lookup
        # itself provides no reading credit and retains no original text.
        material = self.research.read(session_id, material_id, view="original", pages=[page])["material"]
        count = material.get("details", {}).get("parsed_pages")
        if not isinstance(count, int) or count <= 0 or page > count:
            raise ResearchError("INVALID_ARGUMENT", "Choose an extracted physical page; use list_materials for the available range")
        pages = list(range(max(1, page - adjacent_pages), min(count, page + adjacent_pages) + 1))
        result = self.research.read(session_id, material_id, pages=pages, max_chars=max_chars)
        result["context_selection"] = {"center_page": page, "selected_pages": pages,
            "notice": "Follow continuation unchanged; adjacent pages provide context but do not certify a complete definition."}
        return result

    def call(self, name: str, arguments: dict | None = None) -> dict:
        if name not in self._definitions:
            return {"schema_version": 1, "error": {"code": "TOOL_NOT_ALLOWED", "message": "Tool is unavailable in this session", "retryable": False, "details": {}}}
        try:
            arguments = {} if arguments is None else arguments
            if isinstance(arguments, dict):
                arguments = dict(arguments)
                if (self.collection_id and name in ("list_papers", "list_materials", "search_library", "prepare_context", "get_frontier", "prepare_research", "begin_research", "configure_frontier")
                        and "collection_id" not in arguments and not (name == "prepare_context" and arguments.get("project_root"))):
                    arguments["collection_id"] = self.collection_id
            _validate(arguments, self._definitions[name]["function"]["parameters"])
            if name == "metric_reference":
                return success(metric_reference(**arguments))
            if self.required_session_id:
                if name == "begin_research":
                    raise ResearchError("RESEARCH_SESSION_BOUND", "The host already opened this task's research session; use its session_id. Start a new host run to change the task or research stage.")
                if name in ("read_research_material", "read_research_context", "submit_research_detail", "check_research", "extend_research") and arguments["session_id"] != self.required_session_id:
                    raise ResearchError("RESEARCH_SESSION_BOUND", "Use the host-bound research session for this task")
                if name == "end_research":
                    raise ResearchError("RESEARCH_SESSION_BOUND", "The host owns this active task's lifetime")
            if name == "begin_research":
                return success(self.research.begin(**arguments))
            if name == "read_research_material":
                return success(self.research.read(**arguments))
            if name == "read_research_context":
                return success(self._read_context(**arguments))
            if name == "submit_research_detail":
                return success(self.research.submit_detail(**arguments))
            if name == "check_research":
                return success(self.research.check(**arguments))
            if name == "extend_research":
                return success(self.research.extend(**arguments))
            if name == "end_research":
                return success(self.research.end(**arguments))
            if name == "read_material" and self.required_session_id:
                # Ordinary reads remain usable; the host observes their actual text too.
                result = self.research.read(self.required_session_id, **arguments)
                if result.get("continuation"):
                    result["continuation"].pop("session_id", None)
                return success(result)
            if name == "resume_job":
                job_type = self.core.get_job(arguments["job_id"]).get("type")
                if job_type not in ("discovery", "frontier", "citations"):
                    raise ResearchError("TOOL_NOT_ALLOWED",
                        "This tool resumes discovery, frontier or citation jobs only. Use retry_acquisition for public acquisition routes; an authorized host must resolve browser obstacles before browser recovery.",
                        details={"job_type": job_type, "next_actions": ["retry_acquisition", "resume_browser_task"]})
            return success(getattr(self.core, name)(**arguments))
        except ResearchError as exc:
            return {"schema_version": 1, "error": exc.to_dict()}
        except Exception:
            # Unknown exception strings can contain provider URLs, credentials or local internals.
            return {"schema_version": 1, "error": {"code": "INTERNAL_ERROR", "message": "The tool could not complete; inspect local diagnostics", "retryable": False, "details": {}}}
