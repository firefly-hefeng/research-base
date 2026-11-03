"""Run revised discovery suggestions against known misses, preserving old jobs.

This is a regression after observing missed papers, not a blind recall estimate.
It neither marks existing candidates included nor rewrites collection freshness
configuration. Every source page, pause and failure remains in the actual job.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.discovery_plans import frontier_query_plans
from research_base.storage import atomic_write, loads, now
from research_base.workflows import seed_matches, title_key


BASELINES = {"rag": "job_43124ea807f54b75a28a", "bio": "job_1617eeaae3be415f90b7"}


def snapshot(base, job_id, leads):
    with base.store.connect() as db:
        hits = [(r["paper_id"], loads(r["identity"])) for r in db.execute(
            "SELECT paper_id,identity FROM discovery_hits WHERE job_id=? AND stage='broad'", (job_id,))]
        relation_rows = [dict(r) for r in db.execute("SELECT * FROM paper_relations")]
    ids = {pid for pid, _ in hits}
    results = []
    for lead in leads:
        strong = {k: lead[k] for k in ("title", "doi", "arxiv") if lead.get(k)}
        exact = sorted({pid for pid, identity in hits if seed_matches(strong, identity)})
        selected = lead["paper_id"]
        related = {selected}
        for relation in relation_rows:
            if selected in (relation["published_paper_id"], relation["preprint_paper_id"]):
                related.update((relation["published_paper_id"], relation["preprint_paper_id"]))
        related_hits = sorted((related - {selected}) & ids)
        same_title = sorted({pid for pid, identity in hits if title_key(identity.get("title")) == title_key(lead["title"])} - set(exact) - set(related_hits))
        results.append({"title": lead["title"], "selected_paper_id": selected,
            "identity": strong, "selected_id_hit": selected in ids, "exact_identity_hit_ids": exact,
            "verified_relation_hit_ids": related_hits, "same_title_unverified_ids": same_title,
            "status": "found_exact" if exact or selected in ids else "found_verified_relation" if related_hits else "identity_review_required" if same_title else "not_returned"})
    return {"job_id": job_id, "unique_stored_hit_ids": len(ids), "leads": results,
            "notice": "Known-lead regression only. Exact identifiers, explicit provider relations and same-title candidates are separate; no title-only merge or blind field recall claim."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=BASELINES, required=True)
    parser.add_argument("--plan-index", type=int)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--budget", type=int, default=12)
    parser.add_argument("--date-from", default="2025-01-01")
    parser.add_argument("--date-to", default="2026-11-03")
    parser.add_argument("--run-label", default="revised-v1")
    parser.add_argument("--resume-job", help="Resume an existing matching job in a new report after a host interruption")
    parser.add_argument("--biorxiv-category", help="Explicit category-only supplemental scope, not the full suggested category plan")
    args = parser.parse_args()
    if not args.run_label.replace("-", "").replace("_", "").isalnum() or not 1 <= args.rounds <= 100 or not 1 <= args.budget <= 1000:
        raise ValueError("Use a simple label and bounded rounds/budget")
    root = Path(__file__).resolve().parents[1]
    folder = root / ".research-base/core-upgrade/discovery"
    folder.mkdir(parents=True, exist_ok=True)
    selection = json.loads((root / ".research-base/frontier-refresh" / args.case / "selection.json").read_text(encoding="utf-8"))
    plans = frontier_query_plans("rag" if args.case == "rag" else "single_cell", date_from=args.date_from, date_to=args.date_to)
    chosen = range(len(plans)) if args.plan_index is None else [args.plan_index]
    with ResearchBase(root / ".research-base/library") as base:
        for index in chosen:
            plan = deepcopy(plans[index]); plan["budget"] = {"max_requests": args.budget}
            if args.biorxiv_category:
                if plan["sources"] != ["biorxiv"]:
                    raise ValueError("A category override only applies to the explicit bioRxiv feed plan")
                native = json.loads(plan["source_queries"]["biorxiv"])
                native["categories"] = [args.biorxiv_category]
                plan["source_queries"]["biorxiv"] = json.dumps(native, separators=(",", ":"))
            path = folder / f"{args.case}-{args.run_label}-plan{index}.json"
            if path.exists():
                report = json.loads(path.read_text(encoding="utf-8"))
                if report["plan"] != plan:
                    raise ValueError("Preserve existing run; choose another label when changing its plan")
                job = base.get_job(report["job_id"])
            else:
                report = {"case": args.case, "started_at": now(), "plan": plan,
                    "scope": "supplemental_category_or_date_plan" if args.biorxiv_category or args.date_from != "2025-01-01" or args.date_to != "2026-11-03" else "revised_full_declared_window",
                    "evaluation_design": "Known-miss regression after observing the old results; not blind discovery benchmarking.",
                    "baseline": snapshot(base, BASELINES[args.case], selection["records"]), "checkpoints": [], "observed_http_attempts": 0}
                if args.resume_job:
                    if len(list(chosen)) != 1:
                        raise ValueError("Resume exactly one explicit plan")
                    job = base.get_job(args.resume_job)
                    if job["collection_id"] != selection["collection_id"] or job["payload"] != plan:
                        raise ValueError("Resume job must exactly match the case and declared plan")
                    report["resumed_existing_job"] = {"job_id": job["job_id"], "initial_progress": deepcopy(job["progress"]),
                        "notice": "Earlier host output was interrupted; observed_http_attempts counts only this resumed report, not historical attempts."}
                else:
                    job = base.discover_papers(selection["collection_id"], **plan,
                        request_id=f"discovery-regression-{args.case}-{args.run_label}-{index}")
                report["job_id"] = job["job_id"]
            for _ in range(args.rounds):
                if job["state"] in ("succeeded", "cancelled"):
                    break
                if any(s.get("error") for s in job["progress"].get("sources", {}).values()):
                    break
                if job["state"] in ("paused", "partial", "failed"):
                    base.resume_job(job["job_id"])
                before = base.sources.request_count
                job = base.run_worker(job["job_id"])
                report["observed_http_attempts"] += base.sources.request_count - before
                checkpoint = {"at": now(), "state": job["state"], "budget_units": job["progress"].get("requests"),
                    "sources": {name: {key: value.get(key) for key in ("complete", "pages", "records", "total", "error", "warnings")}
                        for name, value in job["progress"].get("sources", {}).items()}}
                report["checkpoints"].append(checkpoint)
                report["job"] = job
                report["regression"] = snapshot(base, job["job_id"], selection["records"])
                atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps({"case": args.case, "plan_index": index, "state": job["state"],
                    "http_attempts": report["observed_http_attempts"], "unique_hit_ids": report["regression"]["unique_stored_hit_ids"],
                    "lead_states": [{"title": r["title"], "status": r["status"]} for r in report["regression"]["leads"]]}, ensure_ascii=True), flush=True)
            report["job"] = job
            report["regression"] = snapshot(base, job["job_id"], selection["records"])
            atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
