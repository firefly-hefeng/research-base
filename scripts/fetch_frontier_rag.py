"""Fetch independently verified RAG originals through declared public PDF URLs.

Downloaded bytes still pass the library's file/identity/version validation.
This host transport does not mark an unresolved strict target as successful.
"""
import json
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now

def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / ".research-base/frontier-refresh/rag"
    selected = json.loads((folder / "selection.json").read_text(encoding="utf-8"))
    leads = json.loads((folder.parent / "rag-independent-discovery.json").read_text(encoding="utf-8"))["papers"]
    by_title = {item["title"]: item for item in leads}
    destination = folder / "downloads"
    destination.mkdir(parents=True, exist_ok=True)
    report = {"started_at": now(), "routes": [], "transport": "public_http_to_trusted_local_import"}
    with ResearchBase(root / ".research-base/library") as base, httpx.Client(timeout=45, follow_redirects=True) as client:
        job = base.acquire_papers(selected["collection_id"], selected["selected_paper_ids"], request_id="frontier-refresh-20261103")
        for record in selected["records"]:
            if record.get("decision") != "included_for_original_review":
                continue
            pid = record["paper_id"]
            paper = base.get_paper(pid)
            if paper["fulltext_available"]:
                continue
            for url in by_title[record["title"]]["pdf_urls"]:
                route = {"paper_id": pid, "url": url}
                try:
                    response = client.get(url)
                    route.update(http_status=response.status_code, bytes=len(response.content))
                    if not response.is_success or not response.content.startswith(b"%PDF-"):
                        route["outcome"] = "not_a_downloaded_pdf"
                    else:
                        path = destination / (pid + ".pdf")
                        path.write_bytes(response.content)
                        material = base.import_material(pid, path=path, version_id=paper["preferred_version_id"])
                        route.update(outcome=material["status"], material_id=material["material_id"], parse_status=material["parse_status"])
                except (httpx.HTTPError, ValueError) as error:
                    route.update(outcome="request_or_import_failed", error_type=type(error).__name__)
                report["routes"].append(route)
                (folder / "original-acquisition.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(route, ensure_ascii=True), flush=True)
                if base.get_paper(pid)["fulltext_available"]:
                    break
        report["job"] = base.get_job(job["job_id"])
        report["available_selected"] = sum(base.get_paper(pid)["fulltext_available"] for pid in selected["selected_paper_ids"])
        (folder / "original-acquisition.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"available_selected": report["available_selected"], "strict_targets": report["job"]["task_count"]}), flush=True)

if __name__ == "__main__":
    main()
