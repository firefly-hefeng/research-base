"""One validated tool surface for MCP, external agents and DeepSeek."""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from .models import ResearchError, success


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
READ_TOOLS = [
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
        definitions = READ_TOOLS + (WRITE_TOOLS if allow_writes else []) + (LOCAL_TOOLS if local_admin and allow_writes else [])
        self._definitions = {item["function"]["name"]: deepcopy(item) for item in definitions}

    def specs(self) -> list[dict]:
        return deepcopy(list(self._definitions.values()))

    def call(self, name: str, arguments: dict | None = None) -> dict:
        if name not in self._definitions:
            return {"schema_version": 1, "error": {"code": "TOOL_NOT_ALLOWED", "message": "Tool is unavailable in this session", "retryable": False, "details": {}}}
        try:
            arguments = {} if arguments is None else arguments
            if isinstance(arguments, dict):
                arguments = dict(arguments)
                if (self.collection_id and name in ("list_papers", "list_materials", "search_library", "prepare_context")
                        and "collection_id" not in arguments and not (name == "prepare_context" and arguments.get("project_root"))):
                    arguments["collection_id"] = self.collection_id
            _validate(arguments, self._definitions[name]["function"]["parameters"])
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
