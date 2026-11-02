"""Use current scoped search records and new originals to revise real plans.

Only selected original ranges are available to the optional external model.
Raw output and observed reads remain local and are reviewed separately.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.storage import now
from selected_originals import SelectedOriginals

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["rag", "bio"], required=True)
    parser.add_argument("--run-label", default="scientific-agent-v1")
    args = parser.parse_args()
    if not args.run_label.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Use a simple run label")
    root = Path(__file__).resolve().parents[1]
    folder = root / ".research-base/frontier-refresh" / args.case
    output = folder / args.run_label
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("Existing scientific output is preserved; choose a new run label")
    selected = json.loads((folder / "selection.json").read_text(encoding="utf-8"))
    cid = selected["collection_id"]
    case_id = "rag-evaluation" if args.case == "rag" else "single-cell-perturbation"
    baseline_name = "rag-reviewed-design.md" if args.case == "rag" else "single-cell-reviewed-design.md"
    baseline = (root / "examples/research-usage" / baseline_name).read_text(encoding="utf-8")
    with ResearchBase(root / ".research-base/library") as base:
        targets = []
        if args.case == "rag":
            # Case-specific methods/metrics page locations, independently inspected.
            for fragment, pages in (("FRESCO:", [3, 4]), ("The Bottleneck Is Retrieval", [3, 4]), ("RARE:", [6, 7])):
                record = next(item for item in selected["records"] if fragment in item["title"])
                paper = base.get_paper(record["paper_id"])
                material = next((m for m in paper["materials"] if m["role"] == "main_text" and m["status"] == "available" and m["parse_status"] == "ready"), None)
                if not material:
                    raise ValueError("New original text is required before evaluating the scientific agent")
                targets.append({"paper_id": paper["paper_id"], "title": paper["title"], "material_id": material["material_id"],
                    "role": "main_text", "pages": pages})
        else:
            code = json.loads((folder / "code-materials.json").read_text(encoding="utf-8"))
            for work in code["works"]:
                wanted = ("docs/USAGE.md", "src/safedelta/adapter.py") if "split-control" in work["title"].lower() else (
                    "src/pie/configs/experiment/replogle_wdataset.yaml", "src/pie/configs/experiment/replogle_xdataset.yaml")
                for item in work["materials"]:
                    if item["repository_path"] not in wanted:
                        continue
                    targets.append({"paper_id": work["paper_id"], "title": work["title"], "material_id": item["material_id"],
                        "role": item["role"], "pages": [1], "repository_path": item["repository_path"], "source_commit": work["commit"]})
        focus = list(dict.fromkeys(item["paper_id"] for item in targets))
        freshness = base.prepare_context("evaluation", collection_id=cid, max_papers=1, max_chars=256)["freshness"]
        if freshness["needs_refresh"]:
            raise ValueError("The configured current search must really complete before evaluating this update")
        scoped = SelectedOriginals(base, cid, targets)
        task = ("请根据本轮已完成范围明确的最新文献检索与新增原始材料，修订旧研究方案。先复核宿主提供的真实检索范围、日期和论文版本日期。"
            "然后通过read_research_material实际阅读下面每个material_id列出的全部页；一次只选一个物理页/代码文本页，max_chars最多6000，"
            "遇到truncated必须原样续读continuation。代码和配置也是原始材料，但不能说成论文全文；不要执行代码。"
            "提交前调用check_research，声明实际使用的paper IDs。\n"
            "输出不超过1800中文字：1）保留旧方案中仍合理的控制；2）至少三项新增材料支持的具体改进，给出论文标题/物理页或代码路径及commit；"
            "3）逐项区分作者的设计/实证和我们的待验证实验；4）实际读取列表与尚缺的材料及覆盖限制。"
            "不能把一篇论文的页归给另一篇，不能声称没有返回过的页/文件已读；不能根据元数据摘要推方法细节。"
            "匹配查询分页已完成也不表示全领域覆盖；不要把别人的提升数字当本实验结果。请提出真正可执行的修订，避免只重述新论文题目。\n"
            "本轮允许读取的原件与范围（定位信息，非科学结论）：\n" + json.dumps(targets, ensure_ascii=False)
            + "\n旧独立复核方案（保留为对照，不作为本次原文已读证明）：\n" + baseline)
        from dotenv import load_dotenv
        load_dotenv(root / ".env.local", override=False)
        with ResearchAgent(scoped) as agent:
            result = agent.run(task, project_root=root / ".research-base/use-cases" / case_id / "research-project",
                focus_paper_ids=focus, research_stage="design", max_steps=20, max_tool_calls=40)
        (output / "agent-plan.md").write_text(result["answer"], encoding="utf-8")
        reads = result.get("research_usage", {}).get("reads", [])
        seen = {(read["material_id"], r.get("page")) for read in reads for r in read.get("ranges", [])}
        report = {"case_id": case_id, "completed_at": now(), "agent": result, "requested_originals": targets,
            "expected_material_pages_returned": all((item["material_id"], page) in seen for item in targets for page in item["pages"]),
            "new_original_read_calls": len(reads), "returned_characters": sum(read["returned_chars"] for read in reads),
            "scientific_review": "pending", "experiments_run": False, "baseline": "examples/research-usage/" + baseline_name}
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": args.case, "status": result["status"], "read_calls": len(reads),
            "characters": report["returned_characters"], "expected_pages_returned": report["expected_material_pages_returned"],
            "current_frontier_ready": result.get("research_usage", {}).get("current_frontier_ready")}), flush=True)

if __name__ == "__main__":
    main()
