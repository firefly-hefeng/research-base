"""Add measured broad-request costs from persisted jobs, without a new search."""
from __future__ import annotations
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase


def main():
    project = Path(__file__).resolve().parents[1]
    pointer = json.loads((project / ".research-base/round2/latest.json").read_text(encoding="utf-8"))
    directory = Path(pointer["directory"])
    reports = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    with ResearchBase(project / ".research-base/library") as base:
        for report in reports:
            current = report["current"]
            frontier = base.get_job(current["frontier_job_id"])
            ids = [frontier["progress"]["broad_job_id"]] + [j["job_id"] for j in current["following_broad_jobs"]]
            jobs = [base.get_job(jid) for jid in ids]
            current["broad_jobs"] = [{"job_id": j["job_id"], "state": j["state"], "budget": j["payload"]["budget"],
                                      "requests": j["progress"].get("requests", 0), "budget_overshoot": j["progress"].get("budget_overshoot", 0)} for j in jobs]
            current["broad_requests_total"] = sum(j["requests"] for j in current["broad_jobs"])
            (directory / (report["case_id"] + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "summary.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{"case": r["case_id"], "actual_broad_http": r["current"]["broad_requests_total"]} for r in reports]))


if __name__ == "__main__":
    main()
