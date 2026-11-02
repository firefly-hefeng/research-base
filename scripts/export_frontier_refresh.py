"""Export public-safe search scope, references and observed research usage."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now

def main():
    root = Path(__file__).resolve().parents[1]
    local = root / ".research-base/frontier-refresh"
    output = root / "examples/frontier-refresh"
    output.mkdir(parents=True, exist_ok=True)
    summary = {"validated_date": "2026-11-03", "generated_at": now(), "original_materials_exported": False,
               "experiments_run": False, "cases": []}
    plans, bibliography = [], []
    with ResearchBase(root / ".research-base/library") as base:
        for case, filename in (("rag", "discovery-acl-corrected.json"), ("bio", "discovery.json")):
            discovery = json.loads((local / case / filename).read_text(encoding="utf-8"))
            selected = json.loads((local / case / "selection.json").read_text(encoding="utf-8"))
            job = base.get_job(discovery["job_id"])
            if job["state"] != "succeeded":
                raise ValueError("An incomplete query cannot be exported as current checked coverage")
            frontier = base.get_frontier(selected["collection_id"])
            agent = json.loads((output / (case + "-review.json")).read_text(encoding="utf-8"))
            sources = {name: {key: state.get(key) for key in ("complete", "pages", "records", "total", "error")}
                       for name, state in job["progress"]["sources"].items()}
            plans.append({"case_id": discovery["case_id"], "plan": discovery["plan"], "sources": sources,
                "job_id": job["job_id"], "state": job["state"], "freshness": frontier["freshness"],
                "reported_budget_units": job["progress"].get("requests"),
                "counter_note": "Persisted job requests count provider-page budget units; cached ACL pages consume units without another HTTP attempt. Actual total HTTP attempts were not separately recorded for this run.",
                "records_unit": "Stored paper identifiers; retained archive/publication records are not automatically one independent study."})
            records = []
            for item in selected["records"]:
                record = {key: item[key] for key in ("paper_id", "title", "doi", "arxiv", "primary_url", "task_relevance", "date", "first_submitted",
                    "latest_arxiv_revision", "publication_month", "reading_caution", "scope_limit", "decision", "identity_selection", "alternate_archive_records_retained") if key in item}
                if item.get("paper_id"):
                    paper = base.get_paper(item["paper_id"])
                    record.update(fulltext_available=paper["fulltext_available"], text_searchable=paper["text_searchable"],
                        publication_history=paper["publication_history"],
                        materials=[{"material_id": m["material_id"], "role": m["role"], "format": m["format"], "status": m["status"],
                                    "parse_status": m["parse_status"], "sha256": m.get("sha256")}
                                   for m in paper["materials"] if m["status"] == "available"])
                records.append(record)
            bibliography.append({"case_id": discovery["case_id"], "selection_method": "Independent primary-record relevance review, then exact identifier selection; other candidates remain unreviewed.",
                                 "papers": records})
            available = sum(base.get_paper(pid)["fulltext_available"] for pid in selected["selected_paper_ids"])
            entry = {"case_id": discovery["case_id"], "provider_records": {name: state["records"] for name, state in sources.items()},
                "unique_stored_ids_returned": len(job["progress"]["paper_ids"]), "selected_recent_works": len(selected["selected_paper_ids"]),
                "selected_with_available_main_text": available, "curated_entries": len(frontier["paper_ids"]),
                "matching_scope_fresh": not frontier["freshness"]["needs_refresh"],
                "local_research_agent_read_calls": agent["read_calls"],
                "local_research_agent_returned_characters": agent.get("returned_characters"),
                "usage_ready": agent.get("usage_check_passed", agent.get("usage_ready")),
                "current_frontier_ready": agent["current_frontier_ready"], "external_model_called_for_new_materials": False}
            if case == "bio":
                code = json.loads((local / case / "code-materials.json").read_text(encoding="utf-8"))
                entry.update(author_code_works_available=len(code["works"]), author_code_materials_available=sum(len(w["materials"]) for w in code["works"]))
            model_path = local / case / "deepseek-approved-v1/result.json"
            if model_path.is_file():
                model_run = json.loads(model_path.read_text(encoding="utf-8"))
                if model_run.get("explicit_user_material_transfer_authorization") is not True:
                    raise ValueError("The actual model run must carry the explicit transfer authorization record")
                model = model_run["agent"]
                usage = model.get("research_usage", {})
                review_path = output / (case + "-deepseek-review.json")
                review_state = "pending"
                if review_path.is_file():
                    review = json.loads(review_path.read_text(encoding="utf-8"))
                    if (review.get("case_id") != discovery["case_id"]
                            or review.get("model_read_calls", review.get("read_calls")) != model_run["new_original_read_calls"]
                            or review.get("model_returned_characters", review.get("returned_characters")) != model_run["returned_characters"]):
                        raise ValueError("Independent review does not match this actual model run")
                    corrections = (review.get("corrections")
                                   or review.get("assessment", {}).get("scientific_corrections_required"))
                    review_state = "completed_with_corrections" if corrections else "completed"
                entry["external_model_called_for_new_materials"] = True
                entry["deepseek_validation"] = {"model": model["model"], "status": model["status"],
                    "explicit_transfer_authorization": True,
                    "read_calls": model_run["new_original_read_calls"],
                    "returned_characters": model_run["returned_characters"],
                    "expected_material_pages_returned": model_run["expected_material_pages_returned"],
                    "usage_ready": usage.get("ready"), "current_frontier_ready": usage.get("current_frontier_ready"),
                    "pending_continuations": len(usage.get("pending_continuations", [])),
                    "scientific_review": review_state}
            summary["cases"].append(entry)
    summary["limitations"] = ["Freshness covers declared queries, collection snapshots and date windows only; index lag, unindexed sources and unscreened candidates remain.",
        "OpenAlex supplementary probes were rate limited; this source is not claimed completed in either declared plan.",
        "Two October single-cell manuscripts were read through pinned author code, not manuscript PDFs; unresolved full texts remain visible.",
        ("DeepSeek read only the selected necessary material ranges after explicit user authorization; passing access checks does not validate scientific interpretation."
         if all(c.get("external_model_called_for_new_materials") for c in summary["cases"])
         else "The external-model record is incomplete; retain per-case status and do not claim a pending transfer was performed."),
        "No benchmark performance, training, independent full reproduction, time-saving percentage or exhaustive field coverage is claimed."]
    for name, value in (("summary.json", summary), ("search-plans.json", plans), ("references.json", bibliography)):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(summary["cases"]), "exported_originals": False,
                      "matching_scopes_fresh": [c["matching_scope_fresh"] for c in summary["cases"]]}))

if __name__ == "__main__":
    main()
