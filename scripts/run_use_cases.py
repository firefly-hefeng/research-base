"""Run bounded real research workflows; retain gaps and actual source/job states.

This explicit network command calls DeepSeek and downloads public papers. It does
not train research models, execute paper code, or automate a browser login.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from dotenv import load_dotenv

from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.integration import attach_project
from research_base.models import ResearchError
from research_base.storage import now


def seed_match(paper, seed):
    versions = [paper] + [v["metadata"] for v in paper.get("versions", [])]
    for meta in versions:
        if seed.get("doi") and (meta.get("doi") or "").lower() == seed["doi"].lower():
            return True
        arxiv = meta.get("identifiers", {}).get("arxiv", "")
        if seed.get("arxiv") and re.sub(r"v\d+$", "", arxiv) == seed["arxiv"]:
            return True
        if seed["title"].lower() in meta.get("title", "").lower():
            return True
    return False


def fetch_readme(base, paper_id, repo, directory):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(f"https://api.github.com/repos/{repo}/readme", headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        record = response.json()
    target = directory / (repo.replace("/", "_") + "_README.md")
    target.write_bytes(base64.b64decode(record["content"]))
    material = base.import_material(paper_id, path=target, role="code")
    with base.store.connect() as db:
        details = {**material["details"], "github_blob_sha": record["sha"], "repository": repo, "fetched_at": now()}
        db.execute("UPDATE materials SET details=?,source_url=? WHERE id=?", (json.dumps(details), record["html_url"], material["material_id"]))
    return {"repo": repo, "blob_sha": record["sha"], "material_id": material["material_id"], "local_path": material["local_path"]}


def run_case(plan, project, max_requests):
    directory = project / ".research-base" / "use-cases" / plan["case_id"]
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    summary = {"case_id": plan["case_id"], "started_at": now(), "discovery_jobs": [], "seed_checks": [], "code_materials": [], "gaps": []}
    with ResearchBase(project / ".research-base" / "library") as base:
        collection = base.create_collection(plan["collection_name"], plan["research_task"])
        cid = collection["collection_id"]
        summary["collection_id"] = cid
        generic_ids = set()
        # A bounded first pass tests the workflow, not exhaustive frontier coverage.
        for search in plan["searches"]:
            search = {**search, "budget": {"max_requests": max_requests}}
            job = base.discover_papers(cid, **search)
            job = base.run_worker(job["job_id"])
            generic_ids.update(job["progress"].get("paper_ids", []))
            summary["discovery_jobs"].append({"kind": "broad", "job_id": job["job_id"], "state": job["state"], "sources": job["progress"].get("sources"), "requests": job["progress"].get("requests"), "stop_reason": job["progress"].get("stop_reason")})
            print(json.dumps({"case": plan["case_id"], "discovery": job["state"], "unique_candidates_so_far": len(generic_ids)}), flush=True)
        seed_ids = []
        for seed in plan["seed_checks"]:
            rows = base.list_papers(cid, membership="all", limit=200)
            ids = []
            while True:
                ids.extend(p["paper_id"] for p in rows["papers"])
                if rows["next_offset"] is None:
                    break
                rows = base.list_papers(cid, membership="all", limit=200, offset=rows["next_offset"])
            match = next((pid for pid in ids if seed_match(base.get_paper(pid), seed)), None)
            broad_recalled = match in generic_ids if match else False
            if not match:
                sources = ["openalex", "crossref", "arxiv"] if plan["case_id"] == "rag-evaluation" else ["openalex", "crossref", "europe_pmc"]
                search = {"collection_id": cid, "query": seed["title"], "sources": sources, "budget": {"max_requests": 3}}
                job = base.run_worker(base.discover_papers(**search)["job_id"])
                candidates = job["progress"].get("paper_ids", [])
                match = next((pid for pid in candidates if seed_match(base.get_paper(pid), seed)), None)
                summary["discovery_jobs"].append({"kind": "targeted_seed_search", "job_id": job["job_id"], "state": job["state"], "sources": job["progress"].get("sources")})
            summary["seed_checks"].append({"title": seed["title"], "broad_recalled": broad_recalled, "found_after_targeted_search": bool(match), "paper_id": match})
            if match:
                seed_ids.append(match)
        english_query = plan["searches"][0]["query"]
        pool = base.search_library(english_query, collection_id=cid, membership="candidate", limit=25)["papers"]
        pool = [{k: p.get(k) for k in ("paper_id", "title", "authors", "year", "abstract")} for p in pool]
        present = {p["paper_id"] for p in pool}
        for pid in seed_ids:
            if pid not in present:
                p = base.get_paper(pid)
                pool.append({k: p.get(k) for k in ("paper_id", "title", "authors", "year", "abstract")})
        with ResearchAgent(base) as agent:
            selection = agent.select_papers(plan["research_task"], pool, max_papers=4)
        summary["selection"] = selection
        chosen = list(dict.fromkeys(selection["selected_paper_ids"] + seed_ids))
        base.set_membership(cid, chosen, "included")
        for pid in chosen:
            base.set_tags(pid, ["case:" + plan["case_id"]] + (["seed"] if pid in seed_ids else []))
        acquisition = base.run_worker(base.acquire_papers(cid)["job_id"])
        summary["acquisition"] = {k: acquisition.get(k) for k in ("job_id", "state", "task_count", "paper_count", "tasks")}
        repositories = [("amazon-science/RAGChecker", 0)] if plan["case_id"] == "rag-evaluation" else [("bm2-lab/scPerturBench", 0), ("ArcInstitute/state", 1)]
        for repo, seed_index in repositories:
            if seed_index >= len(summary["seed_checks"]) or not summary["seed_checks"][seed_index]["paper_id"]:
                summary["gaps"].append({"repository": repo, "reason": "associated_seed_missing"})
                continue
            try:
                summary["code_materials"].append(fetch_readme(base, summary["seed_checks"][seed_index]["paper_id"], repo, directory))
            except Exception as error:
                summary["gaps"].append({"repository": repo, "reason": type(error).__name__})
        trial_project = directory / "research-project"
        trial_project.mkdir(exist_ok=True)
        rules = trial_project / "AGENTS.md"
        if not rules.exists():
            rules.write_text("# Research project\n\nPreserve experiment configurations and describe actual limitations.\n", encoding="utf-8")
        first = attach_project(trial_project, "default", base.library_id, cid)
        second = attach_project(trial_project, "default", base.library_id, cid)
        summary["project_integration"] = {"project_root": str(trial_project), "first": first, "second": second}
        context = base.prepare_context(english_query, project_root=trial_project)
        summary["freshness"] = context["freshness"]
        materials = base.list_materials(collection_id=cid)
        readable = [m for m in materials if m["status"] == "available" and m["parse_status"] == "ready"]
        prompt = (plan["research_task"] + "\n请先查询集合或列出材料，再实际读取至少两篇相关论文的方法/评价部分及可用的作者README，给出可执行研究计划。"
                  "讨论哪些比较条件必须一致、应使用的基线与指标、哪些结论受材料缺失影响。代码README的当前修正说明优先于旧表格。"
                  "没有运行基准实验，不得报告新的性能排名；有限预算检索没有完成整个领域覆盖，不得声称找到全部前沿。"
                  "按需要使用read_material的pages或char_offset定位。材料ID与用途如下：\n" + json.dumps([{k: m.get(k) for k in ("material_id", "paper_id", "role", "format", "parse_status")} for m in readable], ensure_ascii=False))
        with ResearchAgent(base) as agent:
            answer = agent.run(prompt, collection_id=cid, max_steps=10, max_tool_calls=25)
        (directory / "research-plan.md").write_text(answer["answer"], encoding="utf-8")
        summary["research_agent"] = {k: answer[k] for k in ("status", "model", "tool_calls", "usage")}
        summary["research_plan_path"] = str(directory / "research-plan.md")
        summary["materials"] = [{k: m.get(k) for k in ("material_id", "paper_id", "role", "status", "parse_status", "local_path", "text_path")} for m in materials]
        summary["included_count"] = len(chosen)
        summary["candidate_count"] = base.get_collection(cid)["counts"].get("candidate", 0)
        summary["fulltext_available_count"] = sum(p["fulltext_available"] for p in (base.get_paper(pid) for pid in chosen))
        summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
        summary["completed_at"] = now()
        (directory / "run-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": plan["case_id"], "included": len(chosen), "fulltext_available": summary["fulltext_available_count"], "agent_status": answer["status"], "result_path": str(directory / "run-result.json")}, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["rag-evaluation", "single-cell-perturbation", "all"], default="all")
    parser.add_argument("--max-requests", type=int, default=8)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    load_dotenv(project / ".env.local", override=False)
    document = (project / "docs" / "use-cases.md").read_text(encoding="utf-8")
    plans = [json.loads(s) for s in re.findall(r"```json\n(.*?)\n```", document, re.S)]
    for plan in plans:
        if args.case in ("all", plan["case_id"]):
            run_case(plan, project, args.max_requests)


if __name__ == "__main__":
    main()
