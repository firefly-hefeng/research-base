"""Checkpoint actual provider pagination for the two established research cases.

The current task scope is explicit. Earlier broad jobs and their gaps remain
intact; no hand-written completion time is inserted into the search ledger.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now

CASES = {
    "rag": {"case_id": "rag-evaluation", "collection_id": "col_2e45965895b9424fbe84",
        "query": "Text RAG evaluation, factuality, calibration and retrieval robustness",
        "source_queries": {
            "arxiv": '(all:"retrieval augmented generation" OR all:"retrieval-augmented generation") AND (ti:evaluation OR ti:evaluating OR ti:benchmark OR ti:benchmarking OR ti:factuality OR ti:faithfulness OR ti:calibration OR ti:calibrated OR ti:reranking OR ti:rerankers OR ti:"re-ranking")',
        }},
    "bio": {"case_id": "single-cell-perturbation", "collection_id": "col_482a3da9f9a4472cb6ae",
        "query": "Single-cell perturbation predictive modeling, benchmarking and generalization",
        "source_queries": {
            "europe_pmc": '(TITLE_ABS:"single-cell" OR TITLE_ABS:"single cell") AND (TITLE_ABS:perturbation OR TITLE_ABS:perturbations) AND (TITLE_ABS:predict* OR TITLE_ABS:benchmark* OR TITLE_ABS:generalization OR TITLE_ABS:generalisation)',
            "pubmed": '("single-cell"[Title/Abstract] OR "single cell"[Title/Abstract]) AND perturbation[Title/Abstract] AND (predict*[Title/Abstract] OR benchmark*[Title/Abstract] OR generalization[Title/Abstract] OR generalisation[Title/Abstract])',
        }},
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--include-acl", action="store_true")
    parser.add_argument("--run-label", default="v1")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    directory = project / ".research-base/frontier-refresh" / args.case
    directory.mkdir(parents=True, exist_ok=True)
    if not args.run_label.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Use a simple run label")
    filename = directory / (("discovery-acl-" + args.run_label + ".json") if args.include_acl else "discovery.json")
    definition = CASES[args.case]
    plan = {"query": definition["query"], "source_queries": deepcopy(definition["source_queries"]),
            "date_from": "2025-01-01", "date_to": "2026-11-03", "budget": {"max_requests": 10}}
    if args.include_acl:
        if args.case != "rag":
            raise ValueError("ACL supplement is for the RAG case")
        plan["source_queries"]["acl_anthology"] = json.dumps({
            "collections": ["2025.acl", "2025.emnlp", "2025.findings", "2026.acl", "2026.emnlp", "2026.findings", "2026.eacl"],
            "any": ["retrieval augmented", "retrieval-augmented", "RAG"], "all": []}, separators=(",", ":"))
    plan["sources"] = sorted(plan["source_queries"])
    with ResearchBase(project / ".research-base/library") as base:
        cid = definition["collection_id"]
        if filename.exists():
            report = json.loads(filename.read_text(encoding="utf-8"))
            job = base.get_job(report["job_id"])
        else:
            before = base.get_collection(cid)
            config = deepcopy(before["config"])
            config["discovery"] = plan
            config["freshness"] = {**config.get("freshness", {}), "max_age_days": 7}
            base.update_collection(cid, config=config)
            job = base.discover_papers(cid, **plan, request_id="frontier-refresh-20261103" + (("-acl-" + args.run_label) if args.include_acl else ""))
            report = {"case_id": definition["case_id"], "collection_id": cid, "started_at": now(),
                "job_id": job["job_id"], "plan": plan, "previous_config": before["config"], "checkpoints": []}
        for _ in range(args.rounds):
            if job["state"] in ("succeeded", "cancelled"):
                break
            if job["state"] in ("failed", "partial") and any(s.get("error") for s in job["progress"].get("sources", {}).values()):
                # Provider failures need an explicit later retry, not tight loops.
                break
            if job["state"] in ("paused", "partial", "failed"):
                base.resume_job(job["job_id"])
            job = base.run_worker(job["job_id"])
            checkpoint = {"at": now(), "state": job["state"], "requests": job["progress"].get("requests"),
                          "sources": job["progress"].get("sources", {}), "unique_papers": len(job["progress"].get("paper_ids", []))}
            report["checkpoints"].append(checkpoint)
            report["job"] = job
            report["freshness"] = base.prepare_context(definition["query"], collection_id=cid, max_papers=1, max_chars=256)["freshness"]
            filename.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": args.case, "state": job["state"], "requests": checkpoint["requests"],
                "unique_papers": checkpoint["unique_papers"], "source_states": {name: {key: state.get(key) for key in ("complete", "pages", "records", "total", "error")}
                    for name, state in checkpoint["sources"].items()}}, ensure_ascii=True), flush=True)
        if not filename.exists():
            report["job"] = job
            filename.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
