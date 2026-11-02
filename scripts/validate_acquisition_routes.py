"""Bounded real acquisition proof in a fresh, independent library.

Uses one actual seed from the existing RAG case. Metadata is fetched again from
an official source; original case files and the main catalog are read-only.
No model calls, credentials loading, browser control or fabricated failures.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from pypdf import PdfReader

from research_base.core import ResearchBase
from research_base.jobs import JobsMixin
from research_base.materials import MaterialsMixin
from research_base.models import ResearchError
from research_base.resolvers import resolve_locations
from research_base.sources import SourceRegistry
from research_base.storage import now


def _title(value):
    return " ".join(re.findall(r"\w+", value.lower()))


def _code_location(function):
    path = Path(inspect.getsourcefile(function)).resolve()
    _, line = inspect.getsourcelines(function)
    return {"path": path.relative_to(PROJECT).as_posix(), "line": line}


def _original_seed(project, source_library):
    case_file = project / ".research-base/use-cases/rag-evaluation/run-result.json"
    case = json.loads(case_file.read_text(encoding="utf-8"))
    seed = case["seed_checks"][0]
    catalog = source_library / "catalog.sqlite"
    if not catalog.is_file():
        raise ResearchError("NOT_FOUND", "Existing case library is missing")
    # Open the original catalog read-only, including its WAL-visible snapshot.
    connection = sqlite3.connect("file:" + catalog.resolve().as_posix() + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT metadata FROM papers WHERE id=?", (seed["paper_id"],)).fetchone()
        if not row:
            raise ResearchError("NOT_FOUND", "Case seed is absent from the original catalog")
        metadata = json.loads(row[0])
        ids = dict(connection.execute("SELECT namespace,value FROM identifiers WHERE paper_id=?", (seed["paper_id"],)))
    finally:
        connection.close()
    return {"paper_id": seed["paper_id"], "title": metadata["title"], "arxiv": ids.get("arxiv") or ids.get("arxiv_work"),
            "case_file": str(case_file), "case_id": case["case_id"]}


def _fresh_metadata(seed, provider):
    attempts = []
    with SourceRegistry() as sources:
        choices = ["openalex", "arxiv"] if provider == "auto" else [provider]
        for name in choices:
            if name == "arxiv" and not seed["arxiv"]:
                continue
            query = seed["title"] if name == "openalex" else "id:" + seed["arxiv"]
            try:
                page = sources.search_page(name, query, page_size=5)
                matched = [item for item in page.items if _title(item.title) == _title(seed["title"])]
                if name == "arxiv":
                    expected = re.sub(r"v\d+$", "", seed["arxiv"])
                    matched = [item for item in matched if re.sub(r"v\d+$", "", item.identifiers.get("arxiv", "")) == expected]
                attempts.append({"provider": name, "query": query, "returned_count": len(page.items), "identity_matched": bool(matched)})
                if matched:
                    return matched[0], attempts
            except ResearchError as error:
                attempts.append({"provider": name, "error": error.to_dict()})
    raise ResearchError("SEED_NOT_FOUND", "Official metadata lookup did not return the case seed", details={"attempts": attempts})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-library", type=Path, default=PROJECT / ".research-base/library")
    parser.add_argument("--provider", choices=["auto", "openalex", "arxiv"], default="auto")
    parser.add_argument("--output", type=Path, default=PROJECT / ".research-base/round2/acquisition-route-proof.json")
    args = parser.parse_args()
    started = time.monotonic()
    report = {"started_at": now(), "purpose": "independent real acquisition proof; not an increase to use-case paper counts",
              "browser_used": False, "model_used": False, "historical_cases_modified": False}
    run_id = now().replace(":", "").replace(".", "-") + "-" + uuid.uuid4().hex[:6]
    isolated_root = PROJECT / ".research-base/round2/acquisition-smoke" / run_id
    try:
        seed = _original_seed(PROJECT, args.source_library.resolve())
        report["existing_case_seed"] = seed
        candidate, attempts = _fresh_metadata(seed, args.provider)
        report["metadata_lookup"] = attempts
        report["metadata_origin"] = {"source": candidate.source, "source_id": candidate.source_id,
                                     "title": candidate.title, "doi": candidate.doi, "version_kind": candidate.version_kind,
                                     "identifiers": candidate.identifiers, "fulltext_urls": candidate.fulltext_urls}
        with ResearchBase(isolated_root) as base:
            collection = base.create_collection("RAG actual acquisition route proof", "Fresh download only; no prior material imported")
            paper = base.add_paper(candidate, collection_id=collection["collection_id"])
            report["independent_library"] = {"path": str(base.root), "library_id": base.library_id,
                                             "collection_id": collection["collection_id"], "paper_id": paper["paper_id"]}
            before = base.list_materials(paper_id=paper["paper_id"])
            report["available_originals_before"] = sum(m["status"] == "available" for m in before)
            job = base.acquire_papers(collection["collection_id"])
            result = base.run_worker(job["job_id"])
            report["acquisition"] = {key: result.get(key) for key in ("job_id", "state", "task_count", "paper_count", "tasks")}
            report["route_attempts"] = [route for task in result.get("tasks", []) for route in task.get("result", {}).get("route_attempts", [])]
            report["originals"] = []
            for material in base.list_materials(paper_id=paper["paper_id"]):
                if material["status"] != "available" or material["role"] != "main_text" or material["format"] != "pdf":
                    continue
                path = Path(material["local_path"])
                reader = PdfReader(path)
                header = reader.pages[0].extract_text() or ""
                title_words = set(re.findall(r"[a-z0-9]+", seed["title"].lower()))
                header_words = set(re.findall(r"[a-z0-9]+", header.lower()))
                title_overlap = len(title_words & header_words) / max(1, len(title_words))
                stamp = re.search(r"arxiv\s*:\s*((?:\d{4}\.\d{4,5})(?:v\d+)?)", header, re.I)
                actual_arxiv = stamp.group(1) if stamp else None
                expected_arxiv = re.sub(r"v\d+$", "", seed["arxiv"] or "")
                arxiv_matches = (re.sub(r"v\d+$", "", actual_arxiv) == expected_arxiv) if actual_arxiv and expected_arxiv else None
                with path.open("rb") as handle:
                    prefix_ok = handle.read(5) == b"%PDF-"
                report["originals"].append({"material_id": material["material_id"], "local_path": str(path),
                                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
                                            "pdf_magic_valid": prefix_ok, "page_count": len(reader.pages), "first_page_title_word_overlap": title_overlap,
                                            "first_page_arxiv_id": actual_arxiv, "expected_arxiv_id_matches": arxiv_matches,
                                            "parse_status": material["parse_status"], "version_id": material["version_id"]})
            report["success"] = (result["state"] == "succeeded" and report["available_originals_before"] == 0
                                 and any(m["pdf_magic_valid"] and m["page_count"] > 0 and m["first_page_title_word_overlap"] >= .75
                                         and m["expected_arxiv_id_matches"] is not False for m in report["originals"]))
        with SourceRegistry() as sources:
            metadata_parser = _code_location(type(sources.adapters[candidate.source]).search)
        report["source_code_locations"] = {"metadata_parser": metadata_parser,
                                            "route_scheduler": _code_location(JobsMixin._run_acquisition),
                                            "route_resolver": _code_location(resolve_locations),
                                            "pdf_identity_and_ingestion": _code_location(MaterialsMixin._finalize_material)}
    except ResearchError as error:
        report["success"] = False
        report["error"] = error.to_dict()
    except Exception as error:
        report["success"] = False
        report["error"] = {"code": type(error).__name__, "message": "Validation stopped; inspect the independent library and source code"}
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    report["finished_at"] = now()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": report["success"], "route_attempts": report.get("route_attempts"),
                      "pdfs": [{k: m[k] for k in ("page_count", "bytes", "first_page_arxiv_id", "first_page_title_word_overlap")} for m in report.get("originals", [])],
                      "error": report.get("error"), "report_path": str(args.output.resolve())}, ensure_ascii=True), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
