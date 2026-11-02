"""Snapshot current original-material access and export the two fixed case sets.

Run after validate_round2.py and the host browser checks. Existing first-round
reports are not rewritten. Bundles are created in a fresh timestamped folder.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now


def main():
    project = Path(__file__).resolve().parents[1]
    pointer_path = project / ".research-base/round2/latest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    directory = Path(pointer["directory"])
    reports = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    artifacts = {"checked_at": now(), "cases": []}
    with ResearchBase(project / ".research-base/library") as base:
        for report in reports:
            case_id = report["case_id"]
            baseline = json.loads((project / ".research-base/use-cases" / case_id / "run-result.json").read_text(encoding="utf-8"))
            job = base.get_job(baseline["acquisition"]["job_id"])
            papers = [base.get_paper(t["paper_id"]) for t in job["tasks"]]
            bundle = base.export_collection(baseline["collection_id"], directory / "bundles" / case_id)
            result = {"case_id": case_id, "original_collection_id": baseline["collection_id"],
                      "discovery_collection_id": report["collection_id"], "bundle": bundle,
                      "fulltext_count": sum(p["fulltext_available"] for p in papers), "total": len(papers),
                      "job_state": job["state"],
                      "materials": [{"paper_id": p["paper_id"], "title": p["title"], "doi": p.get("doi"),
                                     "related_paper_ids": p.get("related_paper_ids", []),
                                     "materials": [{k: m.get(k) for k in ("material_id", "role", "format", "status", "version_id", "local_path", "text_path", "pages_path")} for m in p["materials"]]} for p in papers],
                      "unresolved": [{"paper_id": t["paper_id"], "task_id": t["task_id"], "state": t["state"], "result": t["result"]} for t in job["tasks"] if t["state"] != "succeeded"]}
            artifacts["cases"].append(result)
    output = directory / "material-access.json"
    output.write_text(json.dumps(artifacts, ensure_ascii=False, indent=2), encoding="utf-8")
    pointer["material_access"] = str(output)
    pointer["acquisition_route_proof"] = str(project / ".research-base/round2/acquisition-route-proof.json")
    pointer_path.write_text(json.dumps(pointer, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"material_access": str(output), "cases": [{"case": c["case_id"], "fulltext": c["fulltext_count"], "total": c["total"], "bundle": c["bundle"]["output_dir"]} for c in artifacts["cases"]]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
