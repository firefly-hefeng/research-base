"""Have the configured research agent revise a case using actual original reads.

The previous plans remain intact. A compact read log records which original
pages the tool returned, without publishing their text or local file paths.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv
from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.storage import now


class ReadAudit:
    def __init__(self, base, original_ids):
        self.base = base
        self.original_ids = set(original_ids)
        self.reads = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def read_material(self, **arguments):
        result = self.base.read_material(**arguments)
        material = result.get("material") or {}
        pages = result.get("pages") or []
        chars = sum(len(page.get("text", "")) for page in pages)
        self.reads.append({"material_id": arguments["material_id"], "paper_id": material.get("paper_id"),
                           "role": material.get("role"), "format": material.get("format"),
                           "view": arguments.get("view", "text"), "requested_pages": arguments.get("pages"),
                           "returned_pages": [p["page"] for p in pages], "char_offset": arguments.get("char_offset", 0),
                           "returned_characters": chars, "content_returned": chars > 0,
                           "new_relative_to_original_included": material.get("paper_id") not in self.original_ids})
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["rag-evaluation", "single-cell-perturbation"], required=True)
    parser.add_argument("--run-label", default="initial", help="Separate run folder; preserves prior model output")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    load_dotenv(project / ".env.local", override=False)
    short = "rag" if args.case == "rag-evaluation" else "bio"
    if not args.run_label.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Use a simple run label")
    output = project / ".research-base/round3" / args.case
    if args.run_label != "initial":
        output = output / args.run_label
    output.mkdir(parents=True, exist_ok=True)
    if (output / "agent-plan.md").exists():
        raise ValueError("Run folder already has a plan; choose a new --run-label")
    baseline_dir = project / ".research-base/use-cases" / args.case
    baseline = json.loads((baseline_dir / "run-result.json").read_text(encoding="utf-8"))
    old_plan = (baseline_dir / "research-plan.md").read_text(encoding="utf-8")
    (output / "previous-plan.md").write_text(old_plan, encoding="utf-8")
    targets = json.loads((project / f".research-base/round3/{short}-reading-targets.json").read_text(encoding="utf-8"))
    navigation = [{k: t[k] for k in ("paper_id", "material_id", "title", "pages") if k in t} for t in targets["targets"]]
    with ResearchBase(project / ".research-base/library") as base:
        original_ids = [t["paper_id"] for t in baseline["acquisition"]["tasks"]]
        original_ids = sorted({linked for pid in original_ids for linked in [pid, *base.get_paper(pid).get("related_paper_ids", [])]})
        audited = ReadAudit(base, original_ids)
        task = ("请基于本项目当前可取得的原始材料更新下面的研究方案。先实际调用read_material读取至少三篇新增论文的方法、评估或限制部分，"
                "必要时用get_paper核对身份。每次read_material只选一个物理页，最多6000字符；truncated时用返回continuation原样续读，换页时char_offset重置为0。"
                "至少完整读三篇新论文的关键方法或限制页，不仅阅读摘要或定位提示。只有实际返回的文本可作证据；没有读的页不能引用为亲读。"
                "可以自主选择最有用的材料；某目标不可读时明确缺口，不能声称已读。\n"
                "输出一份不超过2400中文字的可执行方案，保留已有可靠控制，说明具体基线、数据划分、指标、成本和失败判据。"
                "单独列出相对旧方案的3至6项变化：每项写新增原文支持的具体改动、对应论文标题/DOI与物理页码，或明确只是确认原方案。"
                "区分作者的结果/理论假设与我们的待验证实验；指标名称和定义不得混用。没有跑实验，不报告本系统新性能或排名，不保证全领域覆盖。"
                "未取得的全文只能说明未知，不能从摘要补出方法细节。只用标题/DOI引用，不输出本机路径。\n"
                "下面是同机原件的导航提示，不是科学结论；请自行读取核对：\n" + json.dumps(navigation, ensure_ascii=False)
                + "\n旧方案（比较基线，不是新增原文证据）：\n" + old_plan)
        with ResearchAgent(audited) as agent:
            answer = agent.run(task, project_root=baseline_dir / "research-project", max_steps=16, max_tool_calls=40)
        (output / "agent-plan.md").write_text(answer["answer"], encoding="utf-8")
        read_papers = {r["paper_id"] for r in audited.reads if r["content_returned"]}
        new_read = {r["paper_id"] for r in audited.reads if r["content_returned"] and r["new_relative_to_original_included"] and r["role"] == "main_text"}
        report = {"case_id": args.case, "collection_id": baseline["collection_id"], "completed_at": now(),
                  "agent": {k: answer.get(k) for k in ("status", "model", "usage", "tool_calls", "limit_reason", "project_integration")},
                  "read_log": audited.reads, "unique_papers_with_text_returned": sorted(read_papers),
                  "new_main_text_papers_read": sorted(new_read), "minimum_new_read_requirement_met": len(new_read) >= 3,
                  "previous_plan_path": str(output / "previous-plan.md"), "agent_plan_path": str(output / "agent-plan.md"),
                  "scientific_review": "pending; agent output is preserved separately from reviewed plan",
                  "experiments_run": False}
        (output / "analysis-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": args.case, "status": answer["status"], "tool_calls": len(answer["tool_calls"]),
                          "read_calls": len(audited.reads), "new_main_texts_read": len(new_read), "minimum_met": len(new_read) >= 3,
                          "plan_characters": len(answer["answer"])}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
