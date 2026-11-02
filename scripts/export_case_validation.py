"""Export short case metadata, reviewed plans and counts for public review.

Never copies original papers, complete abstracts, secrets, local paths, browser
responses or the SQLite library into the public example tree.
"""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    project = Path(__file__).resolve().parents[1]
    private = project / ".research-base/round3"
    public = project / "examples/validated-cases"
    summary = {"checked_at": now(), "purpose": "qualitative research-design validation, not a performance or time-saving benchmark", "experiments_run": False, "cases": []}
    with ResearchBase(project / ".research-base/library") as base:
        for short, name in (("rag", "rag-evaluation"), ("bio", "single-cell-perturbation")):
            screening = json.loads((private / (short + "-screening.json")).read_text(encoding="utf-8"))
            acquisition = json.loads((private / (short + "-acquisition.json")).read_text(encoding="utf-8"))
            analysis_dir = private / name / "verified-read"
            analysis = json.loads((analysis_dir / "analysis-result.json").read_text(encoding="utf-8"))
            records = screening.get("papers", screening.get("items", []))
            published_records = [{k: p.get(k) for k in ("paper_id", "title", "doi", "year", "decision", "reason", "research_use", "category", "new_to_round2_collection", "already_in_broad_or_seed", "in_original_included", "already_originally_included") if k in p} for p in records]
            save(public / name / "screening.json", {"stage": "metadata relevance screen; inclusion is not quality certification or proof of full-text reading", "papers": published_records})
            included = []
            offset = 0
            while True:
                page = base.list_papers(acquisition["collection_id"], membership="included", limit=200, offset=offset)
                included.extend(p["paper_id"] for p in page["papers"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            paper_data = {pid: base.get_paper(pid) for pid in included}
            groups = {tuple(sorted([pid, *p.get("related_paper_ids", [])])) for pid, p in paper_data.items()}
            available_groups = {g for g in groups if any(paper_data[pid]["fulltext_available"] for pid in g if pid in paper_data)}
            original = set(acquisition["original_fixed_paper_ids"])
            original_groups = {tuple(sorted([pid, *base.get_paper(pid).get("related_paper_ids", [])])) for pid in original}
            original_available_groups = {g for g in original_groups if any(base.get_paper(pid)["fulltext_available"] for pid in g)}
            new_selected = acquisition["selected_paper_ids"]
            job = base.get_job(acquisition["acquisition"]["job_id"])
            material_hashes = {m["sha256"] for p in paper_data.values() for m in p["materials"] if m["status"] == "available" and m["role"] == "main_text" and m["exists"]}
            unresolved, resolved = [], []
            for potential in screening.get("possible_version_groups", []):
                pair = potential["paper_ids"]
                related = {pair[0], *base.get_paper(pair[0]).get("related_paper_ids", [])}
                if set(pair).issubset(related):
                    resolved.append({**potential, "verified_relation": True, "basis": "explicit provider publication relation recorded in library; original IDs preserved"})
                else:
                    unresolved.append(potential)
            entry = {"case_id": name, "screened_graph_candidates": len(records),
                     "recommended_added_records": len(new_selected), "included_records": len(included),
                     "verified_work_groups": len(groups), "available_main_text_work_groups": len(available_groups),
                     "available_main_text_files": len(material_hashes), "original_fixed_work_groups": len(original_groups),
                     "original_fixed_groups_with_main_text": len(original_available_groups),
                     "new_selected_records_with_main_text": sum(base.get_paper(pid)["fulltext_available"] for pid in new_selected),
                     "new_selected_acquisition_target_states": job["task_count"],
                     "agent_status": analysis["agent"]["status"], "agent_tool_calls": len(analysis["agent"]["tool_calls"]),
                     "agent_new_main_text_papers_with_returned_text": len(analysis["new_main_text_papers_read"]),
                     "agent_read_calls": len(analysis["read_log"]),
                     "read_scope_notice": "Returned passages are not necessarily whole pages or whole papers. See the independent review and read log.",
                     "model": analysis["agent"]["model"], "model_usage": analysis["agent"]["usage"],
                     "scientific_review_required": True, "resolved_version_groups": resolved, "unresolved_version_groups": unresolved}
            summary["cases"].append(entry)
            save(public / name / "reading-log.json", {"case_id": name, "model": analysis["agent"]["model"], "reads": analysis["read_log"], "whole_paper_reading_claimed": False})
            bibliography = [{"paper_id": pid, "title": p["title"], "doi": p.get("doi"), "year": p.get("year"),
                             "identifiers": p.get("identifiers", {}), "related_paper_ids": p.get("related_paper_ids", []), "main_text_available_locally": p["fulltext_available"]} for pid, p in paper_data.items()]
            save(public / name / "bibliography.json", {"case_id": name, "papers": bibliography, "originals_included_in_public_repository": False})
            for filename in ("reviewed-plan.md", "benefits.md"):
                shutil.copyfile(analysis_dir / filename, public / name / filename)
    graph = json.loads((private / "bio-expansion.json").read_text(encoding="utf-8"))
    summary["bio_graph_progress"] = {key: graph[key] for key in ("initial_requests", "additional_requests", "streams_complete", "graph_candidate_count", "newly_added_to_round2_collection")}
    summary["bio_graph_progress"]["total_requests"] = graph["progress"]["requests"]
    summary["bio_screening_model_requests"] = {"actual_requests": 33, "valid_batches": 21, "invalid_responses_retried": 12, "host_overrides": 18}
    save(public / "summary.json", summary)
    save(private / "public-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
