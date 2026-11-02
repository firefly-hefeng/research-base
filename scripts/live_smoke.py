"""Explicit network smoke test. No credentials or raw HTTP errors are printed."""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from dotenv import load_dotenv

from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.sources import SourceRegistry
from research_base.storage import now


def main():
    project = Path(__file__).resolve().parents[1]
    load_dotenv(project / ".env.local", override=False)
    report = {"checked_at": now(), "sources": {}, "model": {}, "acquisition": {}}
    registry = SourceRegistry()
    query = "retrieval augmented generation"

    def check_source(info):
        name = info["name"]
        try:
            page = registry.search_page(name, query, page_size=2, date_from="2020-01-01")
            result = {"ok": True, "returned_count": len(page.items), "has_next_page": page.next_cursor is not None,
                      "sample_titles": [p.title for p in page.items[:2]], "warnings": page.warnings}
        except ResearchError as error:
            result = {"ok": False, "code": error.code, "message": error.message, "details": error.details}
        except Exception as error:
            result = {"ok": False, "code": type(error).__name__}
        print(json.dumps({"source": name, **result}, ensure_ascii=True), flush=True)
        return name, result

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for name, result in pool.map(check_source, registry.list_sources()):
            report["sources"][name] = result
    registry.close()
    library = project / ".research-base" / "live-library"
    with ResearchBase(library) as base:
        collection = base.create_collection("Live smoke RAG literature", "Explicit smoke check; not a completed literature review")
        paper = base.add_paper({"source": "arxiv", "source_id": "2005.11401", "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
                               "authors": ["Patrick Lewis"], "year": 2020, "version_kind": "preprint",
                               "identifiers": {"arxiv": "2005.11401"}, "landing_url": "https://arxiv.org/abs/2005.11401",
                               "fulltext_urls": ["https://arxiv.org/pdf/2005.11401"]}, collection_id=collection["collection_id"])
        job = base.acquire_papers(collection["collection_id"])
        job = base.run_worker(job["job_id"])
        materials = base.list_materials(paper_id=paper["paper_id"])
        report["acquisition"] = {"state": job["state"], "job_id": job["job_id"], "library_root": str(library),
                                 "collection_id": collection["collection_id"], "paper_id": paper["paper_id"],
                                 "materials": [{k: m.get(k) for k in ("material_id", "status", "parse_status", "local_path", "text_path")} for m in materials]}
        print(json.dumps({"acquisition": report["acquisition"]}, ensure_ascii=True), flush=True)
        if os.getenv("DEEPSEEK_API_KEY"):
            try:
                with httpx.Client(timeout=30, follow_redirects=False) as client:
                    response = client.get("https://api.deepseek.com/models", headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]})
                    if response.status_code != 200:
                        raise ResearchError("MODEL_AUTH_FAILED", f"Model listing returned HTTP {response.status_code}")
                    models = [m["id"] for m in response.json().get("data", [])]
                model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
                if model not in models:
                    model = next((name for name in ("deepseek-flash", "deepseek-chat", "deepseek-v4-pro", "deepseek-reasoner") if name in models), model)
                with ResearchAgent(base, model=model) as agent:
                    plan = agent.plan_queries("Find recent methods for evaluating retrieval augmented generation, using multiple literature sources.")
                    analysis = agent.run("请先调用 list_materials，再用 read_material 读取集合中论文第1页，告诉我材料是否可读，并提供原始PDF路径。", collection_id=collection["collection_id"], max_steps=5, max_tool_calls=8)
                report["model"] = {"ok": True, "model": model, "available_models": models, "plan": plan, "agent_result": analysis}
            except ResearchError as error:
                report["model"] = {"ok": False, "code": error.code, "message": error.message}
            except Exception as error:
                report["model"] = {"ok": False, "code": type(error).__name__}
        else:
            report["model"] = {"ok": False, "code": "MODEL_NOT_CONFIGURED"}
    target = project / ".research-base" / "live-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"model_ok": report["model"].get("ok"), "model_code": report["model"].get("code"), "report_path": str(target)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
