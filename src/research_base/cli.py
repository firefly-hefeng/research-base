"""Command-line adapters for the same services used by research agents."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from .models import ResearchError, success

app = typer.Typer(no_args_is_help=True, help="Collect literature and expose original materials to research agents.")
collection_app = typer.Typer(no_args_is_help=True)
material_app = typer.Typer(no_args_is_help=True)
paper_app = typer.Typer(no_args_is_help=True)
worker_app = typer.Typer(no_args_is_help=True)
job_app = typer.Typer(no_args_is_help=True)
browser_app = typer.Typer(no_args_is_help=True)
agent_app = typer.Typer(no_args_is_help=True)
mcp_app = typer.Typer(no_args_is_help=True)
sources_app = typer.Typer(no_args_is_help=True)
library_app = typer.Typer(no_args_is_help=True)
citations_app = typer.Typer(no_args_is_help=True)
for name, group in (("collection", collection_app), ("material", material_app), ("paper", paper_app),
                    ("worker", worker_app), ("job", job_app), ("browser", browser_app),
                    ("agent", agent_app), ("mcp", mcp_app), ("sources", sources_app), ("library", library_app),
                    ("citations", citations_app)):
    app.add_typer(group, name=name)


@app.callback()
def main(ctx: typer.Context, library: Annotated[Path | None, typer.Option("--library", help="Local library root.")] = None,
         json_output: Annotated[bool, typer.Option("--json", help="Emit structured JSON (also the default).")] = False):
    from dotenv import load_dotenv
    load_dotenv(Path.cwd() / ".env.local", override=False)
    ctx.obj = {"library": library or Path(os.environ.get("RESEARCH_BASE_LIBRARY") or ".research-base/library"),
               "json": json_output}


def _base(ctx: typer.Context, *, existing: bool = False):
    if existing and not (Path(ctx.obj["library"]) / "library.json").is_file():
        raise ResearchError("LIBRARY_UNAVAILABLE", "Initialize or select an existing library before attaching a project.")
    from .core import ResearchBase
    base = ResearchBase(ctx.obj["library"])
    close = getattr(base, "close", None)
    if close:
        ctx.call_on_close(close)
    return base


def _output(data: Any) -> None:
    typer.echo(json.dumps(success(data), ensure_ascii=False, indent=2, default=str))


def _call(function, *args, **kwargs):
    try:
        value = function(*args, **kwargs)
        _output(value)
        return value
    except ResearchError as exc:
        typer.echo(json.dumps({"schema_version": 1, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        raise typer.Exit(code=1)
    except ValidationError:
        typer.echo(json.dumps({"schema_version": 1, "error": {
            "code": "INVALID_ARGUMENT", "message": "Input metadata does not match the expected schema.",
            "retryable": False, "details": {}}}, ensure_ascii=False, indent=2))
        raise typer.Exit(code=1)


def _invoke(ctx: typer.Context, method: str, *args, **kwargs):
    def action():
        return getattr(_base(ctx), method)(*args, **kwargs)
    return _call(action)


def _json_file(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ResearchError("INVALID_ARGUMENT", "Input file must contain valid JSON.") from exc
    if not isinstance(value, dict):
        raise ResearchError("INVALID_ARGUMENT", "JSON input must be an object.")
    return value


def _seed_file(path: Path) -> list[dict]:
    from .tools import SEEDS, _validate
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ResearchError("INVALID_ARGUMENT", "Seed file must contain a JSON array of known paper identifiers.") from exc
    _validate(value, SEEDS, "seeds")
    return value


@library_app.command("init")
def library_init(ctx: typer.Context):
    def action():
        base = _base(ctx)
        return {"library_id": base.library_id, "library_root": str(Path(ctx.obj["library"]).resolve())}
    _call(action)


@sources_app.command("list")
def sources_list(ctx: typer.Context):
    _invoke(ctx, "list_sources")


@collection_app.command("create")
def collection_create(ctx: typer.Context, name: Annotated[str, typer.Option("--name")],
                      description: Annotated[str, typer.Option("--description")] = ""):
    _invoke(ctx, "create_collection", name, description=description)


@collection_app.command("list")
def collection_list(ctx: typer.Context):
    _invoke(ctx, "list_collections")


@collection_app.command("update")
def collection_update(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
                      config: Annotated[Path, typer.Option("--config", help="JSON file containing the new collection defaults.")],
                      name: Annotated[str | None, typer.Option("--name")] = None,
                      description: Annotated[str | None, typer.Option("--description")] = None):
    def action():
        defaults = _json_file(config)
        return _base(ctx).update_collection(collection, config=defaults, name=name, description=description)
    _call(action)


@collection_app.command("include")
def collection_include(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
                       paper: Annotated[list[str], typer.Option("--paper")]):
    _invoke(ctx, "set_membership", collection, paper, status="included")


@collection_app.command("exclude")
def collection_exclude(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
                       paper: Annotated[list[str], typer.Option("--paper")]):
    _invoke(ctx, "set_membership", collection, paper, status="excluded")


@collection_app.command("export")
def collection_export(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
                      output: Annotated[Path, typer.Option("--output")],
                      format: Annotated[str, typer.Option("--format")] = "bundle"):
    _invoke(ctx, "export_collection", collection, str(output), format=format)


@collection_app.command("import")
def collection_import(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
                      file: Annotated[Path, typer.Option("--file")],
                      format: Annotated[str | None, typer.Option("--format")] = None,
                      membership: Annotated[str, typer.Option("--membership")] = "candidate"):
    _invoke(ctx, "import_bibliography", str(file), collection, format=format, membership=membership)


@app.command("discover")
def discover(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
             query: Annotated[str, typer.Option("--query")],
             source: Annotated[list[str] | None, typer.Option("--source")] = None,
             date_from: Annotated[str | None, typer.Option("--date-from")] = None,
             date_to: Annotated[str | None, typer.Option("--date-to")] = None,
             max_requests: Annotated[int, typer.Option("--max-requests", min=1)] = 30,
             request_id: Annotated[str | None, typer.Option("--request-id")] = None):
    _invoke(ctx, "discover_papers", collection, query, sources=source, date_from=date_from, date_to=date_to,
            budget={"max_requests": max_requests}, request_id=request_id)


@app.command("frontier")
def frontier(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
             query: Annotated[str, typer.Option("--query")],
             seeds: Annotated[Path, typer.Option("--seeds", help="JSON array of known titles or identifiers, not cached search results.")],
             source: Annotated[list[str] | None, typer.Option("--source")] = None,
             source_queries: Annotated[Path | None, typer.Option("--source-queries", help="Optional JSON object mapping sources to their query strings.")] = None,
             date_from: Annotated[str | None, typer.Option("--date-from")] = None,
             date_to: Annotated[str | None, typer.Option("--date-to")] = None,
             max_requests: Annotated[int, typer.Option("--max-requests", min=1, max=10000)] = 30,
             broad_requests: Annotated[int | None, typer.Option("--broad-requests", min=1, max=10000, help="Part of the total budget reserved for broad discovery.")] = None,
             request_id: Annotated[str | None, typer.Option("--request-id")] = None):
    def action():
        if broad_requests is not None and broad_requests > max_requests:
            raise ResearchError("INVALID_ARGUMENT", "broad-requests cannot exceed max-requests.")
        known = _seed_file(seeds)
        queries = _json_file(source_queries) if source_queries else None
        if queries is not None:
            from .tools import _validate, object_schema, string
            _validate(queries, object_schema(additionalProperties=string()), "source_queries")
        return _base(ctx).discover_frontier(collection, query, known, sources=source, source_queries=queries,
                                            date_from=date_from, date_to=date_to,
                                            budget={"max_requests": max_requests, **({"broad_requests": broad_requests} if broad_requests is not None else {})}, request_id=request_id)
    _call(action)


@citations_app.command("expand")
def citations_expand(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
                     paper: Annotated[list[str], typer.Option("--paper")],
                     direction: Annotated[str, typer.Option("--direction")] = "citations",
                     date_from: Annotated[str | None, typer.Option("--date-from")] = None,
                     date_to: Annotated[str | None, typer.Option("--date-to")] = None,
                     max_requests: Annotated[int, typer.Option("--max-requests", min=1, max=10000)] = 10,
                     request_id: Annotated[str | None, typer.Option("--request-id")] = None):
    def action():
        if direction not in ("citations", "references", "both"):
            raise ResearchError("INVALID_ARGUMENT", "Direction must be citations, references or both.")
        return _base(ctx).expand_citations(collection, paper, direction=direction, date_from=date_from,
                                          date_to=date_to, budget={"max_requests": max_requests}, request_id=request_id)
    _call(action)


@worker_app.command("run")
def worker_run(ctx: typer.Context, job: Annotated[str, typer.Option("--job")]):
    _invoke(ctx, "run_worker", job)


@job_app.command("show")
def job_show(ctx: typer.Context, job_id: str):
    _invoke(ctx, "get_job", job_id)


@job_app.command("resume")
def job_resume(ctx: typer.Context, job_id: str):
    _invoke(ctx, "resume_job", job_id)


@job_app.command("cancel")
def job_cancel(ctx: typer.Context, job_id: str):
    _invoke(ctx, "cancel_job", job_id)


@job_app.command("retry-access")
def job_retry_access(ctx: typer.Context, job_id: str,
                     refresh_locations: Annotated[bool, typer.Option("--refresh-locations/--keep-locations")] = True):
    _invoke(ctx, "retry_acquisition", job_id, refresh_locations=refresh_locations)


@app.command("search")
def search(ctx: typer.Context, query: str,
           collection: Annotated[str | None, typer.Option("--collection")] = None,
           membership: Annotated[str, typer.Option("--membership")] = "included",
           limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
           offset: Annotated[int, typer.Option("--offset", min=0)] = 0):
    _invoke(ctx, "search_library", query, collection_id=collection, membership=membership, limit=limit, offset=offset)


@paper_app.command("show")
def paper_show(ctx: typer.Context, paper_id: str):
    _invoke(ctx, "get_paper", paper_id)


@paper_app.command("list")
def paper_list(ctx: typer.Context,
               collection: Annotated[str | None, typer.Option("--collection")] = None,
               membership: Annotated[str, typer.Option("--membership")] = "included",
               limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 100,
               offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
               tag: Annotated[str | None, typer.Option("--tag")] = None):
    _invoke(ctx, "list_papers", collection_id=collection, membership=membership, limit=limit, offset=offset, tag=tag)


@paper_app.command("tag")
def paper_tag(ctx: typer.Context, paper_id: str,
              tag: Annotated[list[str], typer.Option("--tag")],
              replace: Annotated[bool, typer.Option("--replace")] = False):
    _invoke(ctx, "set_tags", paper_id, tag, replace=replace)


@paper_app.command("add")
def paper_add(ctx: typer.Context,
              metadata: Annotated[Path, typer.Option("--metadata", help="JSON file with normalized paper metadata.")],
              collection: Annotated[str | None, typer.Option("--collection")] = None):
    def action():
        candidate = _json_file(metadata)
        return _base(ctx).add_paper(candidate, collection_id=collection)
    _call(action)


@material_app.command("list")
def material_list(ctx: typer.Context, paper: Annotated[str | None, typer.Option("--paper")] = None,
                  collection: Annotated[str | None, typer.Option("--collection")] = None,
                  paths: Annotated[bool, typer.Option("--paths/--no-paths")] = True):
    _invoke(ctx, "list_materials", paper_id=paper, collection_id=collection, include_paths=paths)


@material_app.command("read")
def material_read(ctx: typer.Context, material_id: str,
                  view: Annotated[str, typer.Option("--view")] = "text",
                  page: Annotated[list[int] | None, typer.Option("--page")] = None,
                  max_chars: Annotated[int, typer.Option("--max-chars", min=1)] = 12000,
                  char_offset: Annotated[int, typer.Option("--char-offset", min=0)] = 0):
    _invoke(ctx, "read_material", material_id, view=view, pages=page, max_chars=max_chars, char_offset=char_offset)


@material_app.command("import")
def material_import(ctx: typer.Context, paper: Annotated[str, typer.Option("--paper")],
                    file: Annotated[Path | None, typer.Option("--file")] = None,
                    url: Annotated[str | None, typer.Option("--url")] = None,
                    role: Annotated[str, typer.Option("--role")] = "main_text",
                    version: Annotated[str | None, typer.Option("--version")] = None,
                    confirmed: Annotated[bool, typer.Option("--confirmed")] = False):
    _invoke(ctx, "import_material", paper, path=str(file) if file else None, url=url, role=role,
            version_id=version, confirmed=confirmed)


@material_app.command("confirm-identity")
def material_confirm_identity(ctx: typer.Context, material_id: str,
                              paper: Annotated[str | None, typer.Option("--paper")] = None,
                              role: Annotated[str | None, typer.Option("--role")] = None,
                              version: Annotated[str | None, typer.Option("--version")] = None):
    _invoke(ctx, "confirm_material_identity", material_id, paper_id=paper, role=role, version_id=version)


@app.command("context")
def context(ctx: typer.Context, task: Annotated[str, typer.Option("--task")],
            collection: Annotated[str | None, typer.Option("--collection")] = None,
            project: Annotated[Path | None, typer.Option("--project")] = None,
            max_papers: Annotated[int, typer.Option("--max-papers", min=1)] = 8,
            max_chars: Annotated[int, typer.Option("--max-chars", min=1)] = 12000):
    _invoke(ctx, "prepare_context", task, collection_id=collection, project_root=str(project) if project else None,
            max_papers=max_papers, max_chars=max_chars)


@app.command("acquire")
def acquire(ctx: typer.Context, collection: Annotated[str, typer.Option("--collection")],
            paper: Annotated[list[str] | None, typer.Option("--paper")] = None,
            request_id: Annotated[str | None, typer.Option("--request-id")] = None):
    _invoke(ctx, "acquire_papers", collection, paper_ids=paper, request_id=request_id)


@browser_app.command("claim")
def browser_claim(ctx: typer.Context, worker: Annotated[str, typer.Option("--worker")],
                  session: Annotated[str, typer.Option("--session")] = "default",
                  job: Annotated[str | None, typer.Option("--job")] = None):
    _invoke(ctx, "claim_browser_task", worker, session_id=session, capabilities=["local_download"], job_id=job)


@browser_app.command("renew")
def browser_renew(ctx: typer.Context, task: Annotated[str, typer.Option("--task")],
                  token: Annotated[str, typer.Option("--lease-token", envvar="RESEARCH_BASE_LEASE_TOKEN", hide_input=True)]):
    _invoke(ctx, "renew_browser_lease", task, token)


@browser_app.command("submit")
def browser_submit(ctx: typer.Context, file: Annotated[Path, typer.Option("--file")]):
    def action():
        payload = _json_file(file)
        return _base(ctx).submit_browser_result(**payload)
    _call(action)


@browser_app.command("resume")
def browser_resume(ctx: typer.Context, task: Annotated[str, typer.Option("--task")]):
    _invoke(ctx, "resume_browser_task", task)


@app.command("attach")
def attach(ctx: typer.Context, project: Annotated[Path, typer.Option("--project")],
           collection: Annotated[str, typer.Option("--collection")],
           library_ref: Annotated[str, typer.Option("--library-ref")] = "default",
           mode: Annotated[str, typer.Option("--mode")] = "agents",
           dry_run: Annotated[bool, typer.Option("--dry-run")] = False):
    from .integration import attach_project
    def action():
        base = _base(ctx, existing=True)
        if not any(c["collection_id"] == collection for c in base.list_collections()):
            raise ResearchError("NOT_FOUND", "Collection does not exist in this library.")
        return attach_project(project, library_ref, base.library_id, collection, mode=mode, dry_run=dry_run)
    _call(action)


@app.command("detach")
def detach(project: Annotated[Path, typer.Option("--project")],
           dry_run: Annotated[bool, typer.Option("--dry-run")] = False):
    from .integration import detach_project
    _call(detach_project, project, dry_run=dry_run)


@app.command("doctor")
def doctor(ctx: typer.Context, project: Annotated[Path, typer.Option("--project")]):
    from .integration import doctor_project
    def action():
        result = doctor_project(project)
        result["selected_library_root"] = str(Path(ctx.obj["library"]).resolve())
        root = Path(ctx.obj["library"])
        if (root / "library.json").exists():
            base = _base(ctx)
            result["library_matches"] = result["library_id"] in (None, base.library_id)
            result["collection_exists"] = any(c["collection_id"] == result["collection_id"] for c in base.list_collections())
        else:
            result["library_matches"] = False
            result["collection_exists"] = False
        return result
    _call(action)


@agent_app.command("run")
def agent_run(ctx: typer.Context, task: str,
              collection: Annotated[str | None, typer.Option("--collection")] = None,
              project: Annotated[Path | None, typer.Option("--project", help="Explicit project whose local rules and task context should be loaded.")] = None,
              allow_writes: Annotated[bool, typer.Option("--allow-writes")] = False,
              max_steps: Annotated[int, typer.Option("--max-steps", min=1, max=32)] = 8):
    def action():
        from .agent import ResearchAgent
        with ResearchAgent(_base(ctx)) as agent:
            return agent.run(task, collection_id=collection, project_root=project, allow_writes=allow_writes, max_steps=max_steps)
    _call(action)


@agent_app.command("plan")
def agent_plan(ctx: typer.Context, task: str):
    def action():
        from .agent import ResearchAgent
        with ResearchAgent(_base(ctx)) as agent:
            return agent.plan_queries(task)
    _call(action)


@mcp_app.command("serve")
def mcp_serve(ctx: typer.Context):
    from .mcp_server import serve
    try:
        serve(ctx.obj["library"])
    except ResearchError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=1)

