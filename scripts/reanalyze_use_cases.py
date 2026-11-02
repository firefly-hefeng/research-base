"""Re-run only project-aware research analysis on already collected materials."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.storage import now


def main():
    project = Path(__file__).resolve().parents[1]
    load_dotenv(project / ".env.local", override=False)
    with ResearchBase(project / ".research-base" / "library") as base:
        for name in ("rag-evaluation", "single-cell-perturbation"):
            directory = project / ".research-base" / "use-cases" / name
            path = directory / "run-result.json"
            result = json.loads(path.read_text(encoding="utf-8"))
            cid = result["collection_id"]
            task = ("为实验室RAG系统比较BM25、稠密检索、混合检索和重排序，设计可诊断的评估；区分检索质量、生成忠实性、覆盖与成本。"
                    if name == "rag-evaluation" else
                    "为单细胞扰动预测设计未见扰动和未见细胞背景的可信外推评估；比较简单基线，检查数据泄漏、指标和当前作者代码修正。")
            materials = base.list_materials(collection_id=cid)
            available = [m for m in materials if m["status"] == "available" and m["parse_status"] == "ready"]
            task += ("\n遵循项目规则。只需读取两篇最相关论文的方法或评估部分（每次至多6000字符），再读一份与本题最相关的作者README。"
                     "然后直接给出不超过1500字的可执行研究计划：设置、基线、指标、复核、与已读近期工作的关系和材料限制。"
                     "不要反复列全库目录；已绑定当前集合。未跑实验，不报告性能排名；检索预算有限，不声称覆盖了全部前沿。"
                     "当前可读取材料：\n" + json.dumps([{k: m.get(k) for k in ("material_id", "paper_id", "role", "format")} for m in available], ensure_ascii=False))
            with ResearchAgent(base) as agent:
                answer = agent.run(task, project_root=directory / "research-project", max_steps=7, max_tool_calls=16)
            old_path = directory / "research-plan.md"
            if old_path.exists():
                (directory / "initial-analysis-result.txt").write_text(old_path.read_text(encoding="utf-8"), encoding="utf-8")
            old_path.write_text(answer["answer"], encoding="utf-8")
            result["initial_research_agent"] = result["research_agent"]
            result["research_agent"] = {k: answer.get(k) for k in ("status", "model", "tool_calls", "usage", "limit_reason", "project_integration")}
            acquisition = base.get_job(result["acquisition"]["job_id"])
            result["acquisition"] = {k: acquisition.get(k) for k in ("job_id", "state", "task_count", "paper_count", "tasks")}
            result["materials"] = [{k: m.get(k) for k in ("material_id", "paper_id", "role", "status", "parse_status", "local_path", "text_path")} for m in materials]
            papers = base.list_papers(cid)["papers"]
            result["fulltext_available_count"] = sum(base.get_paper(p["paper_id"])["fulltext_available"] for p in papers)
            result["analysis_repeated_at"] = now()
            result["freshness"] = base.prepare_context("evaluation perturbation retrieval", project_root=directory / "research-project")["freshness"]
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": name, "fulltext_available": result["fulltext_available_count"], "agent_status": answer["status"],
                              "project_integration": answer.get("project_integration"), "tool_calls": len(answer["tool_calls"]), "plan_chars": len(answer["answer"])}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
