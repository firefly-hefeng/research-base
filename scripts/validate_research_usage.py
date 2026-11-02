"""Exercise research injection with the two existing local full-text use cases.

Default: deterministic checks on actual local originals. --live-model also asks
the configured optional research agent for a bounded, original-grounded design.
Outputs stay in ignored local storage; no paper text is exported publicly.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.integration import attach_project
from research_base.storage import now
from research_base.tools import ToolRegistry


def data(registry, name, args):
    response = registry.call(name, args)
    if "error" in response:
        raise RuntimeError(f"{name}: {response['error']['code']}")
    return response["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["rag-evaluation", "single-cell-perturbation"], required=True)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--run-label", default="injection-v2")
    args = parser.parse_args()
    if not args.run_label.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Use a simple run label")
    project = Path(__file__).resolve().parents[1]
    short = "rag" if args.case == "rag-evaluation" else "bio"
    source = json.loads((project / f".research-base/round3/{short}-reading-targets.json").read_text(encoding="utf-8"))
    baseline_dir = project / ".research-base/use-cases" / args.case
    baseline = json.loads((baseline_dir / "run-result.json").read_text(encoding="utf-8"))
    collection = baseline["collection_id"]
    targets = source["targets"]
    # These selections are case-specific original locations, not a general classifier.
    selected = [targets[1], targets[2]] if short == "rag" else [targets[0], targets[3]]
    profile = "machine_learning" if short == "rag" else "single_cell"
    output = project / ".research-base/research-usage" / args.run_label / args.case
    output.mkdir(parents=True, exist_ok=True)
    if (output / "validation.json").exists():
        raise ValueError("Choose a new run label; previous validation is preserved")
    with ResearchBase(project / ".research-base/library") as base:
        frontier = base.configure_frontier(collection, [item["paper_id"] for item in targets[:5]], profile=profile)
        binding = attach_project(baseline_dir / "research-project", "default", base.library_id, collection)
        task = ("设计RAG评估实验：根据当前原文核对校准方法的适用条件、数据划分，以及混合检索论文的指标定义和可比较条件。" if short == "rag"
                else "设计单细胞扰动预测评估实验：根据当前原文核对指标校准、正负对照、独立控制样本和泛化范围。")
        registry = ToolRegistry(base, collection_id=collection)
        opened = data(registry, "begin_research", {"task": task, "stage": "design", "profile": profile,
            "focus_paper_ids": [item["paper_id"] for item in selected]})
        sid = opened["session_id"]
        before = data(registry, "check_research", {"session_id": sid})
        assert not before["ready"]
        for item in selected:
            data(registry, "read_research_material", {"session_id": sid, "material_id": item["material_id"], "view": "original"})
        paths_only = data(registry, "check_research", {"session_id": sid})
        assert not paths_only["ready"]
        read_calls, partial_blocked = 0, False
        for item in selected:
            # A short first fragment deliberately exercises real continuation handling.
            result = data(registry, "read_research_material", {"session_id": sid,
                "material_id": item["material_id"], "pages": [item["pages"][0]], "max_chars": 1200})
            read_calls += 1
            if result.get("truncated"):
                partial_blocked = partial_blocked or not data(registry, "check_research", {"session_id": sid})["ready"]
            while result.get("continuation"):
                result = data(registry, "read_research_material", result["continuation"])
                read_calls += 1
        after = data(registry, "check_research", {"session_id": sid, "used_paper_ids": [item["paper_id"] for item in selected]})
        assert after["ready"] and partial_blocked
        report = {"case_id": args.case, "generated_at": now(), "profile": profile,
            "curated_papers": len(frontier["paper_ids"]), "binding_status": binding["status"],
            "metadata_only_blocked": not before["ready"], "paths_only_blocked": not paths_only["ready"],
            "partial_selected_range_blocked": partial_blocked, "original_read_calls": read_calls,
            "complete_selected_ranges_ready": after["ready"], "usage": after,
            "scientific_correctness_certified": False, "experiments_run": False}
        data(registry, "end_research", {"session_id": sid})
        if args.live_model:
            from dotenv import load_dotenv
            from selected_originals import SelectedOriginals
            load_dotenv(project / ".env.local", override=False)
            locations = [{"paper_id": item["paper_id"], "material_id": item["material_id"],
                "title": item["title"], "physical_page": item["pages"][0]} for item in selected]
            prompt = (task + "\n请使用宿主给出的研究会话。通过read_research_material实际读取下列两篇论文的指定物理页（一次只读一页，max_chars为6000，截断时续读），"
                "必要时继续其他页，然后check_research。只基于实际返回的原文，给出不超过1000中文字的实验设计修订："
                "每篇至少一项具体设计影响，注明论文名和物理页；精确区分指标、假设、作者实证和我们尚未验证的实验。"
                "说明哪些细节或前沿检索仍缺失。不得宣称整篇已读、当前全领域已覆盖或本轮已运行实验。不要输出机器路径。\n"
                + json.dumps(locations, ensure_ascii=False))
            scoped = SelectedOriginals(base, collection, selected)
            with ResearchAgent(scoped) as agent:
                result = agent.run(prompt, project_root=baseline_dir / "research-project", research_stage="design",
                    profile=profile, focus_paper_ids=[item["paper_id"] for item in selected], max_steps=12, max_tool_calls=24)
            (output / "agent-design.md").write_text(result["answer"], encoding="utf-8")
            report["agent"] = {key: result.get(key) for key in ("status", "model", "tool_calls", "usage", "research_usage", "project_integration")}
            report["scientific_review"] = "pending independent reading; usage success alone is insufficient"
            report["external_material_scope"] = [{"paper_id": item["paper_id"], "material_id": item["material_id"],
                "permitted_physical_pages": item["pages"]} for item in selected]
        (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("case_id", "metadata_only_blocked", "paths_only_blocked",
            "partial_selected_range_blocked", "complete_selected_ranges_ready", "original_read_calls")}
            | {"model_status": report.get("agent", {}).get("status")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
