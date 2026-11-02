"""Continue the saved single-cell citation streams and export their real records.

Does not alter inclusion decisions or download full text. Each resumed worker
keeps the original per-run eight-request budget and its saved provider cursors.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now
from research_base.tools import ToolRegistry


def checked(registry, name, arguments):
    result = registry.call(name, arguments)
    if "error" in result:
        raise RuntimeError(json.dumps(result["error"], ensure_ascii=True))
    return result["data"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-additional-requests", type=int, default=80)
    parser.add_argument("--round2-summary", type=Path, help="Frozen report, otherwise use the latest local round-2 run")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = project / ".research-base/round3/bio-expansion.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path = args.round2_summary
    if summary_path is None:
        latest = json.loads((project / ".research-base/round2/latest.json").read_text(encoding="utf-8"))
        summary_path = Path(latest["summary"])
    round2 = next(r for r in json.loads(summary_path.read_text(encoding="utf-8")) if r["case_id"] == "single-cell-perturbation")
    original = json.loads((project / ".research-base/use-cases/single-cell-perturbation/run-result.json").read_text(encoding="utf-8"))
    job_id = round2["current"]["citation_job_id"]
    with ResearchBase(project / ".research-base/library") as base:
        registry = ToolRegistry(base, allow_writes=True)
        job = checked(registry, "get_job", {"job_id": job_id})
        report = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {
            "case_id": "single-cell-perturbation", "started_at": now(),
            "original_collection_id": original["collection_id"], "discovery_collection_id": round2["collection_id"],
            "citation_job_id": job_id, "initial_requests": job["progress"].get("requests", 0),
            "initial_graph_candidate_count": len(job["progress"].get("paper_ids", [])),
            "runs": [], "criterion": "all six saved one-hop OpenAlex streams complete without stream errors; not exhaustive topic coverage"}
        while not all(s["complete"] for s in job["progress"]["streams"]):
            additional = job["progress"].get("requests", 0) - report["initial_requests"]
            # A page can involve up to a seed lookup plus a data request. Reserve
            # the whole original run before starting; report actual HTTP usage.
            if additional + job["payload"]["budget"]["max_requests"] > args.max_additional_requests:
                report["stop_reason"] = "additional_request_budget"
                break
            before = job["progress"].get("requests", 0)
            checked(registry, "resume_job", {"job_id": job_id})
            job = checked(registry, "run_worker", {"job_id": job_id})
            run = {"checked_at": now(), "state": job["state"], "requests": job["progress"].get("requests", 0) - before,
                   "complete_streams": sum(s["complete"] for s in job["progress"]["streams"]),
                   "stream_count": len(job["progress"]["streams"]), "candidate_count": len(job["progress"].get("paper_ids", [])),
                   "streams": job["progress"]["streams"]}
            report["runs"].append(run)
            report["additional_requests"] = job["progress"].get("requests", 0) - report["initial_requests"]
            report["latest_job_state"] = job["state"]
            report["checked_at"] = now()
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: run[k] for k in ("state", "requests", "complete_streams", "stream_count", "candidate_count")}), flush=True)
            if run["requests"] == 0:
                report["stop_reason"] = "no_progress"
                break
            if any(s.get("error") for s in job["progress"]["streams"]):
                report["stop_reason"] = "source_error_requires_review"
                break
        frontier = base.get_job(round2["current"]["frontier_job_id"])
        broad_jobs = [frontier["progress"]["broad_job_id"]] + [j["job_id"] for j in round2["current"]["following_broad_jobs"]]
        hit_jobs = broad_jobs + [frontier["job_id"]]
        with base.store.connect() as db:
            baseline_ids = {r[0] for r in db.execute("SELECT DISTINCT paper_id FROM discovery_hits WHERE job_id IN (" + ",".join("?" for _ in hit_jobs) + ")", hit_jobs)}
            originally_included = {t["paper_id"] for t in original["acquisition"]["tasks"]}
        records = []
        for pid in job["progress"].get("paper_ids", []):
            paper = base.get_paper(pid, include_raw=True)
            records.append({**{k: paper.get(k) for k in ("paper_id", "title", "authors", "year", "doi", "identifiers", "abstract", "landing_url", "sources", "related_paper_ids")},
                            "already_in_broad_or_seed": pid in baseline_ids,
                            "already_originally_included": pid in originally_included,
                            "fulltext_available": paper["fulltext_available"]})
        report.update(checked_at=now(), latest_job_state=job["state"], progress=job["progress"],
                      additional_requests=job["progress"].get("requests", 0) - report["initial_requests"],
                      streams_complete=all(s["complete"] and not s.get("error") for s in job["progress"]["streams"]),
                      graph_candidate_count=len(records), newly_added_to_round2_collection=sum(not p["already_in_broad_or_seed"] for p in records),
                      original_included_ids=sorted(originally_included), baseline_broad_seed_ids=sorted(baseline_ids), candidates=records)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in ("streams_complete", "additional_requests", "graph_candidate_count", "newly_added_to_round2_collection", "latest_job_state")}), flush=True)


if __name__ == "__main__":
    main()
