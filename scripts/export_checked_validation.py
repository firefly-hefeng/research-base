"""Export metadata and accepted plans; never export original-source bodies."""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from research_base.core import ResearchBase
from research_base.storage import now


def main():
    root = Path(__file__).resolve().parents[1]
    local = root/".research-base/reading-verification"
    output = root/"examples/checked-reading"
    output.mkdir(parents=True,exist_ok=True)
    runs = []
    for case in ("rag","bio"):
        report = json.loads((local/case/"finalized-v1/result.json").read_text(encoding="utf-8"))
        agent = report["agent"]
        usage = agent["research_usage"]
        if (agent["status"] != "completed" or not usage["ready_for_checked_synthesis"]
                or not usage["current_frontier_ready"] or not report["selected_pages_returned"]
                or agent.get("synthesis_review",{}).get("verdict") != "supported"
                or not agent.get("project_integration",{}).get("rules_loaded")):
            raise ValueError("Both cases must actually complete source, detail, final-draft and rule checks")
        text = (local/case/"finalized-v1/agent-plan.md").read_text(encoding="utf-8")
        if hashlib.sha256(text.encode()).hexdigest() != agent["synthesis_review"]["draft_sha256"]:
            raise ValueError("Published text must match the actual independently reviewed draft")
        (output/(case+"-plan.md")).write_bytes((local/case/"finalized-v1/agent-plan.md").read_bytes())
        history = []
        for label in ("checked-v1","checked-v2","checked-v3","guided-v1","guided-v2","finalized-v1"):
            path = local/case/label/"result.json"
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
                history.append({"run":label,"status":old["agent"]["status"],"read_calls":old["read_calls"],
                    "selected_pages_returned":old["selected_pages_returned"],"negative_control":old["negative_control"],
                    "final_draft_review":old["agent"].get("synthesis_review",{}).get("verdict"),
                    "orchestration":old["agent"].get("orchestration","agent_tool_loop")})
        runs.append({"case":case,"model":agent["model"],"orchestration":agent["orchestration"],
            "read_calls":report["read_calls"],"returned_characters":report["returned_characters"],
            "selected_pages_returned":report["selected_pages_returned"],"reading_ready":usage["reading_ready"],
            "detail_ready":usage["detail_ready"],"ready_for_checked_synthesis":usage["ready_for_checked_synthesis"],
            "current_frontier_ready":usage["current_frontier_ready"],"critical_check_count":len(usage["critical_checks"]),
            "detail_revisions":agent["synthesis_review"]["detail_revisions"],
            "negative_control":report["negative_control"],"final_draft_review":"supported",
            "approved_draft_utf8_sha256":agent["synthesis_review"]["draft_sha256"],
            "curation_actor":report.get("curation_actor"),"curation_corrections":report.get("curation_corrections",[]),
            "scientific_agent_read_calls":report.get("scientific_read_calls",report["read_calls"]),
            "verification_read_actor":report.get("verification_read_actor"),
            "rules_loaded":True,"targets":report["targets"],"run_history":history,
            "source_bodies_exported":False,"experiments_run":False})
    imported = json.loads((local/"background-imports.json").read_text(encoding="utf-8"))
    by_paper = {r["paper_id"]:r for r in imported}
    for filename in ("pie-browser-import.json","state-import.json"):
        item=json.loads((local/filename).read_text(encoding="utf-8"));by_paper[item["paper_id"]]=item
    de=json.loads((local/"bio-acquisition/declass-import-result.json").read_text(encoding="utf-8"))
    by_paper[de["paper_id"]]={"material_id":de["material"]["material_id"],"source_url":de["provenance"]["source_url"],
        "acquisition_route":"public_pinned_CC_BY_archive","source_commit":de["provenance"]["archive_commit"]}
    selection=json.loads((root/".research-base/frontier-refresh/bio/selection.json").read_text(encoding="utf-8"))
    inventory=[]
    with ResearchBase(root/".research-base/library") as base:
        for item in selection["records"]:
            paper=base.get_paper(item["paper_id"])
            receipt=by_paper.get(paper["paper_id"],{})
            material=next((m for m in paper["materials"] if m["material_id"]==receipt.get("material_id")),None)
            if material is None:
                material=next(m for m in paper["materials"] if m["role"]=="main_text" and m["format"]=="pdf" and m["status"]=="available")
            version=next(v for v in paper["versions"] if v["version_id"]==material["version_id"])
            inventory.append({"paper_id":paper["paper_id"],"title":paper["title"],"doi":item.get("doi"),"arxiv":item.get("arxiv"),
                "material_id":material["material_id"],"version_id":material["version_id"],"version_kind":version["kind"],
                "repository_revision":receipt.get("repository_revision"),"posted_date":receipt.get("posted_date"),
                "format":"pdf","role":"main_text","status":material["status"],"parse_status":material["parse_status"],
                "pages":material["details"].get("parsed_pages"),"bytes":Path(material["local_path"]).stat().st_size,
                "sha256":material["sha256"],"source_url":receipt.get("source_url") or material.get("source_url") or item.get("primary_url"),
                "source_commit":receipt.get("source_commit"),"acquisition_route":receipt.get("acquisition_route") or receipt.get("download_method") or ("background_browser" if receipt.get("execution_policy") else "existing_public_route"),
                "identity_confirmation_method":material["details"].get("identity_confirmation_method"),
                "text_extraction_caution":"Some form-XObject text was skipped by the parser; use original PDF for omitted content." if "with State" in paper["title"] else "Text extraction and whole-paper comprehension are not certified.",
                "original_published":False})
        frontier=base.get_frontier(selection["collection_id"])
        job=base.get_job("job_38658f38b83d451eb2ed")
        acquisition={"selected_originals":len(inventory),"selected_available":sum(r["status"]=="available" for r in inventory),
                     "acquisition_job_state":job["state"],"task_states":[t["state"] for t in job["tasks"]],
                     "curated_entries":len(frontier["paper_ids"]),"freshness":frontier["freshness"],"papers":inventory}
    summary={"validated_client_date":"2026-11-03","generated_at_utc":now(),"cases":runs,
        "single_cell_selected_originals_available":acquisition["selected_available"],"single_cell_selected_originals":len(inventory),
        "originals_full_abstracts_author_code_credentials_exported":False,"experiments_run":False,
        "limitations":["Selected queries, originals and critical questions are bounded; this is not exhaustive frontier coverage.",
            "Autonomous tool-loop runs that did not complete were blocked and retained in run_history; successful final runs used guided host orchestration.",
            "Independent reviewer calls used the same configured model and did miss errors. Codex source/metric curation and re-review corrected known errors; exact matches and accepted drafts are not scientific correctness certificates.",
            "Original PDF availability does not mean every figure or page has been read or extracted completely.",
            "No scientific benchmark performance, full reproduction or time-saving percentage was measured."]}
    (output/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    (output/"acquisition.json").write_text(json.dumps(acquisition,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"cases":len(runs),"selected_fulltexts":acquisition["selected_available"],"originals_exported":False}))


if __name__ == "__main__":
    main()
