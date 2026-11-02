"""Connect existing case versions only after an official publication relation."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--published-doi", required=True)
    parser.add_argument("--preprint-doi", required=True)
    parser.add_argument("--case", default="single-cell-perturbation")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    endpoint = "https://api.biorxiv.org/pubs/biorxiv/" + args.published_doi
    with httpx.Client(timeout=25) as client:
        response = client.get(endpoint)
        response.raise_for_status()
        records = response.json().get("collection") or []
    verified = next((r for r in records if r.get("published_doi", "").lower() == args.published_doi.lower()
                     and (r.get("preprint_doi") or r.get("biorxiv_doi") or "").lower() == args.preprint_doi.lower()), None)
    if not verified:
        raise ValueError("The official API did not confirm this version pair")
    baseline = json.loads((project / ".research-base/use-cases" / args.case / "run-result.json").read_text(encoding="utf-8"))
    with ResearchBase(project / ".research-base/library") as base:
        with base.store.connect() as db:
            row = db.execute("SELECT paper_id FROM identifiers WHERE namespace='doi' AND value=?", (args.preprint_doi.lower(),)).fetchone()
            pub = db.execute("SELECT paper_id FROM identifiers WHERE namespace='doi' AND value=?", (args.published_doi.lower(),)).fetchone()
        if not row or not pub:
            raise ValueError("Both actual versions must already exist in this library")
        preprint = base.get_paper(row[0])
        before = preprint["fulltext_available"]
        base.add_paper({"source": "biorxiv_relation", "source_id": args.preprint_doi.lower(), "doi": args.preprint_doi.lower(),
                        "title": verified["preprint_title"], "authors": preprint.get("authors", []), "version_kind": "preprint",
                        "landing_url": "https://doi.org/" + args.preprint_doi, "extra": {"published_doi": args.published_doi.lower()}})
        old_job = base.get_job(baseline["acquisition"]["job_id"])
        resumed = []
        for task in old_job["tasks"]:
            if task["paper_id"] == row[0] and task["state"] in ("waiting_browser", "needs_user", "failed"):
                base.resume_browser_task(task["task_id"])
                resumed.append(task["task_id"])
        after = base.get_paper(row[0])
        report = {"case_id": args.case, "verified_at": now(), "provider": "bioRxiv official publication relation API", "url": endpoint,
                  "preprint_doi": args.preprint_doi, "published_doi": args.published_doi,
                  "preprint_paper_id": row[0], "published_paper_id": pub[0], "stable_ids_preserved": row[0] != pub[0],
                  "before_preprint_work_fulltext": before, "after_preprint_work_fulltext": after["fulltext_available"],
                  "resumed_original_tasks": resumed, "counts_as_new_independent_work": False,
                  "original_acquisition": base.get_job(old_job["job_id"])}
        output = project / ".research-base/round3/verified-version-link.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in ("stable_ids_preserved", "before_preprint_work_fulltext", "after_preprint_work_fulltext", "counts_as_new_independent_work")}), flush=True)


if __name__ == "__main__":
    main()
