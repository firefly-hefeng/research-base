"""Include relevance-screened papers in the original case and fetch originals.

Historical case reports remain unchanged. Non-selected entries stay visible in
the screening report; inclusion is not a claim that their full text was read.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv
from research_base.core import ResearchBase
from research_base.storage import now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["rag-evaluation", "single-cell-perturbation"], required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    load_dotenv(project / ".env.local", override=False)
    short = "rag" if args.case == "rag-evaluation" else "bio"
    screen = json.loads((project / f".research-base/round3/{short}-screening.json").read_text(encoding="utf-8"))
    output = project / f".research-base/round3/{short}-acquisition.json"
    selected = screen["recommended_include_ids"]
    if not selected:
        raise ValueError("No recommended papers; inspect the screening result")
    baseline = json.loads((project / ".research-base/use-cases" / args.case / "run-result.json").read_text(encoding="utf-8"))
    cid = baseline["collection_id"]
    with ResearchBase(project / ".research-base/library") as base:
        before = base.get_collection(cid)
        old_ids = [t["paper_id"] for t in baseline["acquisition"]["tasks"]]
        before_fulltext = {pid: base.get_paper(pid)["fulltext_available"] for pid in selected}
        base.set_membership(cid, selected, "included")
        for pid in selected:
            base.set_tags(pid, ["case:" + args.case, "citation-screened", "fulltext-review-pending"])
        job = base.acquire_papers(cid, paper_ids=selected, request_id="round3-screened:" + args.case)
        report = {"case_id": args.case, "collection_id": cid, "started_at": now(), "screening_file": str(project / f".research-base/round3/{short}-screening.json"),
                  "original_fixed_paper_ids": old_ids, "selected_paper_ids": selected,
                  "before_counts": before["counts"], "before_fulltext_available": before_fulltext,
                  "acquisition": job}
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for pid in selected:
            # Small independently checkpointed jobs share targets with the batch.
            one = base.acquire_papers(cid, paper_ids=[pid], request_id="round3-paper:" + args.case + ":" + pid)
            finished = base.run_worker(one["job_id"])
            report["acquisition"] = base.get_job(job["job_id"])
            report["updated_at"] = now()
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": args.case, "paper_id": pid, "state": finished["state"], "task_count": finished["task_count"]}), flush=True)
        report["materials"] = base.list_materials(collection_id=cid)
        report["after_counts"] = base.get_collection(cid)["counts"]
        report["after_selected_fulltext"] = {pid: base.get_paper(pid)["fulltext_available"] for pid in selected}
        report["completed_at"] = now()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": args.case, "selected": len(selected), "newly_available": sum(report["after_selected_fulltext"][p] and not before_fulltext[p] for p in selected),
                          "selected_available": sum(report["after_selected_fulltext"].values()), "report": str(output)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
